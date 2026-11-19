#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'Usage: %s --source-root ABSOLUTE_WSL_PATH --staging-root C:\\mif-build --output-root C:\\mif-build\\artifacts --instanseg-model-root C:\\reviewed-model --native-reader-build-dir C:\\native-build [--accelerator cpu|cuda]\n' "${0##*/}" >&2
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 2
}

normalize_windows_root() {
    local label="$1"
    local value="$2"
    local drive_root_regex='^[A-Za-z]:\\'
    local remainder part
    local -a parts=()

    [[ -n "$value" ]] || die "$label must be an absolute drive-rooted Windows path"
    if [[ "${value:0:2}" == '\\' ]]; then
        die "$label must not be a UNC or device-namespace path"
    fi
    [[ "$value" =~ $drive_root_regex ]] || die "$label must be an absolute drive-rooted Windows path"
    [[ "$value" != */* ]] || die "$label must use native Windows separators"
    [[ "$value" != *"'"* ]] || die "$label contains a character forbidden by the PowerShell handoff"
    [[ "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *$'\t'* ]] || die "$label contains control characters"

    remainder="${value:3}"
    IFS='\' read -r -a parts <<< "$remainder"
    for part in "${parts[@]}"; do
        [[ "$part" != . && "$part" != .. ]] || die "$label contains path traversal"
    done
    while [[ ${#value} -gt 3 && "${value: -1}" == '\' ]]; do
        value="${value::-1}"
    done
    printf '%s\n' "$value"
}

source_root=''
staging_root=''
output_root=''
instanseg_model_root=''
native_reader_build_dir=''
accelerator='cpu'
while (( $# )); do
    case "$1" in
        --source-root)
            [[ $# -ge 2 && -z "$source_root" ]] || { usage; exit 2; }
            source_root="$2"
            shift 2
            ;;
        --staging-root)
            [[ $# -ge 2 && -z "$staging_root" ]] || { usage; exit 2; }
            staging_root="$2"
            shift 2
            ;;
        --output-root)
            [[ $# -ge 2 && -z "$output_root" ]] || { usage; exit 2; }
            output_root="$2"
            shift 2
            ;;
        --instanseg-model-root)
            [[ $# -ge 2 && -z "$instanseg_model_root" ]] || { usage; exit 2; }
            instanseg_model_root="$2"
            shift 2
            ;;
        --accelerator)
            [[ $# -ge 2 && ( "$2" == cpu || "$2" == cuda ) ]] || { usage; exit 2; }
            accelerator="$2"
            shift 2
            ;;
        --native-reader-build-dir)
            [[ $# -ge 2 && -z "$native_reader_build_dir" ]] || { usage; exit 2; }
            native_reader_build_dir="$2"
            shift 2
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done
[[ -n "$source_root" && -n "$staging_root" && -n "$output_root" && -n "$instanseg_model_root" && -n "$native_reader_build_dir" ]] || {
    usage
    exit 2
}

[[ "$source_root" == /* ]] || die "source root must be an absolute WSL path"
[[ "$source_root" != *$'\n'* && "$source_root" != *$'\r'* ]] || die "source root contains control characters"
[[ -d "$source_root" ]] || die "source root is not a directory"
source_root="$(realpath -e -- "$source_root")"
staging_root="$(normalize_windows_root "staging root" "$staging_root")"
output_root="$(normalize_windows_root "output root" "$output_root")"
normalized_instanseg_model_root="$(normalize_windows_root "InstanSeg model root" "$instanseg_model_root")"
[[ "$instanseg_model_root" == "$normalized_instanseg_model_root" && "$instanseg_model_root" != *'\\'* ]] || die "InstanSeg model root must be a canonical Windows path"
normalized_native_reader_build_dir="$(normalize_windows_root "native image reader build directory" "$native_reader_build_dir")"
[[ "$native_reader_build_dir" == "$normalized_native_reader_build_dir" && "$native_reader_build_dir" != *'\\'* ]] || die "native image reader build directory must be a canonical Windows path"

snapshot_root="$(mktemp -d --tmpdir mif-windows-source.XXXXXXXX)"
build_root_wsl=''
cleanup() {
    local status="$?"
    rm -rf -- "$snapshot_root"
    if [[ "$status" -ne 0 && -n "$build_root_wsl" ]]; then
        printf 'failed build workspace is preserved at %s\n' "$build_root_wsl" >&2
    fi
}
trap cleanup EXIT

# Capture once in WSL; Windows verifies the archive at the native handoff.
archive_sha256="$(env -u PYTHONPATH python3 "$source_root/packaging/release_metadata.py" "$source_root" "$snapshot_root/source.tar")"
staging_root_wsl="$(wslpath -u -- "$staging_root")"
[[ "$staging_root_wsl" == /mnt/* ]] || die "staging root must map to a local Windows drive"
mkdir -p -- "$staging_root_wsl/Build With Spaces"
build_root_wsl="$staging_root_wsl/Build With Spaces/$archive_sha256"
mkdir -- "$build_root_wsl" || die "source staging root already exists; choose a fresh staging root for retry"
source_archive_wsl="$build_root_wsl/source.tar"
cp -- "$snapshot_root/source.tar" "$source_archive_wsl"
env -u PYTHONPATH python3 "$source_root/scripts/build_portable.py" extract-source \
    "$source_archive_wsl" "$build_root_wsl/source"
source_windows="$(wslpath -w -- "$build_root_wsl/source")"
source_archive_windows="$(wslpath -w -- "$source_archive_wsl")"
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass \
    -File "$source_windows\scripts\build_windows.ps1" \
    -SourceRoot "$source_windows" -SourceArchive "$source_archive_windows" \
    -SourceArchiveSha256 "$archive_sha256" \
    -StagingRoot "$staging_root" -OutputRoot "$output_root" \
    -InstanSegModelRoot "$instanseg_model_root" \
    -NativeReaderBuildDir "$native_reader_build_dir" -Accelerator "$accelerator"
