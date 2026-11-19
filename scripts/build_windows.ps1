param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [Parameter(Mandatory=$true)][string]$StagingRoot,
    [Parameter(Mandatory=$true)][string]$OutputRoot,
    [Parameter(Mandatory=$true)][string]$InstanSegModelRoot,
    [Parameter(Mandatory=$true)][string]$NativeReaderBuildDir,
    [string]$PythonLauncher = 'py.exe',
    [ValidateSet('cpu', 'cuda')][string]$Accelerator = 'cpu',
    [string]$SourceArchive,
    [string]$SourceArchiveSha256
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-LocalDirectory {
    param([string]$LiteralPath, [string]$Purpose, [switch]$Create)
    if ($LiteralPath -notmatch '^[A-Za-z]:\\' -or $LiteralPath.StartsWith('\\')) {
        throw "$Purpose must be an absolute local drive path"
    }
    $fullPath = [IO.Path]::GetFullPath($LiteralPath)
    # Check the existing ancestry before creating build/output directories.
    $cursor = $fullPath
    while (-not (Test-Path -LiteralPath $cursor)) { $cursor = Split-Path -Parent $cursor }
    $item = Get-Item -LiteralPath $cursor -Force
    if (-not $item.PSIsContainer) { throw "$Purpose must be a directory" }
    while ($null -ne $item) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Purpose contains a junction or symbolic link: $($item.FullName)"
        }
        $item = $item.Parent
    }
    $volume = Get-Volume -DriveLetter $fullPath.Substring(0, 1)
    if ($volume.FileSystem -ne 'NTFS' -or [string]$volume.DriveType -ne 'Fixed') {
        throw "$Purpose must use a local fixed NTFS volume"
    }
    if ($Create) { [void][IO.Directory]::CreateDirectory($fullPath) }
    $resolved = Get-Item -LiteralPath $fullPath -Force
    if (-not $resolved.PSIsContainer) { throw "$Purpose must be a directory" }
    return [IO.Path]::GetFullPath($resolved.FullName)
}

$operatingSystem = Get-CimInstance -ClassName Win32_OperatingSystem
if ($operatingSystem.Caption -notmatch 'Windows 11' -or
    [Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture -ne [Runtime.InteropServices.Architecture]::X64) {
    throw 'portable builder requires Windows 11 x86-64 and native x86-64 PowerShell'
}
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'portable builder must run as an ordinary user'
}

$SourceRoot = Resolve-LocalDirectory $SourceRoot 'source root'
$StagingRoot = Resolve-LocalDirectory $StagingRoot 'staging root' -Create
$OutputRoot = Resolve-LocalDirectory $OutputRoot 'output root' -Create
$InstanSegModelRoot = Resolve-LocalDirectory $InstanSegModelRoot 'InstanSeg model root'
$NativeReaderBuildDir = Resolve-LocalDirectory $NativeReaderBuildDir 'native reader build directory'
$BuildParent = Resolve-LocalDirectory (Join-Path $StagingRoot 'Build With Spaces') 'build parent' -Create
$arguments = @(
    '-3.12-64', '-I', '-B', (Join-Path $SourceRoot 'scripts\build_portable.py'), 'build',
    '--source-root', $SourceRoot, '--output-dir', $OutputRoot, '--build-parent', $BuildParent,
    '--platform', 'windows-x86_64', '--instanseg-model-root', $InstanSegModelRoot,
    '--native-reader-build-dir', $NativeReaderBuildDir, '--accelerator', $Accelerator
)
if ($SourceArchive) {
    $arguments += @('--source-archive', $SourceArchive, '--source-archive-sha256', $SourceArchiveSha256)
}
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
& $PythonLauncher @arguments
exit $LASTEXITCODE
