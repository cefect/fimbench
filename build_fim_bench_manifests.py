#!/usr/bin/env python3
"""Build local and fimeval-comparison manifests for FIM_Bench."""

from __future__ import annotations

import csv
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import fimeval as fe
import requests


ROOT = Path("/home/cefect/LS/10_IO/2501_NSFc/FIM_Bench")
OUT_DIR = Path("/workspace")
TIERS = {"Tier_1", "Tier_2"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify(path: Path) -> tuple[str, str, str]:
    rel = path.relative_to(ROOT)
    parts = rel.parts
    tier = parts[1] if len(parts) >= 3 and parts[0] == "fetch" and parts[1] in TIERS else ""
    site_id = parts[2] if tier and len(parts) >= 4 else ""
    name = path.name
    if name.endswith("_BM.tif"):
        kind = "benchmark_raster"
    elif name.endswith("_AOI.gpkg"):
        kind = "aoi_vector"
    elif name.endswith(".tif.aux.xml"):
        kind = "raster_auxiliary"
    elif rel.parts[0] == "labels" and path.suffix.lower() == ".tif":
        kind = "label_raster"
    elif name.endswith((".geojson", ".gpkg")):
        kind = "derived_or_index_vector"
    elif name.endswith(".log"):
        kind = "log"
    elif name.lower().endswith(".md"):
        kind = "documentation"
    else:
        kind = "other"
    return kind, tier, site_id


def head_status(url: str) -> dict[str, object]:
    if not url:
        return {"url": url, "status": None, "content_length": None, "error": "missing_url"}
    try:
        response = requests.head(url, allow_redirects=True, timeout=20)
        return {
            "url": url,
            "status": response.status_code,
            "content_length": response.headers.get("content-length"),
            "error": "",
        }
    except requests.RequestException as exc:
        return {
            "url": url,
            "status": None,
            "content_length": None,
            "error": type(exc).__name__,
        }


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    local_rows: list[dict[str, object]] = []
    all_files = sorted(path for path in ROOT.rglob("*") if path.is_file())
    for path in all_files:
        stat = path.stat()
        kind, tier, site_id = classify(path)
        local_rows.append(
            {
                "relative_path": path.relative_to(ROOT).as_posix(),
                "absolute_path": str(path),
                "file_name": path.name,
                "asset_kind": kind,
                "tier": tier,
                "site_id": site_id,
                "extension": "".join(path.suffixes),
                "size_bytes": stat.st_size,
                "modified_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "sha256": sha256(path),
            }
        )

    local_path = OUT_DIR / "fim_bench_local_manifest.csv"
    write_csv(local_path, local_rows, list(local_rows[0]))

    result = fe.benchFIMquery(download=False)
    if result.get("status") != "ok":
        raise RuntimeError(f"fimeval catalog query failed: {result.get('message')}")
    records = [m.get("record") or {} for m in result.get("matches") or []]
    records = sorted((r for r in records if r.get("tier") in TIERS), key=lambda r: str(r.get("id")))

    urls = sorted({str(r.get(key) or "") for r in records for key in ("tif_url", "gpkg_url") if r.get(key)})
    remote: dict[str, dict[str, object]] = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(head_status, url): url for url in urls}
        for future in as_completed(futures):
            remote[futures[future]] = future.result()

    comparison_rows: list[dict[str, object]] = []
    for rec in records:
        tier = str(rec.get("tier") or "")
        asset_id = str(rec.get("id") or "")
        site_id = str(rec.get("site_id") or "")
        tif_name = str(rec.get("file_name") or "")
        gpkg_url = str(rec.get("gpkg_url") or "")
        gpkg_name = Path(urlparse(gpkg_url).path).name if gpkg_url else ""
        expected_dir = ROOT / "fetch" / tier / site_id
        tif_path = expected_dir / tif_name
        gpkg_path = expected_dir / gpkg_name
        event_tifs = sorted(expected_dir.glob("*_BM.tif")) if expected_dir.is_dir() else []
        event_gpkgs = sorted(expected_dir.glob("*_AOI.gpkg")) if expected_dir.is_dir() else []
        tif_info = remote.get(str(rec.get("tif_url") or ""), {})
        gpkg_info = remote.get(gpkg_url, {})
        remote_tif_available = tif_info.get("status") == 200
        exact_complete = tif_path.is_file() and gpkg_path.is_file()
        if exact_complete:
            local_status = "complete_exact"
        elif tif_path.is_file():
            local_status = "tif_exact_gpkg_missing"
        elif event_tifs:
            local_status = "same_event_filename_mismatch"
        else:
            local_status = "missing"
        actionable_missing = bool(remote_tif_available and not tif_path.is_file())
        comparison_rows.append(
            {
                "asset_id": asset_id,
                "tier": tier,
                "site_id": site_id,
                "catalog_tif_name": tif_name,
                "catalog_gpkg_name": gpkg_name,
                "catalog_resolution_m": rec.get("resolution_m"),
                "tif_url": rec.get("tif_url") or "",
                "tif_http_status": tif_info.get("status"),
                "tif_remote_size_bytes": tif_info.get("content_length"),
                "tif_check_error": tif_info.get("error"),
                "gpkg_url": gpkg_url,
                "gpkg_http_status": gpkg_info.get("status"),
                "gpkg_remote_size_bytes": gpkg_info.get("content_length"),
                "gpkg_check_error": gpkg_info.get("error"),
                "expected_local_tif": str(tif_path),
                "exact_tif_present": tif_path.is_file(),
                "exact_tif_size_bytes": tif_path.stat().st_size if tif_path.is_file() else "",
                "expected_local_gpkg": str(gpkg_path),
                "exact_gpkg_present": gpkg_path.is_file(),
                "exact_gpkg_size_bytes": gpkg_path.stat().st_size if gpkg_path.is_file() else "",
                "same_event_local_tifs": "|".join(p.name for p in event_tifs),
                "same_event_local_gpkgs": "|".join(p.name for p in event_gpkgs),
                "local_status": local_status,
                "actionable_missing_tif": actionable_missing,
            }
        )

    comparison_path = OUT_DIR / "fim_bench_fimeval_comparison.csv"
    write_csv(comparison_path, comparison_rows, list(comparison_rows[0]))

    tier_summary: dict[str, dict[str, int]] = {}
    for tier in sorted(TIERS):
        rows = [r for r in comparison_rows if r["tier"] == tier]
        tier_summary[tier] = {
            "catalog_records": len(rows),
            "remote_tifs_available_http_200": sum(r["tif_http_status"] == 200 for r in rows),
            "remote_tifs_unavailable": sum(r["tif_http_status"] != 200 for r in rows),
            "local_exact_tifs": sum(bool(r["exact_tif_present"]) for r in rows),
            "local_complete_exact_pairs": sum(r["local_status"] == "complete_exact" for r in rows),
            "same_event_filename_mismatches": sum(r["local_status"] == "same_event_filename_mismatch" for r in rows),
            "actionable_missing_tifs": sum(bool(r["actionable_missing_tif"]) for r in rows),
        }
    summary = {
        "generated_at_utc": generated_at,
        "local_root": str(ROOT),
        "local_file_count": len(local_rows),
        "local_total_size_bytes": sum(int(r["size_bytes"]) for r in local_rows),
        "catalog_source": "fimeval.benchFIMquery(download=False)",
        "comparison_scope": ["Tier_1", "Tier_2"],
        "comparison_record_count": len(comparison_rows),
        "availability_rule": "TIFF HEAD status == 200, matching fetch_all.py",
        "tier_summary": tier_summary,
        "actionable_missing": [
            {
                "asset_id": r["asset_id"],
                "tier": r["tier"],
                "catalog_tif_name": r["catalog_tif_name"],
                "tif_http_status": r["tif_http_status"],
                "local_status": r["local_status"],
                "same_event_local_tifs": r["same_event_local_tifs"],
            }
            for r in comparison_rows
            if r["actionable_missing_tif"]
        ],
    }
    summary_path = OUT_DIR / "fim_bench_manifest_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
