"""Collect AOI metadata and raster coverage polygons into one GeoPackage.

Default run:
    conda run -n fimbench python collect_vectors.py
"""

from __future__ import annotations

import argparse, json, logging, math, sqlite3, subprocess, sys, time, traceback
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import rasterio
from rasterio.enums import Resampling
from rasterio.features import shapes
from shapely.geometry import shape
from shapely.ops import unary_union
from shapely import wkb
from tqdm.auto import tqdm


DEFAULT_SEARCH_DIR = "/home/cefect/LS/10_IO/2501_NSFc/FIM_Bench/fetch"
DEFAULT_OUT_NAME = "collect_vectors.gpkg"
DEFAULT_OUT_FP = "/home/cefect/LS/09_REPOS/05_FORKS/fimbench/collect_vectors.gpkg"
DEFAULT_TIMEOUT_SEC = 120
DEFAULT_MASK_RESOLUTION_M = 100.0


def get_logger(
    log_fp: Path,
    *,
    level: int = logging.INFO,
    logger_name: str = "collect_vectors",
) -> logging.Logger:
    """Create a stream + file logger for collection runs."""
    assert isinstance(log_fp, Path), "log_fp must be a pathlib.Path."
    assert isinstance(level, int), "level must be an int."
    assert isinstance(logger_name, str) and logger_name.strip(), "logger_name must be a non-empty string."
    log_fp.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if logger.handlers:
        logger.handlers.clear()
    formatter_file = logging.Formatter("%(levelname)s-%(asctime)s-%(name)s: %(message)s", datefmt="%H:%M:%S")
    formatter_stream = logging.Formatter("[%(levelname)s]%(name)s: %(message)s")
    file_handler = logging.FileHandler(log_fp)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter_file)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter_stream)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def _quote_sql_ident(name: str) -> str:
    """Return a safely quoted SQL identifier for OGR SQLite dialect."""
    assert isinstance(name, str) and name, "name must be a non-empty string."
    return "\"" + name.replace("\"", "\"\"") + "\""


def _read_single_record_ogr(gpkg_fp: Path) -> dict:
    """Read one feature row from a GPKG using ogrinfo without geometry."""
    assert isinstance(gpkg_fp, Path) and gpkg_fp.exists(), f"Missing GeoPackage: {gpkg_fp}"
    meta_proc = subprocess.run(
        ["ogrinfo", "-ro", "-q", "-json", "-so", str(gpkg_fp)],
        check=True,
        capture_output=True,
        text=True,
    )
    layer_l = (json.loads(meta_proc.stdout) or {}).get("layers") or []
    assert len(layer_l) == 1, f"Expected one layer in {gpkg_fp}, found {len(layer_l)}."
    layer_name = layer_l[0].get("name")
    field_l = [d.get("name") for d in (layer_l[0].get("fields") or []) if d.get("name")]
    assert layer_name and field_l, f"Could not resolve layer fields for {gpkg_fp}"
    field_sql = ", ".join(_quote_sql_ident(col_name) for col_name in field_l)
    query = f"SELECT {field_sql} FROM {_quote_sql_ident(layer_name)} LIMIT 2"
    data_proc = subprocess.run(
        ["ogrinfo", "-ro", "-q", "-json", "-geom=NO", "-dialect", "SQLITE", "-sql", query, str(gpkg_fp)],
        check=True,
        capture_output=True,
        text=True,
    )
    qry_layer_l = (json.loads(data_proc.stdout) or {}).get("layers") or []
    assert qry_layer_l, f"ogrinfo query returned no layers for {gpkg_fp}"
    feature_l = qry_layer_l[0].get("features") or []
    assert len(feature_l) == 1, f"Expected one feature in {gpkg_fp}, found {len(feature_l)}."
    return feature_l[0].get("properties") or {}


def _read_single_record_sqlite(gpkg_fp: Path) -> dict:
    """Read one record from a GPKG table using read-only SQLite access."""
    assert isinstance(gpkg_fp, Path) and gpkg_fp.exists(), f"Missing GeoPackage: {gpkg_fp}"
    con = sqlite3.connect(f"file:{gpkg_fp}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        layer_l = cur.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features'").fetchall()
        assert len(layer_l) == 1, f"Expected one feature layer in {gpkg_fp}, found {len(layer_l)}."
        layer_name = layer_l[0][0]
        assert isinstance(layer_name, str) and layer_name, f"Could not resolve feature layer in {gpkg_fp}."
        layer_name_safe = layer_name.replace("'", "''")
        field_l = [
            row[1]
            for row in cur.execute(f"PRAGMA table_info('{layer_name_safe}')").fetchall()
            if isinstance(row[1], str) and row[1] and row[1].lower() not in {"geom", "geometry", "fid", "ogc_fid"}
        ]
        assert field_l, f"Could not resolve attribute fields for {gpkg_fp}"
        field_sql = ", ".join(_quote_sql_ident(col_name) for col_name in field_l)
        row_l = cur.execute(f"SELECT {field_sql} FROM '{layer_name_safe}' LIMIT 2").fetchall()
        assert len(row_l) == 1, f"Expected one feature in {gpkg_fp}, found {len(row_l)}."
        return dict(zip(field_l, row_l[0]))
    finally:
        con.close()


def _read_single_record(gpkg_fp: Path) -> dict:
    """Read one record from a GPKG without geometry, with conservative fallbacks."""
    assert isinstance(gpkg_fp, Path) and gpkg_fp.exists(), f"Missing GeoPackage: {gpkg_fp}"
    try:
        return _read_single_record_sqlite(gpkg_fp)
    except Exception:
        pass
    try:
        src_df = pyogrio.read_dataframe(gpkg_fp, read_geometry=False, max_features=2)
        assert len(src_df) == 1, f"Expected one feature in {gpkg_fp}, found {len(src_df)}."
        return src_df.iloc[0].to_dict()
    except Exception:
        pass
    try:
        src_df = gpd.read_file(gpkg_fp, rows=1, ignore_geometry=True)
        assert len(src_df) == 1, f"Expected one feature in {gpkg_fp}, found {len(src_df)}."
        return src_df.iloc[0].to_dict()
    except Exception:
        return _read_single_record_ogr(gpkg_fp)


def _collect_single_record(gpkg_fp: Path, search_path: Path) -> tuple[dict, str | None]:
    """Build one output record from one source GeoPackage + adjacent raster."""
    assert isinstance(gpkg_fp, Path) and gpkg_fp.exists(), f"Missing GeoPackage: {gpkg_fp}"
    assert isinstance(search_path, Path) and search_path.exists(), f"Missing search directory: {search_path}"
    rel_parts = gpkg_fp.relative_to(search_path).parts
    assert len(rel_parts) >= 3, f"Expected <tier>/<case>/<file.gpkg>; got: {gpkg_fp}"
    tier, case_name = rel_parts[0], rel_parts[1]
    tier_digits = "".join(ch for ch in tier if ch.isdigit())
    assert tier_digits, f"Could not parse integer tier from: {tier}"

    # Read one non-geometry source record for metadata columns.
    rec = _read_single_record(gpkg_fp)
    for col_name in [k for k in list(rec) if isinstance(k, str) and k.lower() in {"fid", "ogc_fid"}]:
        rec.pop(col_name, None)
    for col_name in ["Datatype", "Compression Type", "Extent"]:
        rec.pop(col_name, None)

    # Resolve adjacent raster, preferring the AOI->BM naming convention.
    tif_fp = gpkg_fp.with_name(gpkg_fp.name.replace("_AOI.gpkg", "_BM.tif"))
    if not tif_fp.exists():
        tif_l = sorted(gpkg_fp.parent.glob("*.tif"))
        assert len(tif_l) == 1, f"Could not uniquely resolve .tif for {gpkg_fp}."
        tif_fp = tif_l[0]
    assert tif_fp.exists(), f"Missing adjacent raster for {gpkg_fp}"

    # Resample raster mask to ~100m (nearest) before polygonization.
    with rasterio.open(tif_fp) as ds:
        x_res = abs(float(ds.transform.a))
        y_res = abs(float(ds.transform.e))
        if ds.crs and ds.crs.is_geographic:
            lat_mid = (float(ds.bounds.bottom) + float(ds.bounds.top)) / 2.0
            x_m_per_degree = max(1.0, 111_320.0 * max(0.01, abs(math.cos(math.radians(lat_mid)))))
            x_res_m = x_res * x_m_per_degree
            y_res_m = y_res * 111_320.0
        else:
            x_res_m, y_res_m = x_res, y_res
        x_scale = max(1.0, DEFAULT_MASK_RESOLUTION_M / max(x_res_m, 1e-12))
        y_scale = max(1.0, DEFAULT_MASK_RESOLUTION_M / max(y_res_m, 1e-12))
        out_width = max(1, int(round(int(ds.width) / x_scale)))
        out_height = max(1, int(round(int(ds.height) / y_scale)))
        out_transform = ds.transform * ds.transform.scale(int(ds.width) / out_width, int(ds.height) / out_height)
        valid_a = ds.read_masks(1, out_shape=(out_height, out_width), resampling=Resampling.nearest) > 0
        assert valid_a.any(), f"No unmasked raster pixels found in {tif_fp}"
        geom_l = [
            shape(geojson_d)
            for geojson_d, val in shapes(valid_a.astype("uint8"), mask=valid_a, transform=out_transform)
            if int(val) == 1
        ]
        assert geom_l, f"Polygonization produced no features for {tif_fp}"
        rec["raster_bbox"] = (
            float(ds.bounds.left),
            float(ds.bounds.bottom),
            float(ds.bounds.right),
            float(ds.bounds.top),
        )
        rec["raster_shape"] = (int(ds.height), int(ds.width))
        rec["mask_shape"] = (int(out_height), int(out_width))
        rec["mask_resolution_m"] = float(DEFAULT_MASK_RESOLUTION_M)
        out_crs_wkt = ds.crs.to_wkt() if ds.crs else None

    # Add requested path-derived metadata and polygonized geometry.
    rec["tier"] = int(tier_digits)
    rec["case_name"] = case_name
    rec["gpkg_fp"] = str(gpkg_fp)
    rec["tif_fp"] = str(tif_fp)
    date_a = rec.get("Date of the Flooding Event")
    date_b = rec.get("Flooding Event")
    if pd.notna(date_a):
        date_s = str(date_a).strip()
        if date_s.endswith(".0"):
            date_s = date_s[:-2]
        rec["datetime"] = pd.to_datetime(date_s, format="%Y%m%d", errors="coerce")
    elif pd.notna(date_b):
        rec["datetime"] = pd.to_datetime(str(date_b).strip(), format="%Y%m%dT%H%M%S", errors="coerce")
    else:
        rec["datetime"] = pd.NaT
    rec["geometry"] = unary_union(geom_l)
    return rec, out_crs_wkt


def _collect_single_record_worker(gpkg_fp: str, search_path: str) -> dict:
    """Run single-record collection in worker mode and return a JSON-safe payload."""
    assert isinstance(gpkg_fp, str) and gpkg_fp, "gpkg_fp must be a non-empty string."
    assert isinstance(search_path, str) and search_path, "search_path must be a non-empty string."
    try:
        rec, out_crs_wkt = _collect_single_record(Path(gpkg_fp), Path(search_path))
        if "datetime" in rec:
            rec["datetime"] = rec["datetime"].isoformat() if pd.notna(rec["datetime"]) else None
        geom = rec.pop("geometry")
        return {
            "status": "ok",
            "record": rec,
            "geometry_wkb": geom.wkb_hex if geom else None,
            "out_crs_wkt": out_crs_wkt,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc), "traceback": traceback.format_exc()}


def _run_single_record_with_timeout(gpkg_fp: Path, search_path: Path, timeout_sec: int) -> dict:
    """Execute one file in a subprocess and stop it if it exceeds timeout."""
    assert isinstance(gpkg_fp, Path) and gpkg_fp.exists(), f"Missing GeoPackage: {gpkg_fp}"
    assert isinstance(search_path, Path) and search_path.exists(), f"Missing search directory: {search_path}"
    assert isinstance(timeout_sec, int) and timeout_sec > 0, "timeout_sec must be a positive int."
    worker_cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker-gpkg-fp",
        str(gpkg_fp),
        "--worker-search-dir",
        str(search_path),
    ]
    try:
        proc = subprocess.run(
            worker_cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "error": f"Exceeded {timeout_sec} seconds."}

    if proc.returncode != 0:
        result = {
            "status": "error",
            "error": f"Worker exited with code {proc.returncode}.",
            "traceback": (proc.stderr or "").strip(),
        }
        return result
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return {"status": "error", "error": "Worker returned empty stdout.", "traceback": (proc.stderr or "").strip()}
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        return {"status": "error", "error": "Worker returned invalid JSON payload.", "traceback": stdout}
    if result.get("status") == "ok":
        rec = result["record"]
        rec["geometry"] = wkb.loads(bytes.fromhex(result["geometry_wkb"])) if result.get("geometry_wkb") else None
        result["record"] = rec
    return result


def main_collect_vectors(
    *,
    search_dir: str = DEFAULT_SEARCH_DIR,
    out_fp: str | None = None,
    resume: bool = True,
    skip_broken: bool = True,
    verbose: bool = True,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    log_fp: str | None = None,
    logger: logging.Logger | None = None,
) -> dict:
    """Build one record per GPKG using raster unmasked area as geometry.

    Parameters
    ----------
    search_dir:
        Root directory scanned recursively for ``*.gpkg`` files.
    out_fp:
        Optional output path. Defaults to ``<cwd>/collect_vectors.gpkg``.
    resume:
        If ``True``, skip source files already present in an existing output.
    skip_broken:
        If ``True``, skip unreadable/corrupt source GeoPackages instead of failing.
    verbose:
        If ``True``, print progress logs to stdout.
    timeout_sec:
        Per-file processing timeout in seconds; files exceeding this are skipped.
    log_fp:
        Optional log file path. Defaults to ``<out_fp>.log``.
    logger:
        Optional preconfigured logger. If ``None``, a stream+file logger is created.

    Returns
    -------
    dict
        Summary metadata for the collection run.
    """
    assert isinstance(search_dir, str) and search_dir.strip(), "search_dir must be a non-empty string."
    assert out_fp is None or (isinstance(out_fp, str) and out_fp.strip()), "out_fp must be None or a non-empty string."
    assert isinstance(resume, bool), "resume must be a bool."
    assert isinstance(skip_broken, bool), "skip_broken must be a bool."
    assert isinstance(verbose, bool), "verbose must be a bool."
    assert isinstance(timeout_sec, int) and timeout_sec > 0, "timeout_sec must be a positive int."
    assert log_fp is None or (isinstance(log_fp, str) and log_fp.strip()), "log_fp must be None or a non-empty string."
    assert logger is None or isinstance(logger, logging.Logger), "logger must be None or logging.Logger."
    t0 = time.perf_counter()
    search_path = Path(search_dir).expanduser().resolve()
    assert search_path.exists() and search_path.is_dir(), f"search_dir does not exist: {search_path}"
    out_path = Path(out_fp).expanduser().resolve() if out_fp else Path(DEFAULT_OUT_FP).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = Path(log_fp).expanduser().resolve() if log_fp else out_path.with_suffix(".log")
    log = logger or get_logger(log_path, level=logging.INFO if verbose else logging.WARNING)
    log.info(f"Found source directory \n    {search_path}")
    log.info(f"Writing output to \n    {out_path}")
    log.info(f"Writing logs to \n    {log_path}")
    log.debug(f"Parameters: search_dir={search_dir}, out_fp={out_fp}, resume={resume}, skip_broken={skip_broken}, timeout_sec={timeout_sec}, verbose={verbose}")

    # Find all source GeoPackages once and fail early if empty.
    gpkg_l = sorted(search_path.rglob("*.gpkg"))
    assert gpkg_l, f"No .gpkg files found under {search_path}"
    log.info(f"Discovered {len(gpkg_l):,} GeoPackage files.")

    # Preload already-collected sources when resuming from an existing output.
    processed_fp_s = set()
    if resume and out_path.exists():
        try:
            existing_df = gpd.read_file(out_path, columns=["gpkg_fp"], ignore_geometry=True)
            processed_fp_s = set(existing_df["gpkg_fp"].dropna().astype(str))
            log.info(f"Resume mode enabled with {len(processed_fp_s):,} prior rows.")
        except Exception as exc:
            log.warning(f"Could not load existing output for resume ({exc!r}); rebuilding.")

    # Build one output record from each GPKG + adjacent raster pair.
    record_l = []
    skipped_existing = 0
    skipped_broken_count = 0
    skipped_broken_l = []
    skipped_timeout_count = 0
    skipped_timeout_l = []
    out_crs = None
    for idx, gpkg_fp in enumerate(tqdm(gpkg_l, desc="Collect vectors", unit="gpkg", disable=not verbose), start=1):
        log.debug(f"Processing {idx:,}/{len(gpkg_l):,} \n    {gpkg_fp}")
        gpkg_str = str(gpkg_fp)
        if gpkg_str in processed_fp_s:
            skipped_existing += 1
            continue
        worker_result = _run_single_record_with_timeout(gpkg_fp, search_path, timeout_sec)
        if worker_result["status"] == "timeout":
            skipped_timeout_count += 1
            skipped_timeout_l.append({"gpkg_fp": gpkg_str, "error": worker_result["error"]})
            log.warning(f"Skipping timed-out file after {timeout_sec}s \n    {gpkg_fp}")
            continue
        if worker_result["status"] == "error":
            if not skip_broken:
                raise RuntimeError(f"Failed processing {gpkg_fp}: {worker_result['error']}\n{worker_result.get('traceback', '')}")
            skipped_broken_count += 1
            skipped_broken_l.append({"gpkg_fp": gpkg_str, "error": worker_result["error"]})
            log.warning(f"Skipping broken file \n    {gpkg_fp}\n    error={worker_result['error']}")
            continue
        rec = worker_result["record"]
        rec_crs = rasterio.crs.CRS.from_wkt(worker_result["out_crs_wkt"]) if worker_result.get("out_crs_wkt") else None
        if out_crs is None:
            out_crs = rec_crs
        elif rec_crs and rec_crs != out_crs:
            raise AssertionError(f"Mixed raster CRS detected: {rec_crs} vs {out_crs}")
        record_l.append(rec)

    # Assemble output and keep previous rows when resume mode is enabled.
    if not record_l and out_path.exists():
        out_gdf = gpd.read_file(out_path)
    elif out_path.exists() and skipped_existing > 0:
        existing_gdf = gpd.read_file(out_path)
        new_gdf = gpd.GeoDataFrame(record_l, geometry="geometry", crs=out_crs or existing_gdf.crs)
        if existing_gdf.crs and new_gdf.crs and existing_gdf.crs != new_gdf.crs:
            raise AssertionError(f"Mixed output CRS detected: {existing_gdf.crs} vs {new_gdf.crs}")
        out_gdf = gpd.GeoDataFrame(pd.concat([existing_gdf, new_gdf], ignore_index=True), geometry="geometry", crs=existing_gdf.crs or new_gdf.crs)
        out_gdf.to_file(out_path, driver="GPKG")
    else:
        assert record_l, "No readable records collected and no existing output found."
        out_gdf = gpd.GeoDataFrame(record_l, geometry="geometry", crs=out_crs)
        out_gdf.to_file(out_path, driver="GPKG")

    # Harmonize output schema and dtypes for downstream use.
    out_gdf = out_gdf.drop(columns=[c for c in ["Datatype", "Compression Type", "Extent"] if c in out_gdf.columns], errors="ignore")
    if "tier" in out_gdf.columns:
        tier_s = out_gdf["tier"].map(
            lambda v: "".join(ch for ch in str(v) if ch.isdigit()) if pd.notna(v) else pd.NA
        )
        out_gdf["tier"] = pd.to_numeric(tier_s.mask(tier_s == ""), errors="coerce").astype("Int64")
    date_a_s = (
        out_gdf["Date of the Flooding Event"]
        if "Date of the Flooding Event" in out_gdf.columns
        else pd.Series(pd.NA, index=out_gdf.index)
    )
    date_b_s = (
        out_gdf["Flooding Event"]
        if "Flooding Event" in out_gdf.columns
        else pd.Series(pd.NA, index=out_gdf.index)
    )
    case_s = out_gdf["case_name"] if "case_name" in out_gdf.columns else pd.Series(pd.NA, index=out_gdf.index)
    date_a_s = date_a_s.map(
        lambda v: str(v).strip()[:-2]
        if pd.notna(v) and str(v).strip().endswith(".0")
        else (str(v).strip() if pd.notna(v) else pd.NA)
    )
    date_b_s = date_b_s.map(lambda v: str(v).strip() if pd.notna(v) else pd.NA)
    case_dt_tok_s = case_s.astype("string").str.extract(r"_(\d{8}T\d{6})_", expand=False)
    case_d_tok_s = case_s.astype("string").str.extract(r"_(\d{8})_", expand=False)
    date_a_dt = pd.to_datetime(date_a_s, format="%Y%m%d", errors="coerce")
    date_b_dt = pd.to_datetime(date_b_s, format="%Y%m%dT%H%M%S", errors="coerce")
    case_dt_dt = pd.to_datetime(case_dt_tok_s, format="%Y%m%dT%H%M%S", errors="coerce")
    case_d_dt = pd.to_datetime(case_d_tok_s, format="%Y%m%d", errors="coerce")
    base_dt = pd.to_datetime(out_gdf["datetime"], errors="coerce") if "datetime" in out_gdf.columns else pd.Series(pd.NaT, index=out_gdf.index)
    out_gdf["datetime"] = base_dt.fillna(date_a_dt).fillna(date_b_dt).fillna(case_dt_dt).fillna(case_d_dt)
    out_gdf.to_file(out_path, driver="GPKG")

    # Return a compact status summary for CLI runs.
    elapsed = time.perf_counter() - t0
    summary = {
        "status": "ok",
        "message": f"Collected {len(record_l):,} new records in {elapsed:,.2f} seconds.",
        "search_dir": str(search_path),
        "output_fp": str(out_path),
        "discovered_gpkg_count": len(gpkg_l),
        "new_record_count": len(record_l),
        "skipped_existing_count": skipped_existing,
        "skipped_timeout_count": skipped_timeout_count,
        "skipped_broken_count": skipped_broken_count,
        "record_count": len(out_gdf),
        "columns": list(out_gdf.columns),
        "log_fp": str(log_path),
        "timeout_sec": timeout_sec,
        "skipped_timeout": skipped_timeout_l[:10],
        "skipped_broken": skipped_broken_l[:10],
    }
    log.info(f"Finished with {len(record_l):,} new records in {elapsed:,.2f}s (timeouts={skipped_timeout_count}, broken={skipped_broken_count}, existing={skipped_existing}).")
    log.debug(f"Run summary: {json.dumps(summary, indent=2)}")
    return summary


def _parse_arguments() -> tuple[tuple, dict]:
    """Parse CLI arguments for ``main_collect_vectors``."""
    parser = argparse.ArgumentParser(
        description="Collect AOI attributes and raster coverage polygons into one GeoPackage."
    )
    parser.add_argument(
        "--search-dir",
        default=DEFAULT_SEARCH_DIR,
        help="Root directory scanned recursively for .gpkg files.",
    )
    parser.add_argument(
        "--out-fp",
        default=str(Path(DEFAULT_OUT_FP).expanduser().resolve()),
        help=f"Output file path. Defaults to {DEFAULT_OUT_FP}.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Disable verbose progress messages.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore existing output and rebuild from scratch.",
    )
    parser.add_argument(
        "--fail-on-broken",
        action="store_true",
        help="Raise on unreadable/corrupt source GeoPackages instead of skipping.",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help="Per-file timeout in seconds before skipping a .gpkg.",
    )
    parser.add_argument(
        "--log-fp",
        default=None,
        help="Optional log file path. Defaults to <out-fp>.log.",
    )
    parser.add_argument(
        "--worker-gpkg-fp",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--worker-search-dir",
        default=None,
        help=argparse.SUPPRESS,
    )
    ns = parser.parse_args()
    return tuple(), {
        "search_dir": ns.search_dir,
        "out_fp": ns.out_fp,
        "resume": not ns.no_resume,
        "skip_broken": not ns.fail_on_broken,
        "verbose": not ns.quiet,
        "timeout_sec": ns.timeout_sec,
        "log_fp": ns.log_fp,
        "worker_gpkg_fp": ns.worker_gpkg_fp,
        "worker_search_dir": ns.worker_search_dir,
    }


if __name__ == "__main__":
    args, kwargs = _parse_arguments()
    worker_gpkg_fp = kwargs.pop("worker_gpkg_fp")
    worker_search_dir = kwargs.pop("worker_search_dir")
    if worker_gpkg_fp or worker_search_dir:
        assert worker_gpkg_fp and worker_search_dir, "Both worker args are required."
        print(json.dumps(_collect_single_record_worker(worker_gpkg_fp, worker_search_dir)))
    else:
        print(json.dumps(main_collect_vectors(*args, **kwargs), indent=2))
