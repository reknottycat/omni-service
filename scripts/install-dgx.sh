#!/usr/bin/env bash
# Fresh Ubuntu 24.04 installation only. Account authorization is a separate step.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
usage() {
  cat <<'USAGE'
Usage: bash scripts/install-dgx.sh TEAM [--install-system-deps]
       [--bundle FILE --bundle-sha256 SHA256] [--source-build] [--jobs 1..16]

Creates a new private instance and prepares verified dependencies only.
It does not log in, start/enable a service, or overwrite an existing instance.
--install-system-deps uses sudo apt-get for Ubuntu prerequisites if requested.
Online installs build pinned TDLib source. Native bundles do not need a compiler.
USAGE
}
if [[ ${1:-} == --help || ${1:-} == -h ]]; then usage; exit 0; fi
if [[ $# -lt 1 || ! $1 =~ ^[a-z0-9][a-z0-9-]{0,47}$ ]]; then usage >&2; exit 2; fi
omni_team=$1
shift
omni_system_deps=0
omni_source_build=0
omni_bundle=
omni_bundle_sha=
omni_jobs=2
while [[ $# -gt 0 ]]; do
  case $1 in
    --install-system-deps) omni_system_deps=1; shift ;;
    --source-build) omni_source_build=1; shift ;;
    --bundle|--bundle-sha256|--jobs)
      if [[ $# -lt 2 || -z $2 ]]; then usage >&2; exit 2; fi
      case $1 in
        --bundle) omni_bundle=$2 ;;
        --bundle-sha256) omni_bundle_sha=$2 ;;
        --jobs) omni_jobs=$2 ;;
      esac
      shift 2 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done
if [[ $(id -u) == 0 ]]; then
  printf 'Run as the normal service user; do not sudo the whole installer.\n' >&2
  exit 1
fi
if [[ $(uname -s) != Linux || ! $(uname -m) =~ ^(aarch64|x86_64)$ ]]; then
  printf 'This installer requires native Linux aarch64 or x86_64.\n' >&2; exit 1
fi
if ! { . /etc/os-release; [[ $ID == ubuntu && $VERSION_ID == 24.04 ]]; }; then
  printf 'This fresh-system installer targets Ubuntu 24.04 (DGX OS).\n' >&2; exit 1
fi
if [[ ! $omni_jobs =~ ^([1-9]|1[0-6])$ ]]; then usage >&2; exit 2; fi
if [[ -n $omni_bundle || -n $omni_bundle_sha ]]; then
  if [[ ! -f $omni_bundle || -L $omni_bundle || ! $omni_bundle_sha =~ ^[0-9a-f]{64}$ ]]; then
    printf 'Supply a regular bundle file and its trusted SHA256.\n' >&2; exit 2
  fi
  omni_actual_sha=$(sha256sum -- "$omni_bundle")
  if [[ ${omni_actual_sha%% *} != "$omni_bundle_sha" ]]; then
    printf 'Bundle SHA256 mismatch; no packages or instance files changed.\n' >&2; exit 1
  fi
else
  omni_source_build=1
fi
omni_config_home=${XDG_CONFIG_HOME:-$HOME/.config}
omni_data_home=${XDG_DATA_HOME:-$HOME/.local/share}
if [[ $omni_config_home != /* || $omni_data_home != /* ]]; then
  printf 'XDG paths must be absolute.\n' >&2; exit 2
fi
for omni_existing in "$omni_config_home/omni-service/$omni_team" \
    "$omni_data_home/omni-service/$omni_team" \
    "$omni_config_home/systemd/user/omni-service-$omni_team.service"; do
  if [[ -e $omni_existing || -L $omni_existing ]]; then
    printf 'Refusing to overwrite existing instance: %s\nChoose a new team name.\n' "$omni_existing" >&2
    exit 1
  fi
done
if [[ $omni_system_deps == 1 ]]; then
  command -v sudo >/dev/null || { printf 'sudo is required to install Ubuntu packages.\n' >&2; exit 1; }
  omni_packages=(ca-certificates python3.12 python3.12-venv libstdc++6 zlib1g)
  if [[ $omni_source_build == 1 ]]; then
    omni_packages+=(build-essential clang cmake gperf libssl-dev zlib1g-dev)
  fi
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends "${omni_packages[@]}"
fi
if ! command -v python3.12 >/dev/null; then
  printf 'Python 3.12 missing; rerun with --install-system-deps.\n' >&2; exit 1
fi
omni_options=("$omni_team" prepare --fresh --jobs "$omni_jobs")
if [[ -n $omni_bundle ]]; then
  omni_options+=(--bundle "$omni_bundle" --bundle-sha256 "$omni_bundle_sha")
fi
# clang limits TDLib's peak compile memory on small machines. A native bundle
# never enters this build path. User-selected compiler paths remain respected.
if [[ $omni_source_build == 1 ]] && command -v clang++ >/dev/null; then
  export CC=${CC:-clang}
  export CXX=${CXX:-clang++}
fi
exec python3.12 -I -B "$script_dir/../deploy/setup_personal.py" "${omni_options[@]}"
