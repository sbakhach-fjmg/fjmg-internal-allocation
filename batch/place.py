"""Rank every transfer candidate's stores with the app's own code (app/analysis/cohorts.py, unchanged)."""
from __future__ import annotations
import sqlite3

import pandas as pd

from app.analysis import cohorts as co
from app.config import store_name
from app.db import SCHEMA
from app.decode.marketcheck import SPEC_COLS

# Output of batch/sql/sales.sql; the subset of the app's `deals` table the ranking reads.
DEAL_COLS = ["dealer_code", "deal_number", "vin", "sale_type", "sold_date", "year", "make", "model", "mileage",
             "front_gross", "back_gross", "total_gross", "days_to_sell", "sold_price"]

CANDIDATE_COLS = ["vin", "stock_no", "current_store", "current_store_name", "days_in_stock", "receive_date", "mileage",
                  "year", "make", "model", "dealer_cost"]
DECODE_COLS = ["trim", "version", "mfr_code", "engine", "ext_color", "int_color"]
POOL_COLS = ["status", "match_level", "exact", "thin", "pool_n", "stores_in_pool", "relaxed_fields", "top_store", "action",
             "current_store_rank"]
STORE_COLS = ["store", "store_name", "rank", "is_current_store", "n", "total_hat", "front_hat", "back_hat", "sum_total",
              "sum_front", "days_hat", "days", "annual", "similar", "why"]
PLACEMENT_COLS = CANDIDATE_COLS + DECODE_COLS + POOL_COLS + STORE_COLS
COMPARABLE_COLS = ["vin", "deal_store", "deal_store_name", "deal_vin", "sold_date", "year", "version", "mileage",
                   "front_gross", "back_gross", "total_gross", "days_to_sell", "sold_price"]


def sales_frame(deals: pd.DataFrame, specs: pd.DataFrame, analysis_months: int) -> pd.DataFrame:
    """The app's analysis frame. Deals and decodes go into an in-memory copy of the app's SQLite schema and through
    cohorts.load_frame, so the window, canonical fields and option keys are derived exactly as the app derives them."""
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA)
    d = deals[DEAL_COLS].copy()
    d["sold_date"] = pd.to_datetime(d["sold_date"]).dt.strftime("%Y-%m-%d")
    d.to_sql("deals", con, if_exists="append", index=False)
    specs[SPEC_COLS].to_sql("vin_specs", con, if_exists="append", index=False)
    co.ANALYSIS_MONTHS = analysis_months
    return co.load_frame(con)


def _clean(rec: dict) -> dict:
    """pandas NaN/NaT -> None; the app's code tests `is None`."""
    return {k: (None if not isinstance(v, (list, dict)) and pd.isna(v) else v) for k, v in rec.items()}


def place(frame: pd.DataFrame, specs: pd.DataFrame, candidates: pd.DataFrame, logic: co.Logic) -> tuple:
    """(placements, comparable_deals): one row per candidate × store in its comparable pool, and one per
    candidate × deal in that pool. Candidates without a decode or without comparable deals get one status row."""
    spec_by_vin = {r["vin"]: _clean(r) for r in specs.to_dict("records")}
    rows, comps = [], []
    for c in candidates.to_dict("records"):
        c = _clean(c)
        cur = c["dealer_code"]
        base = {**{k: c.get(k) for k in CANDIDATE_COLS}, "current_store": cur, "current_store_name": store_name(cur)}
        spec = spec_by_vin.get(c["vin"])
        if not spec or spec.get("error"):
            rows.append({**base, "status": "no decode"})
            continue
        base.update({k: spec.get(k) for k in DECODE_COLS})
        mileage = int(c["mileage"]) if c.get("mileage") is not None else None
        veh = co.vehicle_from_spec(spec, mileage=mileage)
        rec = co.rank_stores(frame, veh, logic) if not frame.empty else None
        if rec is None:
            rows.append({**base, "status": "no comparables"})
            continue
        table = rec["table"].to_dict("records")
        ranks = {r["store"]: r["rank"] for r in table}
        top = next((r["store"] for r in table if r["rank"] == 1), None)
        pool = {"status": "placed", "match_level": rec["level"], "exact": rec["exact"], "thin": rec["thin"],
                "pool_n": rec["n"], "stores_in_pool": rec["stores_in_pool"], "relaxed_fields": ", ".join(rec["relaxed"]),
                "top_store": top, "current_store_rank": ranks.get(cur),
                "action": "No ranked store" if top is None else "Keep at current store" if top == cur else f"Transfer to {top}"}
        for r in table:
            rows.append({**base, **pool, **{k: r.get(k) for k in STORE_COLS}, "is_current_store": r["store"] == cur})
        for d in rec["deals"].to_dict("records"):
            comps.append({"vin": c["vin"], "deal_store": d["dealer_code"], "deal_store_name": d["store_name"],
                          "deal_vin": d["vin"], **{k: d.get(k) for k in COMPARABLE_COLS[4:]}})
    placements = pd.DataFrame(rows, columns=PLACEMENT_COLS)
    for col in ("pool_n", "stores_in_pool", "current_store_rank", "rank", "n"):   # counts and ranks, null on status rows
        placements[col] = placements[col].astype("Int64")
    return placements, pd.DataFrame(comps, columns=COMPARABLE_COLS)
