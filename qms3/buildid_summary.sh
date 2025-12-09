# 그냥 요약만 (지금과 동일)
python3 ./qms3/buildid_summary.py \
    --rootfs ./download/img/ROOTFS \
    ./sample.log

# FOUND/LOG/MISMATCH 태그는 그대로 + JSON/CSV로 저장
python3 ./qms3/buildid_summary.py \
    --rootfs ./download/img/ROOTFS \
    --check-mismatch \
    --output-json buildids.json \
    --output-csv  buildids.csv \
    ./sample.log ./other.log