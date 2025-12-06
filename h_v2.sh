#!/bin/bash

set -e

IMG=./download/img/rootfs.img
ROOTFS=./download/img/ROOTFS_HYBRID
LOGDIR=./logs
CHOWN_LOG="${LOGDIR}/chown_hybrid.log"

# 0. Clean & prepare
rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}"
mkdir -p "${LOGDIR}"

# Initialize chown log
echo "[INFO] Chown log start" > "${CHOWN_LOG}"

# 1. Extract only (NO chmod, NO symlink rewrite here)
python3 rootfs_genertor/hybrid_rootfs_gen_v2.py \
  "${IMG}" \
  --outdir "${ROOTFS}" \
  --mode 3 \
  --logfile-rootfs "${LOGDIR}/rootfs_hybrid.log" \
  --root-owned-report "${LOGDIR}/root_owned_hybrid.log" \
  -v

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

# 4. Rewrite symlinks (relative paths inside ROOTFS)
python3 rootfs_generator/rewrite_symlinks.py \
  --rootfs "${ROOTFS}" --target "${ROOTFS}" \
  --logfile-symlink "${LOGDIR}/symlink_hybrid.log" \
  --broken-report "${LOGDIR}/broken_symlinks_hybrid.log" \
  -v

echo "[INFO] Done."
