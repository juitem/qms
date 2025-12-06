cd /home/juitem/ContainerFolder/quick_symbolizer

#sudo rm -rf ./download/img/ROOTFS_HYBRID


cd ~/ContainerFolder/quick_symbolizer

# python3 ./hybrid_rootfs_gen.py \
#   ./download/img/rootfs.img \
#   --outdir ./download/img/ROOTFS_HYBRID \
#   --chmod 755 \
#   --logfile-rootfs ./logs/rootfs_hybrid.log \
#   -v

# python3 ./rootfs_generator/rewrite_symlinks.py \
#   --logfile-symlink ./logs/symlink_hybrid.log \
#   --broken-report ./logs/broken_symlinks_hybrid.log \
#   --rootfs ./download/img/ROOTFS_HYBRID \
#   --target ./download/img/ROOTFS_HYBRID \
#   -v

python3 ./hybrid_rootfs_gen.py \
  ./download/img/rootfs.img \
  --outdir ./download/img/ROOTFS_HYBRID \
  --chmod 755 \
  --logfile-rootfs ./logs/rootfs_hybrid.log \
  --broken-report ./logs/broken_symlinks_hybrid.log \
  -v
  # --logfile-symlink ./logs/symlink_hybrid.log \


# python3 ./rootfs_generator/rewrite_symlinks.py \
#   --logfile-symlink ./logs/symlink_hybrid.log \
#   --broken-report ./logs/broken_symlinks_hybrid.log \
#   --rootfs ./download/img/ROOTFS_HYBRID \
#   --target ./download/img/ROOTFS_HYBRID \
#   -v