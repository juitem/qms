
ROOTFS=./download/img/ROOTFS

mkdir -p ./logs/final
python3 ./rootfs_generator/check.py \
  "${ROOTFS}" \
  --logfile-rootfs ./logs/final/check_rootfs.log \
  --root-owned-report ./logs/final/check_root_owned.log \
  --logfile-symlink ./logs/final/check_symlink.log \
  --broken-report ./logs/final/check_broken_symlinks.log \
  -v