#!/usr/bin/env bash
# Download the Tier 1/2 assets found missing on 2026-09-01.
# Safe to rerun: assets whose current-catalog TIFF already exists are skipped.
set -u

FETCH_SCRIPT="/workspace/fetch_all.py"
OUT_DIR="/home/cefect/LS/10_IO/2501_NSFc/FIM_Bench/fetch"
failed=0

while IFS= read -r asset_id; do
    [[ -z "$asset_id" ]] && continue
    tier="${asset_id%%/*}"
    remainder="${asset_id#*/}"
    site_id="${remainder%%/*}"
    asset_base="${asset_id##*/}"
    expected_tif="${OUT_DIR}/${tier}/${site_id}/${asset_base}.tif"

    if [[ -f "$expected_tif" ]]; then
        echo "SKIP existing: $asset_id"
        continue
    fi

    echo "DOWNLOAD: $asset_id"
    if ! conda run -n fimbench python "$FETCH_SCRIPT" \
        --out-dir "$OUT_DIR" \
        --asset-id "$asset_id"; then
        echo "FAILED: $asset_id" >&2
        failed=$((failed + 1))
    fi
done <<'ASSET_IDS'
Tier_1/AI_20110412_965145W472341N/AI_0_5m_20110412_965145W472341N_BM
Tier_1/AI_20110414_970623W475915N/AI_0_5m_20110414_970623W475915N_BM
Tier_1/AI_20160103_912407W345941N/AI_0_5m_20160103T4_912407W345941N_BM
Tier_1/AI_20170831T1_953124W295936N/AI_0_3m_20170831T1_953124W295936N_BM
Tier_1/AI_20170903_953036W293058N/AI_0_3m_20170903_953036W293058N_BM
Tier_2/PSS_20161014T150759_780013W352043N/PSS_3_1m_20161014T150759_780013W352043N_BM
Tier_2/PSS_20170830T162251_964235W294719N/PSS_3_1m_20170830T162251_964235W294719N_BM
Tier_2/PSS_20240623T172005_953000W424913N/PSS_3_1m_20240623T172005_953000W424913N_BM
Tier_2/PSS_20240624T162946_950319W430430N/PSS_3_4m_20240624T162946_950319W430430N_BM
Tier_2/PSS_20240624T162946_951226W430652N/PSS_3_0m_20240624T162946_951226W430652N_BM
Tier_2/PSS_20240624T163137_952652W425148N/PSS_3_1m_20240624T163137_952652W425148N_BM
Tier_2/PSS_20240624T164337_962932W431356N/PSS_3_2m_20240624T164337_962932W431356N_BM
Tier_2/PSS_20240624T164337_963354W431528N/PSS_3_2m_20240624T164337_963354W431528N_BM
Tier_2/PSS_20240624T173108_960707W431805N/PSS_2_8m_20240624T173108_960707W431805N_BM
ASSET_IDS

if (( failed > 0 )); then
    echo "$failed asset download(s) failed." >&2
    exit 1
fi

echo "All listed missing assets are present."
