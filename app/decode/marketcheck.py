"""MarketCheck API client: NeoVIN decode (trim, body, drivetrain, powertrain, colors, MSRP) with caching.

Every call needs MARKETCHECK_API_KEY. Calls are throttled, retried on 429/5xx, and stop when the API
reports the monthly quota is exhausted. Results (and errors) are cached in vin_specs so a VIN is
never decoded twice.
"""
from __future__ import annotations
import json
import threading
import time
from datetime import datetime, timedelta
import httpx
from app.config import MARKETCHECK_API_KEY, MARKETCHECK_RATE_PER_SEC

BASE = "https://mc-api.marketcheck.com/v2"
_client = httpx.Client(timeout=30)
_rate_lock = threading.Lock()
_next_slot = [0.0]
LAST_HEADERS: dict = {}
QUOTA_BLOCKED_UNTIL: list = [None]
CALLS_THIS_PROCESS = [0]


def enabled() -> bool:
    return bool(MARKETCHECK_API_KEY) and not quota_blocked()


def quota_blocked() -> bool:
    until = QUOTA_BLOCKED_UNTIL[0]
    return bool(until and datetime.utcnow() < until)


def status() -> dict:
    h = LAST_HEADERS
    return {"key": bool(MARKETCHECK_API_KEY), "blocked": quota_blocked(),
            "blocked_until": QUOTA_BLOCKED_UNTIL[0].strftime("%Y-%m-%d %H:%M UTC") if QUOTA_BLOCKED_UNTIL[0] else None,
            "quota_limit": h.get("quota-limit"), "quota_remaining": h.get("quota-remaining"), "quota_reset": h.get("quota-reset-time"),
            "rate_limit": h.get("ratelimit-limit"), "calls_this_process": CALLS_THIS_PROCESS[0]}


def _throttle():
    with _rate_lock:
        now = time.monotonic()
        slot = max(now, _next_slot[0])
        _next_slot[0] = slot + 1.0 / MARKETCHECK_RATE_PER_SEC
    wait = slot - now
    if wait > 0:
        time.sleep(wait)


def _get(path: str, **params) -> dict:
    if not enabled():
        return {"_error": "marketcheck disabled (no key or quota exhausted)"}
    params["api_key"] = MARKETCHECK_API_KEY
    r = None
    for attempt in range(4):
        _throttle()
        try:
            r = _client.get(f"{BASE}{path}", params=params)
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                return {"_error": f"request failed: {e}"}
            time.sleep(1.5 * (attempt + 1))
            continue
        CALLS_THIS_PROCESS[0] += 1
        LAST_HEADERS.update({k: v for k, v in r.headers.items() if "limit" in k or "quota" in k or "retry" in k})
        if r.status_code == 200:
            return r.json() or {}
        if r.status_code == 429 and ("quota" in r.text.lower() or r.headers.get("quota-remaining") == "0"):
            reset = r.headers.get("quota-reset-time")
            try:
                QUOTA_BLOCKED_UNTIL[0] = datetime.strptime(reset, "%Y-%m-%d %H:%M:%S UTC")
            except Exception:  # noqa: BLE001
                QUOTA_BLOCKED_UNTIL[0] = datetime.utcnow() + timedelta(hours=6)
            return {"_error": "quota exhausted", "_quota": True}
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(1.5 * (attempt + 1))
            continue
        return {"_error": f"{r.status_code}: {r.text[:200]}", "_status": r.status_code}
    return {"_error": f"request failed after retries: {r.status_code if r is not None else '?'}"}


def decode_neovin(vin: str) -> dict:
    d = _get(f"/decode/car/neovin/{vin}/specs")
    if d and "_error" not in d:
        d["_source"] = "marketcheck-neovin"
        return d
    if d.get("_quota"):
        return d
    basic = _get(f"/decode/car/{vin}/specs")
    if basic and "_error" not in basic:
        basic["_source"] = "marketcheck-specs"
        return basic
    return d or basic


def _color(v) -> tuple:
    if isinstance(v, dict):
        return (v.get("name") or None, v.get("base") or None)
    return ((v or None) if isinstance(v, str) else None, None)


def normalize(vin: str, d: dict) -> dict:
    """Map a NeoVIN / specs payload onto vin_specs columns. Errors are stored too (so we do not retry forever)."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if not d or "_error" in d:
        return {"vin": vin, "source": None, "decoded_at": now, "error": (d or {}).get("_error", "empty"), "raw": None}
    g = lambda *ks: next((d[k] for k in ks if d.get(k) not in (None, "", [], {})), None)  # noqa: E731
    ext, ext_base = _color(d.get("exterior_color"))
    inte, int_base = _color(d.get("interior_color"))
    msrp = g("oem_msrp", "original_msrp", "msrp", "combined_msrp", "mc_msrp")
    try:
        msrp = float(msrp) if msrp is not None else None
    except (TypeError, ValueError):
        msrp = None
    cyl = g("cylinders")
    try:
        cyl = int(cyl) if cyl is not None else None
    except (TypeError, ValueError):
        cyl = None
    year = g("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    packages = extract_packages(d)
    options = extract_options(d)
    # keep the raw payload minus the bulky feature lists (confidence fields stay in raw)
    slim = {k: v for k, v in d.items() if k not in ("features", "high_value_features", "installed_equipment", "warranty")}
    return {
        "vin": vin, "source": d.get("_source"), "decoded_at": now, "error": None,
        "year": year, "make": g("make"), "model": g("model"), "trim": g("trim"), "version": g("version"),
        "body_type": g("body_type"), "vehicle_type": g("vehicle_type"), "drivetrain": g("drivetrain"),
        "transmission": g("transmission"), "engine": g("engine"), "cylinders": cyl, "fuel_type": g("fuel_type"),
        "powertrain_type": g("powertrain_type"), "msrp": msrp,
        "ext_color": ext, "ext_base": ext_base, "int_color": inte, "int_base": int_base,
        "packages": json.dumps(packages), "mfr_code": g("manufacturer_code"), "options": json.dumps(options),
        "raw": json.dumps(slim, default=str),
    }


def extract_options(d: dict) -> list:
    """Every installed package (type P) and standalone option (type O): [{type, code, name, msrp}]. Colors are excluded."""
    out, seen = [], set()
    for o in d.get("installed_options_details") or []:
        if not isinstance(o, dict) or o.get("type") not in ("P", "O"):
            continue
        key = (o.get("type"), str(o.get("code") or o.get("name") or "").upper())
        if not key[1] or key in seen:
            continue
        seen.add(key)
        try:
            msrp = float(o.get("msrp")) if o.get("msrp") not in (None, "") else None
        except (TypeError, ValueError):
            msrp = None
        out.append({"type": o["type"], "code": str(o.get("code") or ""), "name": str(o.get("name") or "").strip(), "msrp": msrp})
    return out


def extract_packages(d: dict) -> list:
    """Factory package names (installed_options_details type 'P'), deduped, title-cased as MarketCheck gives them."""
    out, seen = [], set()
    for o in d.get("installed_options_details") or []:
        if isinstance(o, dict) and o.get("type") == "P" and o.get("name"):
            name = str(o["name"]).strip()
            if name.lower() not in seen:
                seen.add(name.lower()); out.append(name)
    return out


SPEC_COLS = ["vin", "source", "decoded_at", "error", "year", "make", "model", "trim", "version", "body_type", "vehicle_type",
             "drivetrain", "transmission", "engine", "cylinders", "fuel_type", "powertrain_type", "msrp",
             "ext_color", "ext_base", "int_color", "int_base", "packages", "mfr_code", "options", "raw"]


def upsert_spec(con, rec: dict):
    row = {c: rec.get(c) for c in SPEC_COLS}
    con.execute(f"INSERT OR REPLACE INTO vin_specs ({','.join(SPEC_COLS)}) VALUES ({','.join('?' for _ in SPEC_COLS)})",
                [row[c] for c in SPEC_COLS])


def decode_and_store(con, vin: str) -> dict:
    """Decode one VIN (cache first). Returns the vin_specs row as a dict."""
    vin = vin.strip().upper()
    cur = con.execute("SELECT * FROM vin_specs WHERE vin=?", (vin,)).fetchone()
    if cur and (cur["error"] is None or not enabled()):
        return dict(cur)
    rec = normalize(vin, decode_neovin(vin))
    if rec.get("error") and "quota" in rec["error"]:
        return rec if not cur else dict(cur)   # do not overwrite a cached row with a quota error
    upsert_spec(con, rec)
    con.commit()
    return rec


# ---------------------------------------------------------------- cache transfer (move decodes between installs)
import gzip


def export_specs(con, path) -> int:
    """Write every successfully decoded VIN as gzip'd JSON lines. Returns the row count."""
    rows = con.execute(f"SELECT {','.join(SPEC_COLS)} FROM vin_specs WHERE error IS NULL").fetchall()
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(dict(zip(SPEC_COLS, r)), default=str) + "\n")
    return len(rows)


def import_specs(con, fileobj) -> dict:
    """Load a cache export. Existing good rows are kept; missing or errored VINs are filled in."""
    have = {r[0] for r in con.execute("SELECT vin FROM vin_specs WHERE error IS NULL")}
    added = skipped = bad = 0
    with gzip.open(fileobj, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                bad += 1
                continue
            vin = (rec.get("vin") or "").upper()
            if len(vin) != 17 or rec.get("error"):
                bad += 1
                continue
            if vin in have:
                skipped += 1
                continue
            rec["vin"] = vin
            if rec.get("packages") is None and rec.get("raw"):
                try:
                    rec["packages"] = json.dumps(extract_packages(json.loads(rec["raw"])))
                except ValueError:
                    pass
            upsert_spec(con, rec)
            have.add(vin)
            added += 1
    con.commit()
    return {"added": added, "already_had": skipped, "bad": bad}
