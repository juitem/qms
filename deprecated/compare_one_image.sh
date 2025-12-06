#!/bin/bash
set -e

IMG="./download/img/rootfs.img"
ROOTFS="./download/img/ROOTFS"
LOGDIR="./logs"

mkdir -p "$ROOTFS"
mkdir -p "$LOGDIR"

python3 ./rootfs_gen_ext4_pure.py \
  "$IMG" \
  --outdir "$ROOTFS" \
  --chmod 755 \
  --logfile-rootfs "$LOGDIR/rootfs_pure.log" \
  -v

python3 ./rootfs_generator/rewrite_symlinks.py \
  --rootfs "$ROOTFS" \
  --target "$ROOTFS" \
  --logfile-symlink "$LOGDIR/symlink.log" \
  --broken-report "$LOGDIR/broken_symlinks.log" \
  -v

python3 ./compare_rootfs_vs_img.py \
  "$IMG" \
  --rootfs "$ROOTFS" \
  --logdir "$LOGDIR" \
  -v
  