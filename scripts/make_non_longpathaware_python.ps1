# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

<#
.SYNOPSIS
    Produces a copy of python.exe whose application manifest does NOT declare
    longPathAware, and prints its path.

.DESCRIPTION
    CPython's python.exe has declared longPathAware since 3.6, so a stock interpreter
    can exceed MAX_PATH whenever the machine-wide LongPathsEnabled registry setting is
    on. That makes stock python.exe unable to reproduce the failure this fix addresses.

    Job attachments code also runs inside host processes that do NOT declare the flag:
    DCC-embedded interpreters (Cinema 4D, After Effects), and pywin32's
    pythonservice.exe used by the Windows Worker Agent running as a service. For those
    hosts the registry setting has no effect and the \\?\ prefix is the only mechanism
    available.

    This script builds a stand-in for such a host by copying python.exe and re-embedding
    its manifest with longPathAware removed. The copy is placed in the same directory as
    the original so it resolves sys.prefix and finds its DLLs unchanged.

    Requires mt.exe from the Windows SDK, which is present on GitHub's windows-latest
    runner images.

.PARAMETER OutputName
    File name for the patched copy. Defaults to python-nolongpath.exe.

.OUTPUTS
    The full path to the patched interpreter, on stdout, as the last line.
#>

[CmdletBinding()]
param(
    [string]$OutputName = "python-nolongpath.exe"
)

$ErrorActionPreference = "Stop"

function Find-MtExe {
    $candidates = Get-ChildItem -Path "${env:ProgramFiles(x86)}\Windows Kits\10\bin", "${env:ProgramFiles}\Windows Kits\10\bin" `
        -Filter "mt.exe" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "\\x64\\" } |
        Sort-Object FullName -Descending

    if ($candidates.Count -eq 0) {
        throw "mt.exe not found. It ships with the Windows SDK; install the SDK or run this on a runner image that includes it."
    }
    return $candidates[0].FullName
}

$mt = Find-MtExe
Write-Host "Using mt.exe: $mt"

$sourceExe = (Get-Command python).Source
Write-Host "Source interpreter: $sourceExe"

$targetExe = Join-Path (Split-Path $sourceExe -Parent) $OutputName
Copy-Item -Path $sourceExe -Destination $targetExe -Force

$work = Join-Path $env:RUNNER_TEMP "longpath-manifest"
if (-not $work) { $work = Join-Path $env:TEMP "longpath-manifest" }
New-Item -ItemType Directory -Path $work -Force | Out-Null
$manifestPath = Join-Path $work "python.manifest"

# Extract the embedded manifest (resource id 1) so we can edit it.
& $mt -nologo "-inputresource:$targetExe;#1" "-out:$manifestPath"
if ($LASTEXITCODE -ne 0) { throw "mt.exe failed to extract the manifest (exit $LASTEXITCODE)" }

$manifest = Get-Content -Path $manifestPath -Raw

if ($manifest -notmatch "longPathAware") {
    throw "The source interpreter's manifest does not declare longPathAware, so there is nothing to remove. Expected a CPython >= 3.6 python.exe. Aborting rather than producing a copy that proves nothing."
}

# Flip the declaration to false rather than deleting the element: it keeps the manifest
# schema-valid, and false is the documented default for a host that opts out.
$patched = $manifest -replace `
    "(<longPathAware[^>]*>)\s*true\s*(</longPathAware>)", '$1false$2'

if ($patched -eq $manifest) {
    throw "Failed to rewrite the longPathAware element. Manifest shape may have changed; inspect $manifestPath."
}

Set-Content -Path $manifestPath -Value $patched -Encoding UTF8

# Re-embed the edited manifest into the copy.
& $mt -nologo "-manifest:$manifestPath" "-outputresource:$targetExe;#1"
if ($LASTEXITCODE -ne 0) { throw "mt.exe failed to embed the patched manifest (exit $LASTEXITCODE)" }

# Confirm the patch took, so a silent failure cannot masquerade as a valid host.
$verifyPath = Join-Path $work "verify.manifest"
& $mt -nologo "-inputresource:$targetExe;#1" "-out:$verifyPath"
if ($LASTEXITCODE -ne 0) { throw "mt.exe failed to read back the manifest (exit $LASTEXITCODE)" }

$verify = Get-Content -Path $verifyPath -Raw
if ($verify -notmatch "<longPathAware[^>]*>\s*false\s*</longPathAware>") {
    throw "Read-back check failed: the patched interpreter still does not declare longPathAware=false. Manifest was:`n$verify"
}

Write-Host "Patched interpreter is not long path aware: $targetExe"
Write-Output $targetExe
