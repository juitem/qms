#!/bin/bash

set -e


function repeat() {
    # IMG=./download/img/rootfs.img
    # ROOTFS=./download/img/ROOTFS_HYBRID
    # LOGDIR=./logs
    # CHOWN_LOG="${LOGDIR}/chown_hybrid.log"
    IMG=$1
    ROOTFS=$2
    OUTDIR=$3
    LOGDIR=$4
    CHOWN_LOG="${LOGDIR}/$5"

  mkdir -p "${OUTDIR}"
  chown "$(id -u)":"$(id -g)" ${OUTDIR}
  mkdir -p "${LOGDIR}"

  # Initialize chown log
  echo "[INFO] Chown log start" > "${CHOWN_LOG}"

  # 1. Extract only (NO chmod, NO symlink rewrite here)
  python3 rootfs_generator/hybrid_rootfs_gen_v2.py \
    "${IMG}" \
    --rootfs-dir "${ROOTFS}" \
    --outdir "${OUTDIR}" \
    --mode 1 \
    --logfile-rootfs "${LOGDIR}/rootfs_hybrid.log" \
    --root-owned-report "${LOGDIR}/root_owned_hybrid.log" \
    --chmod-scope rootfs \
    --chmod 755 \
    -v

  set +e
  # 2. Apply chmod 755 (best-effort)
  echo "[INFO] chmod -R 755 ${ROOTFS}"
  chmod -R 755 "${ROOTFS}" 2>> "${CHOWN_LOG}" || true

  # 3. Selective chown only for root-owned entries (non-blocking)
  echo "[INFO] Running selective chown..." | tee -a "${CHOWN_LOG}"

  if [[ -f "${LOGDIR}/root_owned_hybrid.log" ]]; then
    while IFS= read -r line; do
      # Expected format: "DIR  /path" or "FILE /path"
      target=$(echo "${line}" | awk '{print $2}')
      if [[ -e "$target" ]]; then
        chown "$(id -u)":"$(id -g)" "$target" 2>> "${CHOWN_LOG}" \
          || echo "[WARN] chown failed for $target" >> "${CHOWN_LOG}"
      fi
    done < "${LOGDIR}/root_owned_hybrid.log"
  else
    echo "[INFO] No root-owned report found; skipping chown." | tee -a "${CHOWN_LOG}"
  fi

  echo "[INFO] chown completed." | tee -a "${CHOWN_LOG}"
  set -e

  set +e
  # 4. Rewrite symlinks (relative paths inside ROOTFS)
  python3 rootfs_generator/rewrite_symlinks.py \
    --rootfs "${ROOTFS}" --target "${OUTDIR}" \
    --logfile-symlink "${LOGDIR}/symlink_hybrid.log" \
    --broken-report "${LOGDIR}/broken_symlinks_hybrid.log" \
    -v
  set -e

  echo "[INFO] Done."

}


# 0. Clean & prepare
IMG=./download/img/rootfs.img
ROOTFS=./download/img/ROOTFS
OUTDIR=./download/img/ROOTFS
LOGDIR=./logs/rootfs
CHOWN_LOG="${LOGDIR}/chown_rootfs.log"

rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}"
repeat "${IMG}" "${ROOTFS}" "${OUTDIR}" "${LOGDIR}" "chown_rootfs_hybrid.log"

IMG=./download/img/hal.img
ROOTFS=./download/img/ROOTFS
OUTDIR=./download/img/ROOTFS/hal
LOGDIR=./logs/hal
CHOWN_LOG="${LOGDIR}/chown_hal.log"

repeat "${IMG}" "${ROOTFS}" "${OUTDIR}" "${LOGDIR}" "chown_hal_hybrid.log"

IMG=./download/img/system-data.img
ROOTFS=./download/img/ROOTFS
OUTDIR=./download/img/ROOTFS/opt
LOGDIR=./logs/opt
CHOWN_LOG="${LOGDIR}/chown_opt.log"

mkdir -p ./download/img/ROOTFS/opt/usr/home

repeat "${IMG}" "${ROOTFS}" "${OUTDIR}" "${LOGDIR}" "chown_opt_hybrid.log"

IMG=./download/img/modules.img
ROOTFS=./download/img/ROOTFS
OUTDIR=./download/img/ROOTFS/lib/modules
LOGDIR=./logs/modules
CHOWN_LOG="${LOGDIR}/chown_modules.log"
repeat "${IMG}" "${ROOTFS}" "${OUTDIR}" "${LOGDIR}" "chown_modules_hybrid.log"

