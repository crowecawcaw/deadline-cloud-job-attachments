# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from pathlib import Path
import sys

import pytest

from deadline.job_attachments._utils import (
    _get_unique_dest_dir_name,
    _normalize_windows_path,
    _is_relative_to,
    _retry,
)


class TestUtils:
    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="This test is for paths in Windows format and will be skipped on non-Windows systems.",
    )
    @pytest.mark.parametrize(
        ("input_path", "expected"),
        [
            (r"\\?\C:\path\to\file.txt", Path(r"C:\path\to\file.txt")),
            (r"\\?\D:\another\long\path", Path(r"D:\another\long\path")),
            (r"C:\normal\path.txt", Path(r"C:\normal\path.txt")),
            (r"Z:\already\normal\path", Path(r"Z:\already\normal\path")),
        ],
    )
    def test_normalize_windows_path(self, input_path, expected):
        """
        Tests if _normalize_windows_path correctly strips the \\?\\ prefix
        from Windows extended-length paths.
        """
        assert _normalize_windows_path(Path(input_path)) == expected

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="This test is for paths in POSIX path format and will be skipped on Windows.",
    )
    @pytest.mark.parametrize(
        ("path1", "path2", "expected"),
        [
            ("/a/b/c", "/a/b", True),
            (Path("/a/b/c.txt"), "/a", True),
            ("a/b/c", "a/b", True),
            (Path("a/b/c.txt"), "a", True),
            ("/a/b/c", "a/b", False),
            ("a/b/c", "/a/b", False),
            ("/a/b/c", "/d", False),
            ("a/b/c", "b", False),
            ("a/b/c", "d", False),
        ],
    )
    def test_is_relative_to_on_posix(self, path1, path2, expected):
        """
        Tests if the is_relative_to() works correctly when using Posix paths.
        """
        assert _is_relative_to(path1, path2) == expected

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="This test is for paths in Windows path format and will be skipped on non-Windows.",
    )
    @pytest.mark.parametrize(
        ("path1", "path2", "expected"),
        [
            ("C:/a/b/c", "C:/a/b", True),
            (Path("C:/a/b/c.txt"), "C:/a", True),
            ("C:\\a\\b\\c", "C:\\a\\b", True),
            (Path("C:\\a\\b\\c.txt"), "C:\\a", True),
            ("a/b/c", "a/b", True),
            (Path("a/b/c.txt"), "a", True),
            ("C:/a/b/c", "a/b", False),
            ("a/b/c", "C:/a/b", False),
            ("C:/a/b/c", "C:/d", False),
            ("a/b/c", "b", False),
            ("a/b/c", "d", False),
            (
                "\\\\?\\C:\\path\\to\\a\\very\\long\\file\\path\\that\\exceeds\\the\\windows\\max\\path\\length\\for\\testing\\max\\file\\path\\error\\handling\\when\\comparing\\path\\relativity\\using\\job\\attachments",
                "C:\\path\\to\\",
                True,
            ),
            (
                "\\\\?\\C:\\path\\to\\a\\very\\long\\file\\path\\that\\exceeds\\the\\windows\\max\\path\\length\\for\\testing\\max\\file\\path\\error\\handling\\when\\comparing\\path\\relativity\\using\\job\\attachments",
                "C:\\path\\doesnt\\exist\\",
                False,
            ),
            (
                "\\\\?\\C:\\ProgramData\\Amazon\\OpenJD\\session-612345a668724122b6949a232cb4583e1234567d\\assetroot-777691d8674399c12345\\Desktop\\resources\\isolated-black-tree-silhouettes-white-background-shade-trees-used-product-design-isolated-black-tree-silhouettes-1270.jpg",
                Path(
                    "C:\\ProgramData\\Amazon\\OpenJD\\session-612345a668724122b6949a232cb4583e1234567d\\assetroot-777691d8674399c12345"
                ),
                True,
            ),
        ],
    )
    def test_is_relative_to_on_windows(self, path1, path2, expected):
        """
        Tests if the is_relative_to() works correctly when using Windows paths.
        """
        assert _is_relative_to(path1, path2) == expected

    def test_retry(self):
        """
        Test a function that throws an exception is retried.
        """
        call_count = 0

        # Given
        @_retry(ExceptionToCheck=NotImplementedError, tries=2, delay=0.1, backoff=0.1)
        def test_bad_function():
            nonlocal call_count
            call_count = call_count + 1
            if call_count == 1:
                raise NotImplementedError()

        # When
        test_bad_function()

        # Then
        assert call_count == 2


class TestGetUniqueDestDirName:
    """The name prefixes every asset path a job's applications open, so its length
    is charged against MAX_PATH on Windows.
    """

    ROOT = "/home/username/documents/my_project"

    def test_name_length_is_bounded(self):
        # "assetroot-" plus 12 hex characters. The digest was 20.
        assert len(_get_unique_dest_dir_name(self.ROOT)) == 22

    def test_prefix_is_preserved(self):
        # Globbed by two job scripts that should be reading the destinations out of
        # {{Session.PathMappingRulesFile}} instead. Guarding it keeps this change
        # independent of fixing them, not because the prefix is a contract.
        assert _get_unique_dest_dir_name(self.ROOT).startswith("assetroot-")

    def test_name_is_derived_from_the_root(self):
        # No retry exists to resolve a collision, so the name must be a pure
        # function of the root and must differ between roots.
        assert _get_unique_dest_dir_name(self.ROOT) == _get_unique_dest_dir_name(self.ROOT)
        assert _get_unique_dest_dir_name(self.ROOT) != _get_unique_dest_dir_name(self.ROOT + "2")

    def test_distinct_roots_get_distinct_names(self):
        roots = [f"/home/username/documents/project_{n}" for n in range(1000)]

        names = {_get_unique_dest_dir_name(root) for root in roots}

        assert len(names) == len(roots)
