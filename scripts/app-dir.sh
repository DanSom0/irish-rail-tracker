# shellcheck shell=bash
# Sourced by scripts that use the shared .env. Sets SCRIPT_DIR to the calling script's
# directory, then changes to the app directory: the parent of scripts/ or, for a release
# bundle in releases/<sha>/scripts, the directory that holds releases/.
SCRIPT_DIR=$(cd -P "$(dirname "${BASH_SOURCE[1]}")" && pwd)
cd "$SCRIPT_DIR/.." || exit
if [[ $(basename "$(dirname "$PWD")") == releases ]]; then
  cd ../.. || exit
fi
