cd /home/juitem/ContainerFolder/quick_symbolizer

#sudo rm -rf ./download/img/ROOTFS_HYBRID


cd ~/ContainerFolder/quick_symbolizer

python3 ./hybrid_rootfs_gen.py \
  ./download/img/rootfs.img \
  --outdir ./download/img/ROOTFS_HYBRID \
  --chmod 755 \
  --logfile-rootfs ./logs/rootfs_hybrid.log \
  -v
