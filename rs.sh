  # 4. Rewrite symlinks (relative paths inside ROOTFS)
ROOTFS=./download/img/ROOTFS
OUTDIR=./download/img/ROOTFS
LOGDIR=./logs/rpm
CHOWN_LOG="${LOGDIR}/chown_opt.log"

  python3 rootfs_generator/rewrite_symlinks.py \
    --rootfs "${ROOTFS}" --target "${OUTDIR}" \
    --logfile-symlink "${LOGDIR}/symlink_hybrid.log" \
    --broken-report "${LOGDIR}/broken_symlinks_hybrid.log"
#    -v

echo "BSDTAR"
bsdtar -xf ./download/rpms/*libc*.rpm -C ./download/img/ROOTFS/


  python3 rootfs_generator/rewrite_symlinks.py \
    --rootfs "${ROOTFS}" --target "${OUTDIR}" \
    --logfile-symlink "${LOGDIR}/symlink_hybrid.log" \
    --broken-report "${LOGDIR}/broken_symlinks_hybrid.log"
#    -v
