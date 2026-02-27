"""CLI for downloading benchmark FIM data with tier and asset filters.

Download Tier 1 and Tier 2 data (default):
    conda run -n fimbench python fetch_all.py

Download one exact asset id:
    conda run -n fimbench python fetch_all.py --asset-id <asset_id>

By default, downloads are written to:
    /home/cefect/LS/10_IO/2501_NSFc/FIM_Bench/fetch
"""

from __future__ import annotations

import argparse, json
from pathlib import Path
import shutil

import fimeval as fe
import requests
from botocore.exceptions import ClientError
from tqdm.auto import tqdm


DEFAULT_OUT_DIR = "/home/cefect/LS/10_IO/2501_NSFc/FIM_Bench/fetch"


def main_fetch_all(
    *,
    out_dir: str = DEFAULT_OUT_DIR,
    tiers: list[str] | None = None,
    asset_id: str | None = None,
) -> dict:
    """Download filtered benchmark assets with 404 pre-check.

    Parameters
    ----------
    out_dir:
        Directory to write downloaded files.
    tiers:
        Tier filters (for example ``["Tier_1", "Tier_2"]``). Defaults to Tier_1/Tier_2.
    asset_id:
        Optional exact benchmark asset id from catalog (record ``id``) for single-asset runs.

    Returns
    -------
    dict
        Summary with download results, skipped 404 assets, and failures.
        Downloaded files are mirrored to ``<out_dir>/<tier>/<event_folder>/``.
    """
    assert isinstance(out_dir, str) and out_dir.strip(), "out_dir must be a non-empty string."
    assert asset_id is None or (
        isinstance(asset_id, str) and asset_id.strip()
    ), "asset_id must be None or a non-empty string."
    assert tiers is None or all(
        isinstance(v, str) and v.strip() for v in tiers
    ), "tiers must be None or non-empty strings."
    out_dir = str(Path(out_dir).expanduser())
    tiers = tiers or ["Tier_1", "Tier_2"]
    tier_l = [v.strip() for v in tiers if v.strip()]
    tier_set = {v.lower().replace(" ", "_").replace("-", "_") for v in tier_l}
    asset_id = asset_id.strip() if asset_id else None

    # Load catalog once and validate response.
    log_d = fe.benchFIMquery(download=False)
    assert log_d.get("status") == "ok", f"Catalog query failed: {log_d.get('message')}"
    assert log_d.get("matches"), "No benchmark records found in the catalog."

    # Filter records by selected tiers and optional exact asset id.
    selected_l = []
    for match in tqdm(log_d["matches"], desc="Filter catalog records", unit="record"):
        rec = match.get("record") or {}
        tier = str(rec.get("tier") or "").strip().lower().replace(" ", "_").replace("-", "_")
        rec_id = str(rec.get("id") or "").strip()
        if tier not in tier_set:
            continue
        if asset_id and rec_id != asset_id:
            continue
        selected_l.append(rec)
    assert selected_l, "No catalog records matched the provided tiers/asset_id."

    # Pre-check each TIFF URL and skip missing assets before download.
    ready_l, missing_l = [], []
    for rec in tqdm(selected_l, desc="Pre-check asset URLs", unit="asset"):
        file_name = str(rec.get("file_name") or "").strip()
        rec_id = str(rec.get("id") or "").strip()
        tif_url = str(rec.get("tif_url") or "").strip()
        if not file_name or not tif_url:
            missing_l.append(
                {"asset_id": rec_id, "file_name": file_name or None, "reason": "missing_file_name_or_tif_url"}
            )
            continue
        try:
            code = requests.head(tif_url, allow_redirects=True, timeout=10).status_code
        except requests.RequestException as e:
            missing_l.append(
                {"asset_id": rec_id, "file_name": file_name, "reason": f"request_error:{type(e).__name__}"}
            )
            continue
        if code != 200:
            missing_l.append({"asset_id": rec_id, "file_name": file_name, "reason": f"head_status:{code}"})
            continue
        ready_l.append({"asset_id": rec_id, "file_name": file_name})

    # Download assets one-at-a-time so one failure does not abort the batch.
    downloaded_l, failed_l = [], []
    for item in tqdm(ready_l, desc="Download assets", unit="asset"):
        try:
            one_d = fe.benchFIMquery(file_name=item["file_name"], download=True, out_dir=out_dir)
            if one_d.get("status") in {"ok", "partial"}:
                # Mirror S3 structure using asset id: <tier>/<event_folder>/<asset_base>.
                parts = item["asset_id"].split("/")
                assert len(parts) >= 2, f"Unexpected asset_id format: {item['asset_id']}"
                target_dir = Path(out_dir).joinpath(parts[0], parts[1])
                target_dir.mkdir(parents=True, exist_ok=True)
                downloads = (one_d.get("matches") or [{}])[0].get("downloads") or {}
                moved_l = []
                for fp in [downloads.get("tif")] + list(downloads.get("gpkg_files") or []):
                    if not fp:
                        continue
                    src = Path(fp)
                    if not src.exists():
                        continue
                    dst = target_dir / src.name
                    shutil.move(str(src), str(dst))
                    moved_l.append(str(dst))
                downloaded_l.append({**item, "output_dir": str(target_dir), "files": moved_l})
            else:
                failed_l.append(
                    {
                        "asset_id": item["asset_id"],
                        "file_name": item["file_name"],
                        "reason": one_d.get("message") or "unknown_status",
                    }
                )
        except ClientError as e:
            failed_l.append(
                {"asset_id": item["asset_id"], "file_name": item["file_name"], "reason": str(e)}
            )

    # Build a compact status summary for CLI output.
    status = "ok"
    if failed_l or missing_l:
        status = "partial" if downloaded_l else "error"
    msg = (
        f"Downloaded {len(downloaded_l)} asset(s); "
        f"skipped {len(missing_l)} pre-check missing asset(s); "
        f"failed {len(failed_l)} download(s)."
    )
    return {
        "status": status,
        "message": msg,
        "tiers": tier_l,
        "asset_id_filter": asset_id,
        "requested_count": len(selected_l),
        "ready_count": len(ready_l),
        "downloaded_count": len(downloaded_l),
        "skipped_missing_count": len(missing_l),
        "failed_count": len(failed_l),
        "downloaded_assets": downloaded_l,
        "missing_assets": missing_l,
        "failed_assets": failed_l,
    }


def _parse_arguments() -> tuple[tuple, dict]:
    """Parse CLI args for ``main_fetch_all``."""
    parser = argparse.ArgumentParser(
        description="Download benchmark FIM assets with tier/asset filters."
    )
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help="Output directory for downloaded benchmark files.",
    )
    parser.add_argument(
        "--tiers",
        nargs="+",
        default=["Tier_1", "Tier_2"],
        help="Tier list to download (default: Tier_1 Tier_2).",
    )
    parser.add_argument(
        "--asset-id",
        default=None,
        help="Optional exact asset id to download (record id from catalog).",
    )
    ns = parser.parse_args()
    return tuple(), {"out_dir": ns.out_dir, "tiers": ns.tiers, "asset_id": ns.asset_id}


if __name__ == "__main__":
    args, kwargs = _parse_arguments()
    print(json.dumps(main_fetch_all(*args, **kwargs), indent=2))
