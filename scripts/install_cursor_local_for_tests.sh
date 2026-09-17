#!/usr/bin/env bash
set -euo pipefail

# Pin the local-agent distribution: the standard Cursor CLI cannot use a gateway.
version=2026.07.23-e383d2b
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64)
    platform=linux
    arch=x64
    checksum=a61354ca57608605bdc59773ae64746f18d1eefea177d31acca16cdf1adfd4bd
    ;;
  Darwin/arm64)
    platform=darwin
    arch=arm64
    checksum=635357ee097bf388525eb3b92e37e9cc7948fe69f7c50b6e030c26ee535de926
    ;;
  *) echo "Supported test platforms: Linux x64 and macOS arm64" >&2; exit 1 ;;
esac

destination="${1:?Usage: bash scripts/install_cursor_local_for_tests.sh INSTALL_DIR}"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
curl --fail --location --retry 3 --connect-timeout 15 --max-time 300 \
  "https://anysphere-binaries.s3.amazonaws.com/lab/$version/$platform/$arch/agent-cli-local-package.tar.gz" \
  --output "$scratch/package.tar.gz"
if command -v sha256sum >/dev/null 2>&1; then
  actual=$(sha256sum "$scratch/package.tar.gz")
else
  actual=$(shasum -a 256 "$scratch/package.tar.gz")
fi
if [[ "${actual%% *}" != "$checksum" ]]; then
  echo "Cursor local CLI checksum mismatch" >&2
  exit 1
fi
mkdir -p "$destination"
tar -xzf "$scratch/package.tar.gz" -C "$destination"
test -x "$destination/dist-package/cursor-agent-local"
echo "$destination/dist-package/cursor-agent-local"
