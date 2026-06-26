# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Functions for handling and retrieving AWS clients."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

import boto3
import botocore
from boto3.s3.transfer import create_transfer_manager
from botocore.client import BaseClient, Config

from .. import version
from .aws_config import (
    S3_CONNECT_TIMEOUT_IN_SECS,
    S3_READ_TIMEOUT_IN_SECS,
    S3_RETRIES_MODE,
    VENDOR_CODE,
)

MAX_SIZE_CACHE = 128

logger = logging.getLogger("deadline.job_attachments")


# Should create a new botocore session since botocore session may be modified by boto3 session/client using it
# https://github.com/boto/boto3/blob/61de529b5f9a7bdcc8c76debb472a7f934d048e6/boto3/session.py#L79
def get_botocore_session() -> botocore.session.Session:
    session = botocore.session.get_session()
    # Use regional endpoints by default for STS and S3 (us-east-1) to avoid
    # cross-region calls to the global endpoint. This is the default in newer verisons,
    # but older botocore versions default to "legacy" which routes through us-east-1.
    session.set_config_variable("sts_regional_endpoints", "regional")
    session.set_config_variable("s3", {"us_east_1_regional_endpoint": "regional"})
    return session


@lru_cache(maxsize=MAX_SIZE_CACHE)
def get_boto3_session(
    botocore_session: botocore.session.Session = get_botocore_session(),
) -> boto3.session.Session:
    return boto3.session.Session(botocore_session=botocore_session)


def apply_proxy_settings(
    session: boto3.session.Session,
    *,
    https_proxy: Optional[str] = None,
    ca_bundle: Optional[str] = None,
) -> boto3.session.Session:
    """
    Apply an HTTPS proxy and/or a custom CA certificate bundle to ``session`` so that
    *every* client built from it -- including the S3 and Deadline clients created by
    ``get_s3_client`` / ``get_deadline_client`` -- routes through the proxy and verifies
    TLS against the bundle.

    Both settings are applied at the session level rather than threaded through each
    client factory because botocore merges a session's default client config into every
    per-client ``botocore.config.Config`` (so ``proxies`` is inherited) and reads the
    session's ``ca_bundle`` config variable for the client's ``verify`` value. This keeps
    proxy/CA coverage uniform across all Deadline-created clients without changing every
    download/upload signature.

    This mirrors the ``settings.https_proxy`` / ``settings.ca_bundle`` config options in
    the ``deadline`` client library, which resolves those values and passes them here.

    Args:
        session: The boto3 session to configure, modified in place.
        https_proxy: The proxy URL to apply for both ``http`` and ``https`` endpoints
            (botocore selects the proxy by the endpoint's scheme). When ``None`` or
            empty, the proxy is left unchanged.
        ca_bundle: Path to a CA certificate bundle used to verify TLS connections. ``~``
            is expanded (botocore does not expand it). When ``None`` or empty, the CA
            bundle is left unchanged.

    Returns:
        The same ``session``, for convenient chaining.
    """
    botocore_session = session._session
    if https_proxy and https_proxy.strip():
        proxy = https_proxy.strip()
        existing = botocore_session.get_default_client_config() or Config()
        # Set both schemes so the proxy is honored regardless of the endpoint's scheme.
        botocore_session.set_default_client_config(
            existing.merge(Config(proxies={"http": proxy, "https": proxy}))
        )
    if ca_bundle and ca_bundle.strip():
        # botocore feeds the session's ``ca_bundle`` config variable into each client's
        # ``verify``. It does not expand ``~``, so expand it here.
        botocore_session.set_config_variable("ca_bundle", os.path.expanduser(ca_bundle.strip()))
    return session


@lru_cache(maxsize=MAX_SIZE_CACHE)
def get_deadline_client(
    session: Optional[boto3.session.Session] = None, endpoint_url: Optional[str] = None
) -> BaseClient:
    """
    Get a boto3 Deadline client to make API calls to Deadline
    """
    if session is None:
        session = get_boto3_session()

    return session.client(VENDOR_CODE, endpoint_url=endpoint_url)


@lru_cache(maxsize=MAX_SIZE_CACHE)
def get_s3_client(
    session: Optional[boto3.Session] = None, s3_max_pool_connections: int = 50
) -> BaseClient:
    """
    Get a boto3 S3 client to make API calls to S3
    """
    if session is None:
        session = get_boto3_session()

    client = session.client(
        "s3",
        config=Config(
            signature_version="s3v4",
            connect_timeout=S3_CONNECT_TIMEOUT_IN_SECS,
            read_timeout=S3_READ_TIMEOUT_IN_SECS,
            retries={"mode": S3_RETRIES_MODE},
            user_agent_extra=f"S3A/Deadline/NA/JobAttachments/{version}",
            max_pool_connections=s3_max_pool_connections,
            # Botocore's default ("when_supported") issues a HEAD before every GET to
            # discover the object's checksum algorithm, doubling the request count when
            # downloading many small objects. We accept the small residual risk of
            # undetected at-rest/in-AWS-network corruption: HTTP Content-Length and TLS
            # already guarantee response bodies arrive complete and untampered in transit,
            # which covers the failure modes that actually matter in practice.
            response_checksum_validation="when_required",
        ),
    )

    def add_expected_bucket_owner(params, model, **kwargs):
        """
        Add the expected bucket owner to the params if the API operation to run can use it.
        """
        if "ExpectedBucketOwner" in model.input_shape.members:
            account_id = get_account_id(session=session)
            if account_id:
                params["ExpectedBucketOwner"] = account_id

    client.meta.events.register("provide-client-params.s3.*", add_expected_bucket_owner)

    return client


@lru_cache(maxsize=MAX_SIZE_CACHE)
def get_s3_transfer_manager(s3_client: BaseClient):
    transfer_config = boto3.s3.transfer.TransferConfig()
    return create_transfer_manager(client=s3_client, config=transfer_config)


@lru_cache(maxsize=MAX_SIZE_CACHE)
def get_account_id(session: Optional[boto3.session.Session] = None) -> Optional[str]:
    """
    Get the account id for the current session, or ``None`` if it cannot be determined.

    Sources the account from the session's frozen credentials, which botocore populates
    automatically for credential providers that know the account (e.g. AssumeRole extracts
    it from the assumed-role ARN, and static credentials can be paired with
    ``AWS_ACCOUNT_ID`` / ``aws_account_id`` in config). Falls back to ``sts:GetCallerIdentity``
    only when the credential provider did not supply an account id.
    """
    if session is None:
        session = get_boto3_session()

    credentials = session.get_credentials()
    if credentials is not None:
        frozen = credentials.get_frozen_credentials()
        account_id = getattr(frozen, "account_id", None)
        if account_id:
            return account_id

    # Fallback for older botocore or credential providers that don't populate account_id
    try:
        return session.client("sts").get_caller_identity()["Account"]
    except Exception:
        logger.debug(
            "Could not determine AWS account ID from session credentials or STS. "
            "S3 requests will not include the ExpectedBucketOwner header."
        )
        return None
