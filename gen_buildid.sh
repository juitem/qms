# 예: ROOTFS 아래 /usr/lib64, /usr/bin을 스캔해서 .build-id 링크 생성
rm ./download/logs/buildid_links.tsv
python3 ./gen_buildid/ConstructRegularDebugLink.py \
  --rootfs ./download/img/ROOTFS \
  --debug-root ./download/img/ROOTFS/usr/lib/debug/.build-id \
  --tsv-log ./download/logs/buildid_links.tsv \
  --overwrite \
  --verbose \
  --target / \
  --buildid-backend auto
#   --target /usr/bin \
#   --target /usr/apps \
#  --buildid-backend 
