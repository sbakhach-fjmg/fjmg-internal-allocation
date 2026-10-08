"""The decode table: seed it from a cache export, top up VINs it lacks, and measure how much of the sales history is decoded."""
from __future__ import annotations
import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from app.config import MARKETCHECK_WORKERS
from app.decode import marketcheck as mc

MAX_ATTEMPTS = 3          # a VIN that fails this many decodes stops being retried and stops counting against coverage
COVERAGE_WARN = 0.98      # below this share of decoded sales deals, the run is flagged on the dashboard
SPEC_TABLE_COLS = mc.SPEC_COLS + ["decode_attempts"]


def specs_from_cache_file(path: Path) -> pd.DataFrame:
    """Rows from an app cache export (.jsonl.gz). Older exports lack packages / mfr_code / options; they are
    filled from the raw MarketCheck payload the same way app/db.py backfills them."""
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("error") or len(rec.get("vin") or "") != 17:
                continue
            raw = json.loads(rec["raw"]) if rec.get("raw") else {}
            if rec.get("packages") is None:
                rec["packages"] = json.dumps(mc.extract_packages(raw))
            if rec.get("mfr_code") is None:
                rec["mfr_code"] = raw.get("manufacturer_code")
            if rec.get("options") is None:
                rec["options"] = json.dumps(mc.extract_options(raw))
            rec["vin"] = rec["vin"].upper()
            rec["decode_attempts"] = 1
            rows.append({c: rec.get(c) for c in SPEC_TABLE_COLS})
    return pd.DataFrame(rows, columns=SPEC_TABLE_COLS)


def vins_to_decode(vins, specs: pd.DataFrame) -> list:
    """VINs never decoded, plus failed ones that still have attempts left."""
    known = {r["vin"]: r for r in specs[["vin", "error", "decode_attempts"]].to_dict("records")}
    out = []
    for v in sorted(set(vins)):
        r = known.get(v)
        if r is None or (r["error"] is not None and not pd.isna(r["error"]) and (r["decode_attempts"] or 0) < MAX_ATTEMPTS):
            out.append(v)
    return out


def decode_vins(vins: list, specs: pd.DataFrame, cap: int) -> tuple:
    """(new rows, note). Decodes up to `cap` VINs. Stops at the MarketCheck quota; quota errors are not stored,
    so those VINs are tried again next run."""
    if not vins:
        return pd.DataFrame(columns=SPEC_TABLE_COLS), "nothing to decode"
    if not mc.enabled():
        return pd.DataFrame(columns=SPEC_TABLE_COLS), "MarketCheck disabled (no MARKETCHECK_API_KEY, or quota exhausted)"
    attempts = dict(zip(specs["vin"], specs["decode_attempts"]))
    todo = vins[:cap]

    def one(vin):
        if mc.quota_blocked():
            return None
        d = mc.decode_neovin(vin)
        if d.get("_quota") or str(d.get("_error", "")).startswith("marketcheck disabled"):
            return None   # quota hit (here or in another worker): not a real attempt, retry next run
        rec = mc.normalize(vin, d)
        rec["decode_attempts"] = int(attempts.get(vin) or 0) + 1
        return rec

    with ThreadPoolExecutor(max_workers=MARKETCHECK_WORKERS) as ex:
        recs = [r for r in ex.map(one, todo) if r is not None]
    rows = pd.DataFrame([{c: r.get(c) for c in SPEC_TABLE_COLS} for r in recs], columns=SPEC_TABLE_COLS)
    note = f"decoded {len(rows)} of {len(todo)} ({int(rows['error'].isna().sum())} ok)"
    if len(vins) > cap:
        note += f"; {len(vins) - cap} left for the next run (cap {cap})"
    if mc.quota_blocked():
        note += "; stopped: MarketCheck quota exhausted"
    return rows, note


def merge_specs(specs: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    if new.empty:
        return specs
    if specs.empty:
        return new
    return pd.concat([specs[~specs["vin"].isin(new["vin"])], new], ignore_index=True)


def coverage(frame: pd.DataFrame, specs: pd.DataFrame) -> tuple:
    """(share of in-window sales deals with a usable decode, per-store shares). VINs that used up their
    attempts are left out, so a few undecodable VINs do not hold coverage down for good."""
    if frame.empty:
        return 1.0, pd.Series(dtype=float)
    gave_up = set(specs.loc[specs["error"].notna() & (specs["decode_attempts"] >= MAX_ATTEMPTS), "vin"])
    f = frame[~frame["vin"].isin(gave_up)]
    if f.empty:
        return 1.0, pd.Series(dtype=float)
    return float(f["decoded"].mean()), f.groupby("dealer_code")["decoded"].mean()
