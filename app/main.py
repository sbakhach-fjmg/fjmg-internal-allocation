"""FJ Used Sales Analyzer — FastAPI app."""
from __future__ import annotations
import io
import json
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import (DATA_DIR, STORES, EXCLUDED_STORES, APP_PASSWORD, DECODE_PASSWORD, PLACEMENT_MAX_VINS, store_name, MIN_N_STORE, MIN_N_BEST, MIN_N_COHORT, PRIOR_K, ANALYSIS_MONTHS,
                        GROSS_OVERRIDE_PCT, GROSS_OVERRIDE_ABS)
from app.db import init_db, connect, db, scalar
from app.ingest.sales import parse_sales, store_deals, ORIGIN
from app.decode import marketcheck as mc
from app.decode.taxonomy import UNKNOWN
from app.analysis import cohorts as co
from app.analysis.cohorts import (Logic, CRITERIA, LEVELS, MATCH_STEPS, REQUIRED, TIE_PRESETS, tie_preset_of, KEY_PACKAGES, SIM_WEIGHTS,
                                  OPTION_WEIGHTS, YEAR_SPANS, MILE_SPANS)
from app.decode.taxonomy import MILEAGE_BANDS
from app.analysis.enrich import backfill, decode_counts
from app.auth import AuthMiddleware, password_ok, decode_password_ok, set_session, COOKIE

BASE = Path(__file__).resolve().parent
app = FastAPI(title="FJ Used Sales Analyzer")
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
init_db()


# ---------------------------------------------------------------- template filters
def _isnull(v) -> bool:
    try:
        return v is None or bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def f_money(v, dec=0):
    if _isnull(v):
        return "–"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "–"
    s = f"{abs(v):,.{dec}f}"
    return f"-${s}" if v < 0 else f"${s}"


def f_num(v, dec=0):
    if _isnull(v):
        return "–"
    try:
        return f"{float(v):,.{dec}f}"
    except (TypeError, ValueError):
        return "–"


def f_pct(v):
    if _isnull(v):
        return "–"
    try:
        return f"{float(v) * 100:.0f}%"
    except (TypeError, ValueError):
        return "–"


ACRONYM_MAKES = {"BMW", "GMC", "RAM", "MINI", "AMC", "SRT", "VW", "MG", "FJ"}


def f_title(v):
    """Title-case an upper-case make, keeping acronym makes (BMW, GMC, RAM, MINI) upper-case."""
    if not (isinstance(v, str) and v.isupper()):
        return v
    return " ".join(w if w in ACRONYM_MAKES else w.title() for w in v.split(" "))


templates.env.filters.update(money=f_money, num=f_num, pct=f_pct, t=f_title)
templates.env.globals.update(STORES=STORES, store_name=store_name, UNKNOWN=UNKNOWN, ORIGIN=ORIGIN, now=datetime.now,
                             MIN_N_STORE=MIN_N_STORE, MIN_N_BEST=MIN_N_BEST, MIN_N_COHORT=MIN_N_COHORT, PRIOR_K=PRIOR_K, ANALYSIS_MONTHS=ANALYSIS_MONTHS,
                             GROSS_OVERRIDE_PCT=GROSS_OVERRIDE_PCT, GROSS_OVERRIDE_ABS=GROSS_OVERRIDE_ABS, CRITERIA=CRITERIA, LEVELS=LEVELS, MATCH_STEPS=MATCH_STEPS, MIN_DAYS=10, TIE_PRESETS=TIE_PRESETS, tie_preset_of=tie_preset_of,
                             PLACEMENT_MAX_VINS=PLACEMENT_MAX_VINS)


# ---------------------------------------------------------------- cached frame + background job
_FRAME = {"version": None, "df": None}
_frame_lock = threading.Lock()
JOB = {"status": "idle"}


def frame() -> pd.DataFrame:
    with connect() as con:
        v = co.data_version(con)
        with _frame_lock:
            if _FRAME["version"] != v:
                _FRAME["df"] = co.load_frame(con)
                _FRAME["version"] = v
            return _FRAME["df"]


def start_backfill(limit: Optional[int] = None) -> bool:
    if JOB.get("status") == "running":
        return False
    JOB.clear()
    JOB.update({"status": "running", "cancel": False})

    def _go():
        con = connect()
        try:
            backfill(con, JOB, **({"limit": limit} if limit else {}))
            JOB["status"] = "done"
        except Exception as e:  # noqa: BLE001
            JOB["status"] = "error"
            JOB["error"] = str(e)
        finally:
            con.close()
    threading.Thread(target=_go, daemon=True).start()
    return True


LOGIC_COOKIE = "fj_used_logic"


def get_logic(request: Request) -> Logic:
    raw = request.cookies.get(LOGIC_COOKIE)
    try:
        return Logic.from_dict(json.loads(raw)) if raw else Logic()
    except (ValueError, TypeError):
        return Logic()


def ctx(request: Request, **kw) -> dict:
    with connect() as con:
        deals = scalar(con, "SELECT COUNT(*) FROM deals") or 0
        rng = con.execute("SELECT MIN(sold_date), MAX(sold_date) FROM deals").fetchone()
        dc = decode_counts(con)
    df = _FRAME["df"]
    if df is not None and not df.empty:
        win = (str(df["sold"].min().date()), str(df["sold"].max().date()))
    else:
        win = (rng[0], rng[1])
    return {"request": request, "deals": deals, "sold_min": win[0], "sold_max": win[1], "all_min": rng[0], "dc": dc, "job": JOB, "mc": mc.status(),
            "flash": request.query_params.get("msg"), "path": request.url.path, "logic": get_logic(request),
            "decode_gated": bool(DECODE_PASSWORD), **kw}


# ---------------------------------------------------------------- auth / health
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = ""):
    if not APP_PASSWORD:   # no password configured (local use): nothing to sign in to
        return RedirectResponse(url=next if next.startswith("/") else "/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "next": next, "error": error})


@app.post("/login")
def login(password: str = Form(...), next: str = Form("/")):
    if not password_ok(password):
        return RedirectResponse(url=f"/login?next={next}&error=1", status_code=303)
    return set_session(RedirectResponse(url=next if next.startswith("/") else "/", status_code=303))


@app.post("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- pages
@app.get("/stores", response_class=HTMLResponse)
def overview(request: Request):
    df = frame()
    if df.empty:
        return RedirectResponse(url="/upload?msg=Upload+the+Advent+sales+file+to+get+started", status_code=303)
    logic = get_logic(request)
    stores = co.overview(df)
    strengths = co.store_strengths(df, logic=logic)
    return templates.TemplateResponse("overview.html", ctx(request, stores=stores, strengths=strengths))


@app.get("/models", response_class=HTMLResponse)
def models(request: Request, make: str = "all", model: str = "all", year: str = "all", level: str = "model", min_n: int = 3):
    df = frame()
    if df.empty:
        return RedirectResponse(url="/upload", status_code=303)
    keys = {"model": ["make", "model"], "trim": ["make", "model", "trim"], "year": ["year", "make", "model"],
            "miles": ["make", "model", "mileage_band"], "spec": ["make", "model", "spec"], "body": ["make", "body"]}.get(level, ["make", "model"])
    scope = df if make == "all" else df[df["make"] == make]
    models_list = [(k, int(n)) for k, n in scope.groupby("model").size().sort_values(ascending=False).items()]
    if model != "all" and model not in {k for k, _ in models_list}:
        model = "all"
    m = co.matrix(df, keys, min_n=min_n, filters={"make": make, "model": model, "year": year}, logic=get_logic(request))
    years = sorted({int(y) for y in df["year"].dropna().unique() if y}, reverse=True)
    return templates.TemplateResponse("models.html", ctx(request, m=m, make=make, model=model, year=year, level=level, min_n=min_n,
                                                         makes=co.make_list(df), models_list=models_list, years=years))


@app.get("/model", response_class=HTMLResponse)
def model_detail(request: Request, make: str, model: str):
    df = frame()
    sub = df[(df["make"] == make) & (df["model"] == model)] if not df.empty else df
    if sub.empty:
        return RedirectResponse(url="/models?msg=No+deals+for+that+model", status_code=303)
    logic = get_logic(request)
    overall = co.store_table(sub, logic)
    mx = lambda keys: co.matrix(sub, keys, min_n=2, logic=logic)  # noqa: E731
    sections = [("By year", mx(["year"])), ("By trim", mx(["trim"])), ("By mileage band", mx(["mileage_band"])),
                ("By spec (drivetrain · powertrain)", mx(["spec"])), ("By body", mx(["body"])), ("By exterior color", mx(["ext_base"])),
                ("Year · Trim · Miles", mx(["year", "trim", "mileage_band"])), ("By factory package", co.package_matrix(sub, min_n=3, logic=logic))]
    deals = sub.sort_values("sold", ascending=False)
    return templates.TemplateResponse("model.html", ctx(request, make=make, model=model, overall=overall.to_dict(orient="records"),
                                                        sections=sections, deals=deals.to_dict(orient="records"), n=len(sub),
                                                        decoded=float(sub["decoded"].mean())))


# ---------------------------------------------------------------- placement recommender
_VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")


def parse_vehicle_lines(text: str) -> list:
    """Each line: VIN [mileage] [note...]. Mileage = first number after the VIN."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _VIN_RE.search(line.upper())
        if not m:
            out.append({"vin": None, "raw": line, "mileage": None})
            continue
        nums = re.findall(r"\d[\d,]*", line.upper()[m.end():])
        miles = int(nums[0].replace(",", "")) if nums else None
        out.append({"vin": m.group(1), "mileage": miles, "raw": line})
    return out


def parse_vehicle_file(data: bytes, name: str) -> list:
    buf = io.BytesIO(data)
    df = pd.read_csv(buf) if name.lower().endswith(".csv") else pd.read_excel(buf)
    cols = {re.sub(r"[^a-z0-9]", "", str(c).lower()): c for c in df.columns}
    vcol = next((cols[k] for k in cols if k in ("vin", "vinnumber")), None)
    mcol = next((cols[k] for k in cols if k in ("mileage", "miles", "odometer", "odo")), None)
    if not vcol:
        raise ValueError(f"No VIN column found. Columns: {list(df.columns)}")
    out = []
    for _, r in df.iterrows():
        vin = str(r[vcol]).strip().upper()
        if len(vin) != 17:
            continue
        miles = None
        if mcol is not None:
            try:
                miles = int(float(str(r[mcol]).replace(",", "")))
            except (TypeError, ValueError):
                miles = None
        out.append({"vin": vin, "mileage": miles, "raw": vin})
    return out


def place(vehicles: list, logic: Logic) -> list:
    df = frame()
    results = []
    with connect() as con:
        for v in vehicles:
            if not v.get("vin"):
                results.append({**v, "error": "no VIN found on this line"})
                continue
            spec = mc.decode_and_store(con, v["vin"])
            pk = spec.get("packages")
            spec["packages_list"] = json.loads(pk) if isinstance(pk, str) and pk.startswith("[") else []
            op = spec.get("options")
            spec["options_list"] = json.loads(op) if isinstance(op, str) and op.startswith("[") else []
            try:
                raw = json.loads(spec.get("raw") or "{}")
            except ValueError:
                raw = {}
            spec["confidence"] = {k.replace("_confidence", "").replace("_", " "): raw.get(k) for k in
                                  ("trim_confidence", "version_confidence", "transmission_confidence", "listing_confidence", "record_confidence") if raw.get(k) is not None}
            if spec.get("error"):
                results.append({**v, "spec": spec, "error": f"decode failed: {spec['error']}"})
                continue
            veh = co.vehicle_from_spec(spec, mileage=v.get("mileage"))
            rec = co.rank_stores(df, veh, logic) if not df.empty else None
            history = df[df["vin"] == v["vin"]].sort_values("sold", ascending=False).to_dict(orient="records") if not df.empty else []
            results.append({**v, "spec": spec, "veh": veh, "rec": rec, "history": history,
                            "table": rec["table"].to_dict(orient="records") if rec else [],
                            "error": None if rec else "not enough retail history for this kind of car at any level"})
    return results


@app.get("/", response_class=HTMLResponse)
@app.get("/placement", response_class=HTMLResponse)
def placement_page(request: Request):
    return templates.TemplateResponse("placement.html", ctx(request, results=None, text="", error=None))


@app.post("/")
@app.post("/placement", response_class=HTMLResponse)
async def placement_run(request: Request, text: str = Form(""), file: UploadFile = File(None)):
    vehicles = parse_vehicle_lines(text)
    err = None
    if file and file.filename:
        try:
            vehicles += parse_vehicle_file(await file.read(), file.filename)
        except Exception as e:  # noqa: BLE001
            err = str(e)
    # de-duplicate on VIN, then cap the run
    seen, unique = set(), []
    for v in vehicles:
        key = v.get("vin") or v.get("raw")
        if key in seen:
            continue
        seen.add(key)
        unique.append(v)
    if len(unique) > PLACEMENT_MAX_VINS:
        err = (err + " · " if err else "") + f"{len(unique)} vehicles submitted; only the first {PLACEMENT_MAX_VINS} were run. Split larger lists into batches."
        unique = unique[:PLACEMENT_MAX_VINS]
    vehicles = unique
    results = place(vehicles, get_logic(request)) if vehicles else []
    text_all = "\n".join(f"{r['vin']} {r['mileage'] or ''}".strip() for r in results if r.get("vin"))
    return templates.TemplateResponse("placement.html", ctx(request, results=results, text=text_all, error=err))


@app.post("/placement/export")
def placement_export(request: Request, text: str = Form("")):
    logic = get_logic(request)
    results = place(parse_vehicle_lines(text)[:PLACEMENT_MAX_VINS], logic)
    rows, detail = [], []
    for r in results:
        veh, rec = r.get("veh") or {}, r.get("rec")
        base = {"VIN": r.get("vin"), "Mileage": r.get("mileage"), "Year": veh.get("year"), "Make": f_title(veh.get("make")), "Model": veh.get("model"),
                "Trim": veh.get("trim"), "Body": veh.get("body"), "Spec": veh.get("spec"), "Mileage band": veh.get("mileage_band")}
        if not rec:
            rows.append({**base, "Recommendation": r.get("error")})
            continue
        t = rec["table"]
        for i, (_, s) in enumerate(t[t["ranked"]].head(3).iterrows(), 1):
            base[f"#{i} store"] = s["store_name"]
            base[f"#{i} n"] = s["n"]
            base[f"#{i} adj total"] = round(s["total_hat"])
            base[f"#{i} adj days"] = round(s["days_hat"])
        rows.append({**base, "Matched on": rec["level"], "History n": rec["n"], "Ranked by": logic.chain})
        for _, s in t.iterrows():
            detail.append({"VIN": r["vin"], "Matched on": rec["level"], "Store": s["store_name"], "Code": s["store"], "Rank": s["rank"],
                           "n": s["n"], "Similar": s["similar"], "Options match": s["opt_match"], "Avg front": s["front"], "Adj front": s["front_hat"], "Avg back": s["back"],
                           "Adj back": s["back_hat"], "Avg total": s["total"], "Adj total": s["total_hat"], "Median days": s["days"],
                           "Adj days": s["days_hat"], "Gross / slot / yr": s["annual"], "Avg price": s["price"]})
    return _xlsx({"Placement": pd.DataFrame(rows), "Store detail": pd.DataFrame(detail)}, "placement")


# ---------------------------------------------------------------- logic
@app.get("/logic", response_class=HTMLResponse)
def logic_page(request: Request):
    return templates.TemplateResponse("logic.html", ctx(request))


@app.post("/logic")
async def logic_save(request: Request):
    form = await request.form()
    d = {k: form.get(k) for k in ("order", "k", "min_store", "min_best", "min_cohort", "over_abs", "tie_gross", "tie_days")}
    for pct_field, target in (("over_pct", "over_pct"), ("tie_units_pct", "tie_units_pct"), ("tie_rel", "tie_rel")):
        try:
            d[target] = float(form.get(pct_field) or 0) / 100.0
        except ValueError:
            d[target] = None
    preset = form.get("tie_preset")
    if preset in TIE_PRESETS and form.get("advanced_open") != "1":   # simple mode: the preset sets all four tie bands
        _, _, u, g, dd, r = TIE_PRESETS[preset]
        d.update({"tie_units_pct": u, "tie_gross": g, "tie_days": dd, "tie_rel": r})
    logic = Logic.from_dict(d)
    resp = RedirectResponse(url="/logic?msg=Saved.+Ranking+by+" + logic.label.replace(" ", "+"), status_code=303)
    resp.set_cookie(LOGIC_COOKIE, json.dumps(logic.to_dict()), max_age=365 * 24 * 3600, samesite="lax")
    return resp


@app.get("/logic/reset")
def logic_reset():
    resp = RedirectResponse(url="/logic?msg=Reset+to+defaults", status_code=303)
    resp.delete_cookie(LOGIC_COOKIE)
    return resp


# ---------------------------------------------------------------- rules
def rules_for(logic: Logic) -> list:
    m = f_money
    stores = ", ".join(f"{v['name']} ({k})" for k, v in STORES.items())
    excluded = ", ".join(sorted(EXCLUDED_STORES))
    bands = " · ".join(b[2] for b in MILEAGE_BANDS)
    fams = ", ".join(f for f, _ in KEY_PACKAGES)
    levels = "".join(f"<li>{step}</li>" for step in MATCH_STEPS)
    preset = tie_preset_of(logic)
    chain = " → ".join(CRITERIA[c][0] for c in logic.order)
    return [
        ("Data", [
            {"title": "Retail and lease only", "body": "Only used deals with Sale Type <b>Retail</b> or <b>Lease</b> are loaded. Wholesale deals are dropped at import because they are not a retail outcome and we want to retail these cars. New-car rows are dropped too."},
            {"title": "Active stores only", "body": f"Deals from stores no longer with the group are dropped at import: <b>{excluded}</b>. Active stores: {stores}. Store names can be overridden in <code>data/stores.json</code>."},
            {"title": "Rolling six-month window", "body": f"Every ranking uses deals sold in the last <b>{ANALYSIS_MONTHS} months</b> ending at the newest sold date in the data. Older deals stay in the database but are not counted."},
            {"title": "Monthly re-upload merges", "body": "Deals are keyed on store + deal number + VIN, so uploading the Advent file again (overlapping months included) updates existing deals and adds new ones. Nothing is duplicated."},
            {"title": "Decode once, cache forever", "body": "Each VIN is decoded through MarketCheck once and stored. Only VINs the site has never seen cost an API call. The cache can be exported and imported on the Data tab, so a new install does not re-decode."},
            {"title": "Decoding needs a password", "body": "Starting a backfill (the \"Decode pending VINs\" button, or decode-after-upload) requires the decode password because each VIN is a MarketCheck call. Placement lookups still decode a brand-new VIN on the fly for signed-in users."},
        ]),
        ("Vehicle taxonomy", [
            {"title": "Year and make from Advent", "body": "Year and make come from the sales export. Model, trim, body, drivetrain, powertrain, exterior color, MSRP and factory packages come from the MarketCheck NeoVIN decode."},
            {"title": "Trims normalized and upper-cased", "body": "Mercedes trims are spaced consistently (C300 → C 300, GLC350e → GLC 350E) and drivetrain words like 4MATIC are removed from the trim, since drivetrain lives in Spec. A \"Base\" Mercedes trim is replaced by the version name (EQS 450+). All trims display in upper case."},
            {"title": "Body words stripped from model", "body": "Trailing body words are removed from model names so C-Class Sedan and C-Class group together. Genuinely different models keep their names: EQS SUV is not EQS, Bronco 4-Door is not Bronco."},
            {"title": "Mileage bands", "body": f"Mileage is grouped into bands: <b>{bands}</b>. Placement uses the mileage you supply with the VIN; a VIN decode does not carry mileage."},
            {"title": "Spec is drivetrain and powertrain", "body": "On the Models page, Spec = drivetrain (4WD / 2WD) · powertrain (Combustion, MHEV, HEV, PHEV, BEV). Placement matching uses the fuller version string and manufacturer code instead."},
            {"title": "Key package families", "body": f"On the Models page, factory packages are grouped into families so spelling variants roll up together: <b>{fams}</b>. Placement matching uses the exact option codes instead."},
            {"title": "Decode confidence shown", "body": "MarketCheck reports how confident it is in the trim, version and transmission it decoded, plus an overall record confidence. These are shown on the VIN page so a low-confidence decode can be sanity-checked."},
            {"title": "Base cars are real", "body": "A unit with no packages is a base-spec car, not missing data. Against an optioned car it counts as an options mismatch; against another base car the options half of the score simply does not apply."},
            {"title": "Undecoded VINs fall back", "body": "If a VIN has not been decoded yet, its model and trim are approximated from the Advent model code (Mercedes sales codes are expanded) and spec shows as (unknown) until the backfill catches up."},
        ]),
        ("Ranking", [
            {"title": "Volume leader is number one", "body": "Under the default criterion the store that has sold the most of a car ranks first and the rest follow in volume order. Volume is the proven-market signal."},
            {"title": "Gross override", "body": f"When \"Most units sold\" is first, another store takes #1 only if its adjusted total gross is at least <b>{m(logic.over_abs)}</b> more per car than the volume leader's" + (f" and at least {int(logic.over_pct*100)}% more" if logic.over_pct > 0 else "") + f", and it has at least <b>{logic.min_best}</b> sales of its own."},
            {"title": "Priority order with tie bands", "body": f"Criteria are ranked in the order set on the Logic tab, currently <b>{chain}</b>. The first criterion decides; stores within the tie band on it count as tied and the next criterion breaks the tie. Tie preset: <b>{TIE_PRESETS[preset][0] if preset in TIE_PRESETS else 'Custom'}</b> (units within {int(logic.tie_units_pct*100)}%, gross within {m(logic.tie_gross)}, days within {int(logic.tie_days)})."},
            {"title": "Thin stores are not ranked", "body": f"A store needs at least <b>{logic.min_store}</b> deals in a cohort to be ranked for it. Thinner stores are listed in grey for reference only."},
            {"title": "Adjusted figures shrink small samples", "body": f"Adj front / back / total / days pull a store's average toward the group-wide average for that car in proportion to how few deals it has: (n × store avg + K × group avg) ÷ (n + K), with K = <b>{int(logic.k)}</b>. A store with {int(logic.k)} deals is weighted half on its own numbers."},
            {"title": "Gross per slot per year", "body": "Adj total gross × 365 ÷ adj days to sell: what one parking spot earns in a year if you keep putting this car there. Informational unless chosen as a criterion."},
            {"title": "Similar units score", "body": f"Within the comparison pool each sold unit scores 0–1: <b>{int(SIM_WEIGHTS['options']*100)}%</b> installed options shared with the car, <b>{int(SIM_WEIGHTS['year']*100)}%</b> year closeness, <b>{int(SIM_WEIGHTS['ext']*100)}%</b> exterior color match, <b>{int(SIM_WEIGHTS['int']*100)}%</b> interior color match (weights renormalize when the car has no options or an unknown color). A store's Similar figure is the sum, shown out of its unit count; click it for the breakdown."},
            {"title": "Options match column", "body": "The average share of the incoming car's installed options (P and O) found on the store's sold units, shown next to Similar so the options effect is visible on its own."},
            {"title": "Most similar needs a vehicle", "body": "The \"Most similar units sold\" criterion only applies on Placement and VIN pages, where there is a car to compare to. On the Models and Stores pages it is skipped and the next criterion in your list applies."},
        ]),
        ("Placement", [
            {"title": "Seven fields must match exactly", "body": "The comparison pool is every sold unit that matches the incoming car exactly on <b>make, model, trim/version, manufacturer code, body, engine and fuel type</b>. Trim/version is MarketCheck's version string (e.g. CLS 450 4MATIC); the manufacturer code is the factory sales code (e.g. CLS450C4)."},
            {"title": "Same year first, then adjacent", "body": "Within the pool the same model year is used when it has enough sales; otherwise ±1 year, then ±2. Year closeness also feeds the Similar score (same year 1.0, ±1 0.7, ±2 0.4)."},
            {"title": "Mileage within 5k", "body": "Units within 5,000 miles either way of the incoming car are preferred; the band widens to 10k and then 20k only when it has too few sales. Enter mileage with each VIN; a VIN decode does not carry it."},
            {"title": "Options: match as many as possible", "body": "Every installed item MarketCheck lists is used — P (factory packages) and O (standalone options). A unit scores by the share of the car's options it also carries, packages weighing 2× standalone options. Nothing is required; more matches rank higher."},
            {"title": "Colors are a bonus", "body": "Matching exterior color adds a smaller bonus, matching interior color a smaller one still. They never exclude a unit."},
            {"title": "Fallback when the pool is thin", "body": f"If fewer than the cohort minimum match exactly, the pool relaxes to same version → same trim → same model → same make and body → same make, and the result is labelled accordingly.<ol class='list-decimal ml-5 mt-1'>{levels}</ol>"},
            {"title": "Minimum deals per level", "body": f"A level is only used when it has at least <b>{logic.min_cohort}</b> deals and at least one store that qualifies to be ranked. Otherwise the next, broader level is tried. The level used is shown with each result."},
            {"title": "150 VINs per run", "body": f"A placement run accepts up to <b>{PLACEMENT_MAX_VINS}</b> VINs (duplicates removed first). Longer lists are cut to the first {PLACEMENT_MAX_VINS} with a notice; split them into batches. This also caps how many MarketCheck calls one run can trigger."},
            {"title": "Sold by us before", "body": "If an incoming VIN has been retailed by the group inside the window, that history is shown with the recommendation."},
        ]),
        ("Display", [
            {"title": "Number one is blue", "body": "In every table the #1 store for the cohort is highlighted in light blue; the ranking criterion's column header is darkened."},
            {"title": "Store code is the Advent DealerCode", "body": "Codes like NBMBN and BHAUD are the Advent dealer codes and appear next to the store name everywhere so tables stay compact."},
            {"title": "Logic settings are per browser", "body": "The priority order and settings on the Logic tab are stored in your browser, so two people can look at the same data under different logic without affecting each other. Reset returns to the site defaults."},
        ]),
    ]


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    return templates.TemplateResponse("rules.html", ctx(request, rules=rules_for(get_logic(request))))


# ---------------------------------------------------------------- VIN page
@app.get("/vin/{vin}", response_class=HTMLResponse)
def vin_page(request: Request, vin: str, mileage: Optional[int] = None):
    vin = vin.strip().upper()
    res = place([{"vin": vin, "mileage": mileage, "raw": vin}], get_logic(request))[0]
    return templates.TemplateResponse("vin.html", ctx(request, r=res, vin=vin, mileage=mileage))


# ---------------------------------------------------------------- upload + decode
@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    with connect() as con:
        ups = [dict(r) for r in con.execute("SELECT * FROM uploads ORDER BY id DESC LIMIT 20")]
        by_store = [dict(r) for r in con.execute(
            "SELECT dealer_code, COUNT(*) n, SUM(sale_type='Lease') lease, MIN(sold_date) lo, MAX(sold_date) hi FROM deals GROUP BY dealer_code ORDER BY n DESC")]
    return templates.TemplateResponse("upload.html", ctx(request, uploads=ups, by_store=by_store))


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), decode: str = Form("0"), decode_password: str = Form("")):
    dest = DATA_DIR / "uploads" / f"{datetime.now():%Y%m%d-%H%M%S}-{Path(file.filename).name}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        rows, stats = parse_sales(dest)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(url=f"/upload?msg=Could+not+read+file:+{str(e)[:160]}", status_code=303)
    dates = [r["sold_date"] for r in rows if r["sold_date"]]
    with db() as con:
        cur = con.execute("INSERT INTO uploads (filename, uploaded_at, rows_total, rows_used, rows_kept, sold_min, sold_max, stats) VALUES (?,?,?,?,?,?,?,?)",
                          (file.filename, datetime.now().strftime("%Y-%m-%d %H:%M"), stats["total"], stats["used"], stats["kept"],
                           min(dates) if dates else None, max(dates) if dates else None, str(stats)))
        store_deals(con, rows, cur.lastrowid)
    msg = f"Loaded {stats['kept']} used retail deals ({stats['retail']} retail, {stats['lease']} lease); skipped {stats['wholesale']} wholesale, {stats['excluded_store']} from former stores"
    if decode == "1":
        if not decode_password_ok(decode_password):
            msg += ". Decode NOT started: wrong decode password"
        elif mc.enabled():
            start_backfill()
            msg += ". Decoding new VINs"
    return RedirectResponse(url="/upload?msg=" + msg.replace(" ", "+"), status_code=303)


@app.post("/decode/start")
def decode_start(limit: Optional[int] = Form(None), password: str = Form("")):
    if not decode_password_ok(password):
        return RedirectResponse(url="/upload?msg=Wrong+decode+password", status_code=303)
    started = start_backfill(limit)
    return RedirectResponse(url="/upload?msg=" + ("Decoding+started" if started else "Decode+already+running"), status_code=303)


@app.post("/decode/stop")
def decode_stop():
    JOB["cancel"] = True
    return RedirectResponse(url="/upload?msg=Stopping+after+the+current+VIN", status_code=303)


@app.post("/specs/import")
async def specs_import(file: UploadFile = File(...), password: str = Form("")):
    if not decode_password_ok(password):
        return RedirectResponse(url="/upload?msg=Wrong+decode+password", status_code=303)
    data = await file.read()
    try:
        with connect() as con:
            res = mc.import_specs(con, io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(url=f"/upload?msg=Import+failed:+{str(e)[:120].replace(' ', '+')}", status_code=303)
    msg = f"Decode cache imported: {res['added']} VINs added, {res['already_had']} already present, {res['bad']} skipped"
    return RedirectResponse(url="/upload?msg=" + msg.replace(" ", "+"), status_code=303)


@app.post("/specs/export")
def specs_export(password: str = Form("")):
    if not decode_password_ok(password):
        return RedirectResponse(url="/upload?msg=Wrong+decode+password", status_code=303)
    tmp = DATA_DIR / f"decode-cache-{datetime.now():%Y%m%d}.jsonl.gz"
    with connect() as con:
        mc.export_specs(con, tmp)
    return FileResponse(tmp, media_type="application/gzip", filename=tmp.name)


@app.get("/decode/status")
def decode_status():
    with connect() as con:
        return JSONResponse({"job": {k: v for k, v in JOB.items()}, "counts": decode_counts(con), "mc": mc.status()})


# ---------------------------------------------------------------- exports
def _xlsx(sheets: dict, name: str) -> StreamingResponse:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for sheet, d in sheets.items():
            d.to_excel(xw, sheet_name=sheet[:31], index=False)
            ws = xw.sheets[sheet[:31]]
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 48)
            ws.freeze_panes = "A2"
    buf.seek(0)
    fn = f"fj-used-{name}-{datetime.now():%Y%m%d}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{fn}"'})


def _matrix_sheet(m: dict) -> pd.DataFrame:
    rows = []
    for r in m["rows"]:
        row = {**{k.replace("_", " ").title(): (f_title(v) if k == "make" else v) for k, v in r["key"].items()}, "n": r["n"],
               "Avg total gross": r["total"], "Avg front": r["front"], "Avg back": r["back"], "Median days": r["days"],
               "#1 store": r["best"]["store_name"] if r["best"] else None,
               "Best adj gross": round(r["best"]["total_hat"]) if r["best"] else None, "Best adj days": round(r["best"]["days_hat"]) if r["best"] else None,
               "Best gross/slot/yr": round(r["best"]["annual"]) if r["best"] else None}
        for c in m["stores"]:
            cell = r["cells"].get(c)
            row[f"{c} n"] = cell["n"] if cell else None
            row[f"{c} gross"] = round(cell["total"]) if cell and cell["total"] is not None else None
            row[f"{c} days"] = round(cell["days"]) if cell and cell["days"] is not None else None
        rows.append(row)
    return pd.DataFrame(rows)


@app.get("/export.xlsx")
def export_all(request: Request, make: str = "all"):
    df = frame()
    logic = get_logic(request)
    filters = {"make": make}
    mx = lambda keys: _matrix_sheet(co.matrix(df, keys, 3, filters, logic))  # noqa: E731
    ov = pd.DataFrame(co.overview(df)).drop(columns=["top", "top_models"], errors="ignore")
    sheets = {"Stores": ov, "Model x Store": mx(["make", "model"]), "Trim x Store": mx(["make", "model", "trim"]),
              "Year Model x Store": mx(["year", "make", "model"]), "Miles x Store": mx(["make", "model", "mileage_band"]),
              "Spec x Store": mx(["make", "model", "spec"]),
              "Logic": pd.DataFrame([{"Setting": k, "Value": str(v)} for k, v in {**logic.to_dict(), "priority": logic.chain}.items()])}
    cols = ["dealer_code", "deal_number", "sale_type", "sold_date", "receive_date", "days_to_sell", "vin", "stock_no", "year", "make", "model", "trim",
            "body", "spec", "mileage", "mileage_band", "ext_base", "sold_price", "net_cost", "front_gross", "back_gross", "total_gross", "origin_code", "decoded"]
    sheets["Deals"] = df[[c for c in cols if c in df.columns]]
    return _xlsx(sheets, "analysis")
