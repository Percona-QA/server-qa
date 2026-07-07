#!/bin/bash
# PXB version helpers for bash backup test scripts.
# Usage: source pxb_helper.sh

# PXB release-aware normalization (NOT the same as normalize_version)
normalize_xtrabackup_version() {
  local major=0 minor=0 patch=0 release=0
  if [[ $1 =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)(-([0-9]+))?$ ]]; then
    major=${BASH_REMATCH[1]}
    minor=${BASH_REMATCH[2]}
    patch=${BASH_REMATCH[3]}
    release=${BASH_REMATCH[5]:-0}
  fi
  echo $((major * 1000000 + minor * 10000 + patch * 100 + release))
}

_parse_pxb_version() {
  grep -oE 'xtrabackup version [0-9]+\.[0-9]+\.[0-9]+(-[0-9]+)?' | awk '{print $3}'
}

# Initialize PXB_VERSION from local xtrabackup binary (or explicit path)
init_pxb_version() {
  local xtrabackup_bin="${1:-${xtrabackup_dir}/xtrabackup}"
  PXB_VER=$("$xtrabackup_bin" --no-defaults --version 2>&1 | _parse_pxb_version)
  PXB_VER="${PXB_VER:-0.0.0}"
  PXB_VERSION=$(normalize_xtrabackup_version "$PXB_VER")
}

# Initialize PXB_VERSION from a PXB docker image
init_pxb_version_docker() {
  local image="$1"
  PXB_VER=$(sudo docker run --rm "$image" xtrabackup --version 2>&1 | _parse_pxb_version)
  PXB_VER="${PXB_VER:-0.0.0}"
  PXB_VERSION=$(normalize_xtrabackup_version "$PXB_VER")
}

prepare_args_for_pxb_version() {
  local params="$1"
  if [ "${PXB_VERSION:-0}" -ge "$(normalize_xtrabackup_version "8.4.0-6")" ] \
     && [[ "$params" != *"--check-tables"* ]]; then
    if [ -n "$params" ]; then
      params="$params --check-tables"
    else
      params="--check-tables"
    fi
  fi
  echo "$params"
}
