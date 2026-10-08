"""Weekly transfer placement run. See docs/transfer-pipeline-plan.md.

    python -m batch.run                          MSSQL -> decode new VINs -> place -> runs/<date>/ -> Tableau Server
    python -m batch.run --local DIR              read DIR/*.parquet instead of MSSQL (new decodes saved to DIR)
    python -m batch.run --skip-decode --skip-publish
    python -m batch.run [--local DIR] seed-decodes FILE.jsonl.gz   one-time: load an app cache export into vin_specs

Exit code 1 when publishing fails (the local snapshot is still written), 2 on any other failure.
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

import batch  # noqa: F401  (sets DATA_DIR before `app` is imported)

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("batch")


def _source(args):
    from batch.sources import LocalFolder, Mssql
    if args.local:
        return LocalFolder(args.local)
    return Mssql(os.environ["MSSQL_CONNECTION"])


def seed_decodes(args) -> int:
    from batch.decode import specs_from_cache_file
    src = _source(args)
    rows = specs_from_cache_file(args.file)
    have = set(src.read_specs()["vin"])
    rows = rows[~rows["vin"].isin(have)]
    src.save_specs(rows)
    log.info("seeded %d decodes (%d already present)", len(rows), len(have))
    return 0


def run(args) -> int:
    from batch.rule import load_rule
    rule = load_rule(args.rule)
    from batch import decode as dc
    from batch.place import place, sales_frame

    run_date = date.today().isoformat()
    out = Path(args.runs_dir) / run_date
    (out / "inputs").mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(out / "run.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)

    src = _source(args)
    deals = src.read("sales")
    candidates = src.read("transfer_candidates")
    specs = src.read_specs()
    log.info("read %d sales deals, %d transfer candidates, %d decodes", len(deals), len(candidates), len(specs))

    if not args.skip_decode:
        todo = dc.vins_to_decode(list(deals["vin"]) + list(candidates["vin"]), specs)
        new, note = dc.decode_vins(todo, specs, args.decode_cap)
        src.save_specs(new)
        specs = dc.merge_specs(specs, new)
        log.info("decode: %d VINs to decode; %s", len(todo), note)

    used = specs[specs["vin"].isin(set(deals["vin"]) | set(candidates["vin"]))].copy()
    used["raw"] = None   # bulky and unused by the ranking; the column stays so the snapshot replays with --local
    deals.to_parquet(out / "inputs" / "sales.parquet", index=False)
    candidates.to_parquet(out / "inputs" / "transfer_candidates.parquet", index=False)
    used.to_parquet(out / "inputs" / "vin_specs.parquet", index=False)

    frame = sales_frame(deals, specs, rule.analysis_months)
    cov, by_store = dc.coverage(frame, specs)
    log.info("decode coverage of in-window sales: %.1f%%", cov * 100)
    for store, share in by_store.items():
        if share < dc.COVERAGE_WARN:
            log.warning("  %s: %.1f%% decoded", store, share * 100)

    placements, comparables = place(frame, specs, candidates, rule.logic)
    placements.insert(0, "run_date", run_date)
    comparables.insert(0, "run_date", run_date)
    placements["decode_coverage"] = cov
    placements["coverage_warning"] = (f"Decode coverage {cov:.0%}: rankings may be loose" if cov < dc.COVERAGE_WARN else None)
    log.info("placed %d candidates: %s", candidates["vin"].nunique(),
             placements.drop_duplicates("vin")["status"].value_counts().to_dict())

    placements = _tableau_types(placements)
    comparables = _tableau_types(comparables)
    placements.to_parquet(out / "transfer_placements.parquet", index=False)
    comparables.to_parquet(out / "transfer_comparable_deals.parquet", index=False)

    if args.skip_publish:
        return 0
    from batch.publish import publish, write_hyper
    try:
        paths = [out / "transfer_placements.hyper", out / "transfer_comparable_deals.hyper"]
        write_hyper(placements, paths[0])
        write_hyper(comparables, paths[1])
        publish(paths)
        log.info("published %s", ", ".join(p.stem for p in paths))
    except Exception:
        log.exception("publishing to Tableau Server failed; outputs are in %s", out)
        return 1
    return 0


def _tableau_types(df):
    """Give columns that hold None next to numbers/booleans a nullable dtype, so the .hyper columns get real types."""
    import pandas as pd
    df = df.copy()
    for c in df.columns:
        if df[c].dtype != object:
            continue
        s = df[c].dropna()
        if s.empty:
            df[c] = df[c].astype("string")
        elif s.map(lambda v: isinstance(v, bool)).all():
            df[c] = df[c].astype("boolean")
        elif s.map(lambda v: isinstance(v, int) and not isinstance(v, bool)).all():
            df[c] = df[c].astype("Int64")
        elif s.map(lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)).all():
            df[c] = df[c].astype("Float64")
        else:
            df[c] = df[c].astype("string")
    for c in ("sold_date", "receive_date"):
        if c in df:
            df[c] = pd.to_datetime(df[c])
    return df


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m batch.run")
    p.add_argument("--local", type=Path, help="read parquet files from this folder instead of MSSQL")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("seed-decodes")
    s.add_argument("file", type=Path)
    p.add_argument("--rule", type=Path, default=ROOT / "batch" / "ranking_rule.yaml")
    p.add_argument("--runs-dir", type=Path, default=ROOT / "runs")
    p.add_argument("--decode-cap", type=int, default=12000)
    p.add_argument("--skip-decode", action="store_true")
    p.add_argument("--skip-publish", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return seed_decodes(args) if args.cmd == "seed-decodes" else run(args)
    except Exception:
        log.exception("run failed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
