#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'Usage: %s --source-root ABSOLUTE_LINUX_PATH --output-dir ABSOLUTE_LINUX_PATH --python ABSOLUTE_LINUX_PATH --instanseg-model-root ABSOLUTE_LINUX_PATH --native-reader-build-dir ABSOLUTE_LINUX_PATH [--accelerator cpu|cuda]\n' "${0##*/}" >&2
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 2
}

require_linux_path() {
    local label="$1"
    local value="$2"
    [[ "$value" == /* ]] || die "$label must be an absolute Linux path"
    [[ "$value" != /mnt && "$value" != /mnt/* ]] || die "$label must not be under /mnt"
}

source_root=''
output_dir=''
python_executable=''
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
        --output-dir)
            [[ $# -ge 2 && -z "$output_dir" ]] || { usage; exit 2; }
            output_dir="$2"
            shift 2
            ;;
        --python)
            [[ $# -ge 2 && -z "$python_executable" ]] || { usage; exit 2; }
            python_executable="$2"
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
[[ -n "$source_root" && -n "$output_dir" && -n "$python_executable" && -n "$instanseg_model_root" && -n "$native_reader_build_dir" ]] || {
    usage
    exit 2
}
unset MIF_INSTANSEG_MODEL_BUILD_ROOT MIF_NATIVE_READER_BUILD_DIR

user_id="$(id -u)"
[[ "$user_id" != 0 ]] || die "portable builds must not run as root"

os_id=''
os_version=''
os_release="$(cat /etc/os-release)"
while IFS='=' read -r key value; do
    value="${value#\"}"
    value="${value%\"}"
    case "$key" in
        ID) os_id="$value" ;;
        VERSION_ID) os_version="$value" ;;
    esac
done <<< "$os_release"
[[ "$os_id" == ubuntu ]] || die "builder requires Ubuntu"
[[ "$os_version" == 24.04 ]] || die "builder requires Ubuntu 24.04"
host_architecture="$(uname -m)"
[[ "$host_architecture" == x86_64 ]] || die "builder requires x86_64"

require_linux_path "source root" "$source_root"
require_linux_path "output directory" "$output_dir"
require_linux_path "Python executable" "$python_executable"
require_linux_path "InstanSeg model root" "$instanseg_model_root"
require_linux_path "native image reader build directory" "$native_reader_build_dir"
temporary_parent="${TMPDIR:-/tmp}"
require_linux_path "temporary build directory" "$temporary_parent"

[[ -d "$source_root" ]] || die "source root is not a directory"
[[ -d "$instanseg_model_root" ]] || die "InstanSeg model root is not a directory"
[[ -d "$native_reader_build_dir" ]] || die "Java extension build directory does not exist: $native_reader_build_dir. Use the output directory from scripts/build_native_extension.py."
[[ -x "$python_executable" && -f "$python_executable" ]] || die "Python executable is not an executable regular file"
mkdir -p -- "$output_dir" "$temporary_parent"
source_root="$(realpath -e -- "$source_root")"
output_dir="$(realpath -e -- "$output_dir")"
python_executable="$(realpath -e -- "$python_executable")"
instanseg_model_root_resolved="$(realpath -e -- "$instanseg_model_root")"
native_reader_build_dir_resolved="$(realpath -e -- "$native_reader_build_dir")"
temporary_parent="$(realpath -e -- "$temporary_parent")"
require_linux_path "resolved source root" "$source_root"
require_linux_path "resolved output directory" "$output_dir"
require_linux_path "resolved Python executable" "$python_executable"
require_linux_path "resolved InstanSeg model root" "$instanseg_model_root_resolved"
[[ "$instanseg_model_root" == "$instanseg_model_root_resolved" ]] || die "InstanSeg model root must be a canonical local Linux path"
[[ "$instanseg_model_root" != "$source_root" && "$instanseg_model_root" != "$source_root"/* ]] || die "InstanSeg model root must be outside the source checkout"
require_linux_path "resolved native image reader build directory" "$native_reader_build_dir_resolved"
[[ "$native_reader_build_dir" == "$native_reader_build_dir_resolved" ]] || die "native image reader build directory must be canonical"
[[ "$native_reader_build_dir" != "$source_root" && "$native_reader_build_dir" != "$source_root"/* ]] || die "native image reader build directory must be outside the source checkout"
[[ -f "$native_reader_build_dir/qupath-extension-mif-0.1.0.jar" && ! -L "$native_reader_build_dir/qupath-extension-mif-0.1.0.jar" ]] || die "Java extension JAR is missing or is not a regular file: $native_reader_build_dir/qupath-extension-mif-0.1.0.jar. Build it with scripts/build_native_extension.py first."
require_linux_path "resolved temporary build directory" "$temporary_parent"

python_identity="$(env -u PYTHONPATH "$python_executable" -c 'import platform; print(f"{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]} {platform.machine()}")')"
[[ "$python_identity" == "3.12 x86_64" ]] || die "builder requires CPython 3.12 x86_64"

exec env -u PYTHONPATH "$python_executable" -I -B "$source_root/scripts/build_portable.py" build \
    --source-root "$source_root" --output-dir "$output_dir" \
    --build-parent "$temporary_parent" --platform ubuntu24.04-x86_64 \
    --instanseg-model-root "$instanseg_model_root" \
    --native-reader-build-dir "$native_reader_build_dir" --accelerator "$accelerator"
