"""Cohort x store profitability analysis and the placement recommender. Retail + lease deals only.

A *cohort* is a group of used retail deals that share taxonomy keys at some level, from very specific
(year · make · model · trim · mileage band · spec) down to broad (make). For each cohort we score
every store that has sold that kind of car:

  total_hat / front_hat / back_hat
              avg deal gross at the store, shrunk toward the cohort mean so a store with 3 deals cannot
              out-rank one with 30 on luck alone:  (n*mean + K*cohort_mean) / (n + K)
  days_hat    avg days receive -> sold, shrunk the same way
  annual      total_hat * 365 / days_hat  — gross one inventory slot earns per year
  similar     how many of the store's units look like the vehicle being placed (attribute overlap)

Which of these decides #1 is the user's choice (the Logic tab). The default is Nick's rule: the store
that has sold the MOST of that car is #1, and another store only takes #1 when its adj total gross
per unit is dramatically higher (≥ over_pct AND ≥ over_abs dollars more, with ≥ min_best sales).

The recommender walks LEVELS from specific to broad and stops at the first level with enough history
(min_cohort deals) and at least one store with min_store deals.
"""
from __future__ import annotations
import json
import math
from dataclasses import dataclass, asdict
from typing import Optional
import pandas as pd
from app.config import (PRIOR_K, MIN_N_STORE, MIN_N_BEST, MIN_N_COHORT, MIN_DAYS, ANALYSIS_MONTHS, GROSS_OVERRIDE_PCT,
                        GROSS_OVERRIDE_ABS, store_name, STORES)
from app.decode.taxonomy import canonical, UNKNOWN, BAND_ORDER

# (label, taxonomy keys that must match exactly, require package overlap?)
LEVELS = [
    ("Year · Model · Trim · Spec · Miles · Options", ["year", "make", "model", "trim", "spec", "mileage_band"], True),
    ("Year · Model · Trim · Spec · Miles", ["year", "make", "model", "trim", "spec", "mileage_band"], False),
    ("Year · Model · Trim · Spec", ["year", "make", "model", "trim", "spec"], False),
    ("Year · Model · Trim", ["year", "make", "model", "trim"], False),
    ("Model · Trim (any year)", ["make", "model", "trim"], False),
    ("Year · Model", ["year", "make", "model"], False),
    ("Model", ["make", "model"], False),
    ("Make · Body", ["make", "body"], False),
    ("Make", ["make"], False),
]
PACKAGE_MATCH_SHARE = 0.5   # a sold unit "matches on options" if it carries at least this share of the incoming car's packages
SPEC_KEYS = ["year", "make", "model", "trim", "version", "body_type", "vehicle_type", "drivetrain", "powertrain_type", "ext_base", "msrp", "error", "packages"]
SIM_ATTRS = ["trim", "drivetrain", "powertrain", "body", "mileage_band", "ext_base", "year"]

# key -> (label, one-line description, sort column, ascending?)
CRITERIA = {
    "volume": ("Most units sold", "The store that has sold the most of this car is #1; the rest follow in volume order. Another store takes #1 only "
                                  "when its adj total gross per unit beats the leader's by both override margins and it has at least the "
                                  "challenger minimum of sales.", "n", False),
    "total": ("Best total gross", "Adj total deal gross per unit (front + back), shrunk toward the cohort mean. Ties go to volume.", "total_hat", False),
    "front": ("Best front-end gross", "Adj front gross per unit. Reflects pricing power and acquisition cost for this car at that store.", "front_hat", False),
    "back": ("Best back-end gross", "Adj back (F&I) gross per unit. Caution: back gross often says more about the store's finance office than about the car.",
             "back_hat", False),
    "slot": ("Gross per slot per year", "Adj total gross × 365 / adj days to sell: what one inventory slot earns in a year. Rewards profit and turn together.",
             "annual", False),
    "days": ("Fastest turn", "Lowest adj days from receive to sold. Use when moving metal matters more than margin.", "days_hat", True),
    "similar": ("Most similar units sold", "Which store has sold the most cars that look like this one: each sold unit scores 0–1 on matching trim, "
                                           "drivetrain, powertrain, body, mileage band, color and year; the store total is the count of look-alikes. "
                                           "Only meaningful on Placement / VIN pages (there is a vehicle to compare to); elsewhere falls back to volume.",
                "similar", False),
}


@dataclass
class Logic:
    rank_by: str = "volume"
    k: float = PRIOR_K
    min_store: int = MIN_N_STORE
    min_best: int = MIN_N_BEST
    min_cohort: int = MIN_N_COHORT
    over_pct: float = GROSS_OVERRIDE_PCT
    over_abs: float = GROSS_OVERRIDE_ABS

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Logic":
        base = cls()
        if not d:
            return base
        for f, caster in (("rank_by", str), ("k", float), ("min_store", int), ("min_best", int), ("min_cohort", int), ("over_pct", float), ("over_abs", float)):
            if d.get(f) not in (None, ""):
                try:
                    setattr(base, f, caster(d[f]))
                except (TypeError, ValueError):
                    pass
        if base.rank_by not in CRITERIA:
            base.rank_by = "volume"
        base.k = max(0.0, base.k)
        base.min_store = max(1, base.min_store)
        base.min_best = max(1, base.min_best)
        base.min_cohort = max(1, base.min_cohort)
        return base

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def label(self) -> str:
        return CRITERIA[self.rank_by][0]


DEFAULT = Logic()


# ---------------------------------------------------------------- data frame
def load_frame(con) -> pd.DataFrame:
    deals = pd.read_sql_query("SELECT * FROM deals WHERE sale_type IN ('Retail','Lease')", con)
    if deals.empty:
        return deals
    sold = pd.to_datetime(deals["sold_date"], errors="coerce")
    cutoff = sold.max() - pd.DateOffset(months=ANALYSIS_MONTHS)
    deals = deals[sold >= cutoff].reset_index(drop=True)
    specs = pd.read_sql_query(f"SELECT vin, {', '.join(SPEC_KEYS)} FROM vin_specs", con)
    specs = specs.rename(columns={c: f"s_{c}" for c in SPEC_KEYS})
    df = deals.merge(specs, on="vin", how="left")
    canon = []
    for rec in df.to_dict(orient="records"):
        has_spec = isinstance(rec.get("s_make"), str) or isinstance(rec.get("s_error"), str)
        spec = None
        if has_spec:
            spec = {k: (None if (isinstance(rec.get(f"s_{k}"), float) and math.isnan(rec.get(f"s_{k}"))) else rec.get(f"s_{k}")) for k in SPEC_KEYS}
        canon.append(canonical(rec, spec))
    df = df.rename(columns={"year": "adv_year", "make": "adv_make", "model": "adv_model"})
    df = pd.concat([df.reset_index(drop=True), pd.DataFrame(canon)], axis=1)
    df["packages"] = [json.loads(v) if isinstance(v, str) and v.startswith("[") else [] for v in df["s_packages"]]
    df["sold"] = pd.to_datetime(df["sold_date"], errors="coerce")
    df["month"] = df["sold"].dt.to_period("M").astype(str)
    df["store_name"] = df["dealer_code"].map(store_name)
    df["days_c"] = df["days_to_sell"].clip(lower=1, upper=365)
    return df


def data_version(con) -> tuple:
    r = con.execute("SELECT (SELECT COUNT(*) FROM deals), (SELECT COUNT(*) FROM vin_specs WHERE error IS NULL), (SELECT MAX(decoded_at) FROM vin_specs)").fetchone()
    return tuple(r)


# ---------------------------------------------------------------- helpers
def _mean(s) -> Optional[float]:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return float(s.mean()) if len(s) else None


def _median(s) -> Optional[float]:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return float(s.median()) if len(s) else None


def _shrink(n: int, mean: Optional[float], prior: Optional[float], k: float) -> Optional[float]:
    if mean is None:
        return prior
    if prior is None:
        return mean
    return (n * mean + k * prior) / (n + k)


def store_order(df: pd.DataFrame) -> list:
    vol = df.groupby("dealer_code").size().to_dict()
    return sorted(vol, key=lambda c: (STORES.get(c, {}).get("brand", "zz"), -vol[c]))


def package_overlap(units: pd.Series, packages: list) -> pd.Series:
    """Share of the vehicle's packages present on each sold unit (0..1)."""
    want = {str(x).lower() for x in (packages or [])}
    if not want:
        return pd.Series(0.0, index=units.index)
    return units.apply(lambda lst: len(want & {str(x).lower() for x in (lst or [])}) / len(want))


def similarity(g: pd.DataFrame, vehicle: Optional[dict]) -> Optional[float]:
    """Sum over the store's units of (matching attributes / attributes known on the vehicle). Package overlap counts as one attribute."""
    if not vehicle:
        return None
    attrs = [a for a in SIM_ATTRS if vehicle.get(a) not in (None, UNKNOWN, 0, "", "0")]
    pk = vehicle.get("packages") or []
    if not attrs and not pk:
        return None
    score = pd.Series(0.0, index=g.index)
    for a in attrs:
        score += (g[a].astype(str) == str(vehicle[a])).astype(float)
    n_attrs = len(attrs)
    if pk and "packages" in g.columns:
        score += package_overlap(g["packages"], pk)
        n_attrs += 1
    return float((score / n_attrs).sum())


# ---------------------------------------------------------------- scoring
def store_table(sub: pd.DataFrame, logic: Logic = DEFAULT, vehicle: Optional[dict] = None) -> pd.DataFrame:
    """Score every store that sold the deals in `sub`. One row per store, ranked per `logic`."""
    k = logic.k
    pri = {"total": _mean(sub["total_gross"]), "front": _mean(sub["front_gross"]), "back": _mean(sub["back_gross"]), "days": _mean(sub["days_c"]) or 45.0}
    rows = []
    for code, g in sub.groupby("dealer_code"):
        n = len(g)
        total_hat = _shrink(n, _mean(g["total_gross"]), pri["total"], k)
        days_hat = _shrink(n, _mean(g["days_c"]), pri["days"], k)
        rows.append({"store": code, "store_name": store_name(code), "n": n,
                     "front": _mean(g["front_gross"]), "back": _mean(g["back_gross"]), "total": _mean(g["total_gross"]),
                     "front_hat": _shrink(n, _mean(g["front_gross"]), pri["front"], k), "back_hat": _shrink(n, _mean(g["back_gross"]), pri["back"], k),
                     "total_hat": total_hat, "days": _median(g["days_to_sell"]), "days_hat": days_hat,
                     "annual": (total_hat or 0.0) * 365.0 / max(days_hat or pri["days"], MIN_DAYS),
                     "similar": similarity(g, vehicle),
                     "price": _mean(g["sold_price"]), "miles": _median(g["mileage"]), "lease_share": float((g["sale_type"] == "Lease").mean()),
                     "ranked": n >= logic.min_store})
    if not rows:
        return pd.DataFrame(rows)
    ranked = _rank([r for r in rows if r["ranked"]], logic)
    thin = sorted([r for r in rows if not r["ranked"]], key=lambda r: (-r["n"], -(r["total_hat"] or 0)))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    for r in thin:
        r["rank"] = None
    out = pd.DataFrame(ranked + thin)
    out["rank"] = pd.Series([r["rank"] for r in ranked + thin], dtype="object")
    return out


def gross_overrides(challenger: dict, incumbent: dict, logic: Logic) -> bool:
    """True when `challenger` makes so much more per unit than `incumbent` that it should take #1."""
    if challenger["n"] < logic.min_best:
        return False
    c, i = challenger["total_hat"] or 0.0, incumbent["total_hat"] or 0.0
    if c - i < logic.over_abs:
        return False
    if i <= 0:
        return True
    return c >= i * (1.0 + logic.over_pct)


def _rank(rows: list, logic: Logic) -> list:
    by_volume = sorted(rows, key=lambda r: (-r["n"], -(r["total_hat"] or 0)))
    crit = logic.rank_by
    if crit == "similar" and not any(r.get("similar") is not None for r in rows):
        crit = "volume"
    if crit == "volume":
        if len(by_volume) < 2:
            return by_volume
        leader = by_volume[0]
        challengers = [r for r in by_volume[1:] if gross_overrides(r, leader, logic)]
        if challengers:
            top = max(challengers, key=lambda r: r["total_hat"] or 0)
            return [top] + [r for r in by_volume if r is not top]
        return by_volume
    col, asc = CRITERIA[crit][2], CRITERIA[crit][3]

    def key(r):
        v = r.get(col)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            v = float("inf") if asc else float("-inf")
        return (v if asc else -v, -r["n"])
    return sorted(rows, key=key)


def rank_stores(df: pd.DataFrame, vehicle: dict, logic: Logic = DEFAULT) -> Optional[dict]:
    """Walk LEVELS from specific to broad; return the first level with enough history."""
    pk = vehicle.get("packages") or []
    for label, keys, need_options in LEVELS:
        if need_options and not pk:
            continue
        if any(vehicle.get(k) in (None, UNKNOWN, 0, "", "0") for k in keys):
            continue
        mask = pd.Series(True, index=df.index)
        for k in keys:
            mask &= df[k].astype(str) == str(vehicle[k])
        if need_options:
            mask &= package_overlap(df["packages"], pk) >= PACKAGE_MATCH_SHARE
        sub = df[mask]
        if len(sub) < logic.min_cohort:
            continue
        t = store_table(sub, logic, vehicle)
        if t.empty or not t["ranked"].any():
            continue
        match = {k: vehicle[k] for k in keys}
        if need_options:
            match["options"] = f"≥{int(PACKAGE_MATCH_SHARE * 100)}% of: " + ", ".join(pk)
        return {"level": label, "match": match, "n": len(sub), "table": t, "best": t.iloc[0].to_dict(), "deals": sub}
    return None


# ---------------------------------------------------------------- page data
def overview(df: pd.DataFrame) -> list:
    out = []
    for code, g in df.groupby("dealer_code"):
        top = (g.groupby(["make", "model"]).agg(n=("vin", "size"), total=("total_gross", "mean"), days=("days_to_sell", "median"))
               .sort_values("n", ascending=False).head(10).reset_index())
        out.append({"store": code, "store_name": store_name(code), "brand": STORES.get(code, {}).get("brand", ""),
                    "n": len(g), "lease_share": float((g["sale_type"] == "Lease").mean()),
                    "front": _mean(g["front_gross"]), "back": _mean(g["back_gross"]), "total": _mean(g["total_gross"]),
                    "total_sum": float(pd.to_numeric(g["total_gross"], errors="coerce").sum()), "days": _median(g["days_to_sell"]),
                    "price": _mean(g["sold_price"]), "miles": _median(g["mileage"]),
                    "decoded": float(g["decoded"].mean()) if len(g) else 0.0,
                    "top_models": top.to_dict(orient="records")})
    out.sort(key=lambda s: (STORES.get(s["store"], {}).get("brand", "zz"), -s["n"]))
    return out


def matrix(df: pd.DataFrame, keys: list, min_n: int = 3, filters: Optional[dict] = None, logic: Logic = DEFAULT) -> dict:
    """Cohort rows (at `keys` level) x store columns. Each cell: n, avg total gross, median days, rank."""
    sub = df
    for k, v in (filters or {}).items():
        if v not in (None, "", "all"):
            sub = sub[sub[k].astype(str) == str(v)]
    stores = store_order(sub)
    rows = []
    for key, g in sub.groupby(keys, sort=False):
        key = key if isinstance(key, tuple) else (key,)
        n = len(g)
        if n < min_n:
            continue
        t = store_table(g, logic)
        best = t.iloc[0].to_dict() if (not t.empty and t["ranked"].any()) else None
        cells = {r["store"]: r for r in t.to_dict(orient="records")}
        rows.append({"key": dict(zip(keys, key)), "label": " · ".join(str(k) for k in key), "n": n,
                     "total": _mean(g["total_gross"]), "front": _mean(g["front_gross"]), "back": _mean(g["back_gross"]),
                     "days": _median(g["days_to_sell"]), "price": _mean(g["sold_price"]), "best": best, "cells": cells})
    if keys == ["mileage_band"]:
        rows.sort(key=lambda x: BAND_ORDER.index(x["label"]) if x["label"] in BAND_ORDER else 99)
    elif keys == ["year"]:
        rows.sort(key=lambda x: -int(x["key"]["year"] or 0))
    else:
        rows.sort(key=lambda x: -x["n"])
    return {"cols": keys, "stores": stores, "store_names": {c: store_name(c) for c in stores}, "rows": rows}


def package_matrix(sub: pd.DataFrame, min_n: int = 3, logic: Logic = DEFAULT) -> dict:
    """Factory packages x store for the deals in `sub` (a unit with three packages counts in three rows)."""
    cols = ["dealer_code", "sale_type", "total_gross", "front_gross", "back_gross", "days_to_sell", "days_c", "sold_price", "mileage", "packages"]
    ex = sub[cols].explode("packages")
    ex = ex[ex["packages"].notna() & (ex["packages"].astype(str) != "")].rename(columns={"packages": "package"})
    if ex.empty:
        return {"cols": ["package"], "stores": [], "store_names": {}, "rows": []}
    return matrix(ex, ["package"], min_n=min_n, logic=logic)


def store_strengths(df: pd.DataFrame, min_n: int = 4, logic: Logic = DEFAULT) -> dict:
    """For each store: the model cohorts where it ranks #1 under the active logic."""
    m = matrix(df, ["make", "model"], min_n=min_n, logic=logic)
    out = {c: [] for c in m["stores"]}
    for row in m["rows"]:
        b = row["best"]
        if b:
            out.setdefault(b["store"], []).append({**row, "best": b})
    for c in out:
        out[c].sort(key=lambda r: (-r["best"]["n"], -(r["best"]["total_hat"] or 0)))
    return out


def make_list(df: pd.DataFrame) -> list:
    v = df.groupby("make").size().sort_values(ascending=False)
    return [(k, int(n)) for k, n in v.items()]


def vehicle_from_spec(spec: dict, mileage=None, year=None, make=None) -> dict:
    deal = {"year": year or (spec or {}).get("year"), "make": make or (spec or {}).get("make"), "model": None, "mileage": mileage}
    veh = canonical(deal, spec)
    pk = (spec or {}).get("packages_list")
    if pk is None:
        raw = (spec or {}).get("packages")
        pk = json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else []
    veh["packages"] = pk
    return veh
