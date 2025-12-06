#!/bin/bash

sudo rm -rf ./download/img/ROOTFS
#!/bin/bash

set -e

IMAGE="./download/img/rootfs.img"
ROOTFS="./download/img/ROOTFS"
LOGDIR="./logs"

mkdir -p "$LOGDIR"
mkdir -p "$ROOTFS"

python3 ./rootfs_gen_ext4_pure.py \
  "$IMAGE" \
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