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

Placement matching (Nick, 2026-10-05): the comparison pool is every sold unit that matches the incoming car
EXACTLY on make, model, trim/version, manufacturer code, body, engine and fuel type. Within the pool the same
model year is preferred, widening to ±1 and ±2 years only when the same year has too few sales. Each unit then
scores on how many of the car's installed options (packages weigh more than standalone options) it shares, plus
a smaller bonus for matching exterior and interior color. Only when the exact pool is too small does matching
relax to same version → same trim → same model → same make, and the result says so.
"""
from __future__ import annotations
import json
import math
import re
from dataclasses import dataclass, asdict
from typing import Optional
import pandas as pd
from app.config import (PRIOR_K, MIN_N_STORE, MIN_N_BEST, MIN_N_COHORT, MIN_DAYS, ANALYSIS_MONTHS, GROSS_OVERRIDE_PCT,
                        GROSS_OVERRIDE_ABS, store_name, STORES)
from app.decode.taxonomy import canonical, UNKNOWN, BAND_ORDER

# Fields that must match exactly to be in the comparison pool (field, label)
REQUIRED = [("make", "Make"), ("model", "Model"), ("version", "Trim / version"), ("mfr_code", "Manufacturer code"),
            ("body", "Body"), ("engine", "Engine"), ("fuel", "Fuel type")]
YEAR_SPANS = [(0, "same yr"), (1, "±1 yr"), (2, "±2 yr")]
MILE_SPANS = [(5000, "≤5k mi"), (10000, "≤10k mi")]
MILE_HARD_LIMIT = 10000     # units farther than this from the car's mileage are never comparable
# Fallbacks when the exact pool is too small: (label, fields that must still match)
FALLBACKS = [("Same version (ignoring manufacturer code, engine, fuel)", ["make", "model", "version"]),
             ("Same trim", ["make", "model", "trim"]), ("Same model", ["make", "model"])]   # never broader than the model
MATCH_STEPS = [
    "Exact match on make, model, trim/version, manufacturer code, body, engine and fuel type",
    "Same model year; widen to ±1 then ±2 years only if the same year has too few sales",
    "Mileage within 5,000 miles either way, widening to 10,000 only if the band has too few sales; beyond 10,000 miles a unit is never comparable",
    "Score each unit on installed options shared with the car (packages weigh 2×, standalone options 1×)",
    "Bonus for matching exterior color, smaller bonus for matching interior color",
    "If the exact pool is too small: same version → same trim → same model (flagged); never broader than the model — a thin pool is used instead",
]
# kept for pages that still list it
LEVELS = [(label, [], False) for label in MATCH_STEPS]
PACKAGE_MATCH_SHARE = 0.5
KEY_PACKAGE_WEIGHT = 3.0    # kept for the Models-page package breakdown weighting of key families
OPTION_WEIGHTS = {"P": 2.0, "O": 1.0}
SIM_WEIGHTS = {"options": 0.55, "year": 0.25, "ext": 0.12, "int": 0.08}
# Key package families: (family label, regex on the package name). Spelling variants collapse into one family,
# e.g. "AMG Line", "AMG Line Exterior Package" and "AMG Line w/Night Package" are all the AMG Line family.
KEY_PACKAGES = [
    ("AMG Line", re.compile(r"\bAMG\s*Line\b", re.I)),
    ("Night", re.compile(r"\bNight\b", re.I)),
    ("Premium", re.compile(r"\bPremium\s*(Package|Pkg|Plus)", re.I)),
    ("Driver Assistance", re.compile(r"\bDriv(er|ing)\s*Assist", re.I)),
    ("Exclusive", re.compile(r"\bExclusive\b", re.I)),
    ("Pinnacle", re.compile(r"\bPinnacle\b", re.I)),
    ("M Sport", re.compile(r"\bM\s*Sport\b", re.I)),
    ("Shadowline", re.compile(r"\bShadow\s*line\b", re.I)),
    ("Executive", re.compile(r"\bExecutive\b", re.I)),
    ("Black Optic", re.compile(r"\bBlack\s*Optic", re.I)),
    ("S Sport", re.compile(r"\bS\s*(Sport|line)\b", re.I)),
    ("Sport Chrono", re.compile(r"\bSport\s*Chrono\b", re.I)),
    ("TRD", re.compile(r"\bTRD\b", re.I)),
    ("F Sport", re.compile(r"\bF\s*SPORT\b", re.I)),
    ("Technology", re.compile(r"\bTechnology\s*(Package|Pkg)", re.I)),
]
_ACCESSORY = re.compile(r"\(PIO\)|floor|mat\b|cargo|wheel lock|key glove|smoking|storage package", re.I)


def key_families(packages) -> list:
    """Key package families present in a list of package names (deduped, in KEY_PACKAGES order)."""
    names = [str(x) for x in (packages or [])]
    return [fam for fam, rx in KEY_PACKAGES if any(rx.search(n) for n in names)]


def option_keys(options) -> dict:
    """{key: type} for an options list; key is TYPE:CODE (or TYPE:NAME when there is no code)."""
    out = {}
    for o in options or []:
        if not isinstance(o, dict):
            continue
        k = f"{o.get('type')}:{(o.get('code') or o.get('name') or '').upper()}"
        if k.endswith(":"):
            continue
        out[k] = o.get("type")
    return out


def minor_packages(packages) -> list:
    """Package names that are neither a key family nor an accessory."""
    out = []
    for n in (packages or []):
        n = str(n)
        if _ACCESSORY.search(n) or any(rx.search(n) for _, rx in KEY_PACKAGES):
            continue
        out.append(n)
    return out
SPEC_KEYS = ["year", "make", "model", "trim", "version", "body_type", "vehicle_type", "drivetrain", "powertrain_type", "ext_base", "msrp", "error",
             "packages", "mfr_code", "engine", "fuel_type", "ext_color", "int_color", "options"]
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
    "similar": ("Most similar units sold", "Which store has sold the most cars that look like this one. Within the exact-match pool each sold unit "
                                           "scores 0–1: 55% for installed options shared with the car (packages 2×, standalone options 1×), 25% for "
                                           "year closeness, 12% exterior color, 8% interior color. The store total is its count of look-alikes. Only "
                                           "meaningful on Placement / VIN pages; elsewhere falls back to volume.",
                "similar", False),
}


DEFAULT_ORDER = ["volume", "total", "front", "back", "slot", "days", "similar"]


@dataclass
class Logic:
    """User-chosen ranking logic. `order` is the criteria priority list: the first decides; stores that are
    within the tie band on it are treated as tied and the next criterion breaks the tie, and so on."""
    order: list = None
    k: float = PRIOR_K
    min_store: int = MIN_N_STORE
    min_best: int = MIN_N_BEST
    min_cohort: int = MIN_N_COHORT
    over_pct: float = GROSS_OVERRIDE_PCT
    over_abs: float = GROSS_OVERRIDE_ABS
    tie_units_pct: float = 0.15     # units sold within 15% of each other = tied
    tie_gross: float = 500.0        # adj gross figures within $500 = tied
    tie_days: float = 5.0           # adj days within 5 = tied
    tie_rel: float = 0.15           # gross/slot/yr and similarity within 15% = tied

    def __post_init__(self):
        if not self.order:
            self.order = list(DEFAULT_ORDER)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Logic":
        base = cls()
        if not d:
            return base
        for f, caster in (("k", float), ("min_store", int), ("min_best", int), ("min_cohort", int), ("over_pct", float), ("over_abs", float),
                          ("tie_units_pct", float), ("tie_gross", float), ("tie_days", float), ("tie_rel", float)):
            if d.get(f) not in (None, ""):
                try:
                    setattr(base, f, max(0.0, caster(d[f])))
                except (TypeError, ValueError):
                    pass
        order = d.get("order") or ([d["rank_by"]] if d.get("rank_by") else [])
        if isinstance(order, str):
            order = [x.strip() for x in order.split(",")]
        seen = []
        for c in order:
            if c in CRITERIA and c not in seen:
                seen.append(c)
        base.order = seen + [c for c in DEFAULT_ORDER if c not in seen]
        base.min_store = max(1, int(base.min_store))
        base.min_best = max(1, int(base.min_best))
        base.min_cohort = max(1, int(base.min_cohort))
        return base

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def rank_by(self) -> str:
        return self.order[0]

    @property
    def label(self) -> str:
        return CRITERIA[self.order[0]][0]

    @property
    def chain(self) -> str:
        return " → ".join(CRITERIA[c][0] for c in self.order)


# "How close counts as a tie" presets -> (units %, gross $, days, relative %)
TIE_PRESETS = {
    "tight": ("Tight", "stores must be almost identical to count as tied", 0.05, 200.0, 2.0, 0.05),
    "normal": ("Normal", "a modest gap still counts as tied", 0.15, 500.0, 5.0, 0.15),
    "loose": ("Loose", "stores in the same ballpark count as tied", 0.25, 1000.0, 10.0, 0.25),
}


def tie_preset_of(logic: "Logic") -> str:
    for key, (_, _, u, g, d, r) in TIE_PRESETS.items():
        if (abs(logic.tie_units_pct - u) < 1e-9 and abs(logic.tie_gross - g) < 1e-6 and abs(logic.tie_days - d) < 1e-6 and abs(logic.tie_rel - r) < 1e-9):
            return key
    return "custom"


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
    df["key_pkgs"] = [key_families(p) for p in df["packages"]]
    df["opt_keys"] = [option_keys(o) for o in df["options"]]
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
    """Weighted share of the vehicle's packages present on each sold unit (0..1). Key families (AMG Line, Night,
    Premium, ...) weigh KEY_PACKAGE_WEIGHT each; other non-accessory packages weigh 1; accessories are ignored."""
    fams = key_families(packages)
    minors = {m.lower() for m in minor_packages(packages)}
    total = KEY_PACKAGE_WEIGHT * len(fams) + len(minors)
    if total == 0:
        return pd.Series(0.0, index=units.index)

    def score(lst):
        lst = lst or []
        have_f = set(key_families(lst))
        have_m = {str(x).lower() for x in lst}
        return (KEY_PACKAGE_WEIGHT * len(have_f & set(fams)) + len(minors & have_m)) / total
    return units.apply(score)


def options_filter(units: pd.Series, packages: list) -> pd.Series:
    """Which sold units count as an options match for the Placement levels: every key family of the car must be
    present; with no key families, at least PACKAGE_MATCH_SHARE of its (non-accessory) packages."""
    fams = key_families(packages)
    if fams:
        want = set(fams)
        return units.apply(lambda lst: want <= set(key_families(lst)))
    return package_overlap(units, packages) >= PACKAGE_MATCH_SHARE


def option_share(units: pd.Series, veh_opts: dict) -> pd.Series:
    """Weighted share of the car's installed options present on each unit (0..1). Packages weigh 2, options 1."""
    if not veh_opts:
        return pd.Series(0.0, index=units.index)
    total = sum(OPTION_WEIGHTS.get(t, 1.0) for t in veh_opts.values())
    return units.apply(lambda ok: sum(OPTION_WEIGHTS.get(t, 1.0) for k, t in veh_opts.items() if k in (ok or {})) / total)


def year_closeness(years: pd.Series, year) -> pd.Series:
    """1.0 same year, 0.7 ±1, 0.4 ±2, 0.1 beyond."""
    if not year:
        return pd.Series(1.0, index=years.index)
    dy = (pd.to_numeric(years, errors="coerce") - int(year)).abs()
    return dy.map(lambda d: 1.0 if d == 0 else 0.7 if d == 1 else 0.4 if d == 2 else 0.1).fillna(0.1)


def unit_similarity(g: pd.DataFrame, vehicle: Optional[dict]) -> Optional[pd.Series]:
    """Per sold unit, 0..1: options shared (55%), year closeness (25%), exterior color (12%), interior color (8%).
    Weights renormalize when the car has no installed options or an unknown color."""
    if not vehicle:
        return None
    veh_opts = vehicle.get("opt_keys") or option_keys(vehicle.get("options"))
    parts, weights = [], []
    if veh_opts:
        parts.append(option_share(g["opt_keys"], veh_opts)); weights.append(SIM_WEIGHTS["options"])
    parts.append(year_closeness(g["year"], vehicle.get("year"))); weights.append(SIM_WEIGHTS["year"])
    if vehicle.get("ext_color") not in (None, UNKNOWN, ""):
        parts.append((g["ext_color"].astype(str) == str(vehicle["ext_color"])).astype(float)); weights.append(SIM_WEIGHTS["ext"])
    if vehicle.get("int_color") not in (None, UNKNOWN, ""):
        parts.append((g["int_color"].astype(str) == str(vehicle["int_color"])).astype(float)); weights.append(SIM_WEIGHTS["int"])
    total = sum(weights)
    score = pd.Series(0.0, index=g.index)
    for part, w in zip(parts, weights):
        score += part * (w / total)
    return score


def similarity(g: pd.DataFrame, vehicle: Optional[dict]) -> Optional[float]:
    """Count of look-alike units at the store: sum of per-unit similarity."""
    u = unit_similarity(g, vehicle)
    return None if u is None else float(u.sum())


def options_match(g: pd.DataFrame, vehicle: Optional[dict]) -> Optional[float]:
    """Average share of the car's installed options found on the store's units (0..1)."""
    veh_opts = (vehicle or {}).get("opt_keys") or option_keys((vehicle or {}).get("options"))
    if not veh_opts or g.empty:
        return None
    return float(option_share(g["opt_keys"], veh_opts).mean())


def sim_detail(g: pd.DataFrame, vehicle: Optional[dict]) -> Optional[dict]:
    """What matched, for the Similar breakdown."""
    if not vehicle:
        return None
    n = int(len(g))
    veh_opts = vehicle.get("opt_keys") or option_keys(vehicle.get("options"))
    yrs = pd.to_numeric(g["year"], errors="coerce")
    vy = int(vehicle.get("year") or 0)
    years = {"same": int((yrs == vy).sum()), "pm1": int(((yrs - vy).abs() == 1).sum()), "pm2": int(((yrs - vy).abs() == 2).sum()),
             "more": int(((yrs - vy).abs() > 2).sum())} if vy else None
    opts = []
    for o in vehicle.get("options") or []:
        if not isinstance(o, dict):
            continue
        k = f"{o.get('type')}:{(o.get('code') or o.get('name') or '').upper()}"
        cnt = int(g["opt_keys"].apply(lambda ok: k in (ok or {})).sum())
        opts.append({"type": o.get("type"), "code": o.get("code"), "name": o.get("name"), "msrp": o.get("msrp"), "matched": cnt})
    opts.sort(key=lambda x: (x["type"] != "P", -(x["msrp"] or 0)))
    ext = int((g["ext_color"].astype(str) == str(vehicle.get("ext_color"))).sum()) if vehicle.get("ext_color") not in (None, UNKNOWN, "") else None
    inte = int((g["int_color"].astype(str) == str(vehicle.get("int_color"))).sum()) if vehicle.get("int_color") not in (None, UNKNOWN, "") else None
    w = dict(SIM_WEIGHTS)
    if not veh_opts:
        w["options"] = 0.0
    tot = sum(v for k, v in w.items() if (k != "ext" or ext is not None) and (k != "int" or inte is not None))
    weights = {k: round(v / tot * 100) if tot else 0 for k, v in w.items()}
    return {"n": n, "required": [(label, vehicle.get(f)) for f, label in REQUIRED if vehicle.get(f) not in (None, UNKNOWN, "")],
            "years": years, "options": opts, "ext": ext, "ext_color": vehicle.get("ext_color"), "int": inte, "int_color": vehicle.get("int_color"),
            "weights": weights}


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
                     "similar": similarity(g, vehicle), "opt_match": options_match(g, vehicle), "sim_detail": sim_detail(g, vehicle),
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


def _val(r: dict, col: str):
    v = r.get(col)
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return float(v)


def _tied(crit: str, a, b, logic: Logic) -> bool:
    """Are two values on `crit` close enough to count as a tie (so the next criterion decides)?"""
    if a is None or b is None:
        return False
    if crit == "volume":
        return abs(a - b) <= logic.tie_units_pct * max(a, b)
    if crit in ("total", "front", "back"):
        return abs(a - b) <= logic.tie_gross
    if crit == "days":
        return abs(a - b) <= logic.tie_days
    return abs(a - b) <= logic.tie_rel * max(abs(a), abs(b))


def _tier_sort(rows: list, order: list, logic: Logic) -> list:
    if len(rows) < 2:
        return list(rows)
    if not order:
        return sorted(rows, key=lambda r: (-r["n"], -(r["total_hat"] or 0)))
    crit = order[0]
    col, asc = CRITERIA[crit][2], CRITERIA[crit][3]

    def key(r):
        v = _val(r, col)
        if v is None:
            return (1, 0.0, -r["n"])
        return (0, v if asc else -v, -r["n"])
    ordered = sorted(rows, key=key)
    tiers, cur = [], []
    for r in ordered:
        if cur and _tied(crit, _val(cur[0], col), _val(r, col), logic):
            cur.append(r)
        else:
            if cur:
                tiers.append(cur)
            cur = [r]
    if cur:
        tiers.append(cur)
    out = []
    for t in tiers:
        out += _tier_sort(t, order[1:], logic) if len(t) > 1 else t
    return out


def _rank(rows: list, logic: Logic) -> list:
    """Priority-ordered ranking. 'Most similar units' is skipped when there is no vehicle to compare to."""
    have_similar = any(r.get("similar") is not None for r in rows)
    order = [c for c in logic.order if c != "similar" or have_similar]
    ranked = _tier_sort(rows, order, logic)
    if order and order[0] == "volume" and len(ranked) >= 2:          # Nick's rule: gross override on the #1 slot
        leader = ranked[0]
        challengers = [r for r in ranked[1:] if gross_overrides(r, leader, logic)]
        if challengers:
            top = max(challengers, key=lambda r: r["total_hat"] or 0)
            ranked = [top] + [r for r in ranked if r is not top]
    return ranked


def rank_stores(df: pd.DataFrame, vehicle: dict, logic: Logic = DEFAULT) -> Optional[dict]:
    """Build the comparison pool: mileage within the hard limit, exact on REQUIRED (relaxing only if too thin),
    then prefer the same year and the tighter mileage band. Rank the stores in it."""
    known = lambda f: vehicle.get(f) not in (None, UNKNOWN, 0, "", "0")  # noqa: E731
    if vehicle.get("vin"):
        df = df[df["vin"] != vehicle["vin"]]      # a car we sold before must not match itself
    mile_limited = vehicle.get("mileage") is not None
    if mile_limited:
        miles = pd.to_numeric(df["mileage"], errors="coerce")
        df = df[(miles - int(vehicle["mileage"])).abs() <= MILE_HARD_LIMIT]

    def exact(fields):
        mask = pd.Series(True, index=df.index)
        for f in fields:
            mask &= df[f].astype(str) == str(vehicle[f])
        return mask

    req = [f for f, _ in REQUIRED if known(f)]
    attempts = [("Exact", req)] + [(label, [f for f in fields if known(f)]) for label, fields in FALLBACKS]
    chosen = None
    for label, fields in attempts:
        if not fields:
            continue
        pool = df[exact(fields)]
        if pool.empty:
            continue
        if chosen is None:
            chosen = (label, fields, pool)          # most specific non-empty pool, used if nothing reaches the minimum
        if len(pool) >= logic.min_cohort:
            chosen = (label, fields, pool)
            break
    if chosen is None:
        return None
    label, fields, pool = chosen
    year_label = "any yr"
    if known("year"):
        yrs = pd.to_numeric(pool["year"], errors="coerce")
        for span, yl in YEAR_SPANS:
            sub = pool[(yrs - int(vehicle["year"])).abs() <= span]
            if len(sub) >= logic.min_cohort:
                pool, year_label = sub, yl
                break
    mile_label = "any mi"
    if mile_limited:
        miles = pd.to_numeric(pool["mileage"], errors="coerce")
        mile_label = MILE_SPANS[-1][1]
        for span, ml in MILE_SPANS:
            sub = pool[(miles - int(vehicle["mileage"])).abs() <= span]
            if len(sub) >= logic.min_cohort:
                pool, mile_label = sub, ml
                break
    t = store_table(pool, logic, vehicle)
    if t.empty:
        return None
    thin = not bool(t["ranked"].any())
    match = {lab: vehicle[f] for f, lab in REQUIRED if f in fields}
    match["year"] = year_label
    match["mileage"] = mile_label
    return {"level": f"{label} · {year_label} · {mile_label}", "exact": label == "Exact", "thin": thin, "match": match,
            "n": len(pool), "table": t, "best": t.iloc[0].to_dict(), "deals": pool,
            "pool_detail": sim_detail(pool, vehicle), "relaxed": [lab for f, lab in REQUIRED if known(f) and f not in fields],
            "stores_in_pool": int(pool["dealer_code"].nunique())}


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
                     "days": _median(g["days_to_sell"]), "price": _mean(g["sold_price"]), "best": best, "cells": cells,
                     "ranking": t.to_dict(orient="records")})
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
    veh["mileage"] = mileage
    veh["vin"] = (spec or {}).get("vin")
    pk = (spec or {}).get("packages_list")
    if pk is None:
        raw = (spec or {}).get("packages")
        pk = json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else []
    veh["packages"] = pk
    veh["key_pkgs"] = key_families(pk)
    veh["opt_keys"] = option_keys(veh.get("options"))
    return veh
