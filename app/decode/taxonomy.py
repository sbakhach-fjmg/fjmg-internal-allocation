"""Canonical vehicle taxonomy used by every analysis: year / make / model / trim / mileage band / spec.

Year and make come from the Advent deal (always present). Model, trim, body, drivetrain and powertrain
come from the MarketCheck NeoVIN decode when we have it; otherwise from the Advent Model column
(Mercedes sales codes such as GLC300W4 are expanded so they line up roughly with NeoVIN naming).
"""
from __future__ import annotations
import re

UNKNOWN = "(unknown)"

MILEAGE_BANDS = [(0, 10_000, "0-10k"), (10_000, 20_000, "10-20k"), (20_000, 35_000, "20-35k"), (35_000, 50_000, "35-50k"),
                 (50_000, 75_000, "50-75k"), (75_000, 100_000, "75-100k"), (100_000, 10**9, "100k+")]
BAND_ORDER = [b[2] for b in MILEAGE_BANDS] + [UNKNOWN]


def mileage_band(m) -> str:
    try:
        m = float(m)
    except (TypeError, ValueError):
        return UNKNOWN
    for lo, hi, label in MILEAGE_BANDS:
        if lo <= m < hi:
            return label
    return UNKNOWN


# ---- Mercedes-Benz sales code fallback (only used when a VIN has not been decoded) ----
_MB_RE = re.compile(r"^(?P<cls>[A-Z]+?)(?P<num>\d{2,3})(?P<plus>\+?)(?P<body>[A-Z]*?)(?P<four>4?)(?P<ev>E?)$")
_MB_CLASS_MODEL = {"C": "C-Class", "E": "E-Class", "S": "S-Class", "G": "G-Class", "A": "A-Class", "B": "B-Class", "CLA": "CLA", "CLE": "CLE",
                   "CLS": "CLS", "GLA": "GLA", "GLB": "GLB", "GLC": "GLC", "GLE": "GLE", "GLS": "GLS", "SL": "SL", "GT": "AMG GT",
                   "EQA": "EQA", "EQB": "EQB", "EQC": "EQC", "EQE": "EQE", "EQS": "EQS", "EQG": "G-Class"}
_SUV = {"GLA", "GLB", "GLC", "GLE", "GLS", "G", "EQB", "EQC", "EQG"}
_EV = {"EQA", "EQB", "EQC", "EQE", "EQS", "EQG"}
_AMG = {"35", "43", "45", "53", "55", "63"}


def mb_from_code(code: str) -> dict:
    """GLC300W4 -> model GLC, trim 'GLC 300', body SUV, drivetrain 4WD, powertrain Combustion."""
    out = {"model": code, "trim": UNKNOWN, "body": UNKNOWN, "drivetrain": UNKNOWN, "powertrain": UNKNOWN}
    c = (code or "").strip().upper()
    if c.startswith("AMG") and len(c) > 5:
        c = c[3:]
    m = _MB_RE.match(c)
    if not m:
        if c.startswith(("MMPV", "SPRINTER", "METRIS")) or re.match(r"^[DM][A-Z0-9]{5}$", c):
            out.update({"model": "Van", "body": "Van", "powertrain": "Combustion"})
        return out
    cls, num, plus, body, four, ev = m.group("cls", "num", "plus", "body", "four", "ev")
    b = body.replace("E", "")[:1]
    electrified = ev == "E" or "E" in body or cls in _EV
    amg = num in _AMG or cls == "GT"
    if cls in _EV:
        body_name = "Sedan" if b in ("V", "") else "SUV"
    else:
        body_name = {"W": "SUV" if cls in _SUV else "Sedan", "C": "Coupe", "A": "Convertible", "V": "Sedan", "X": "SUV", "R": "Convertible",
                     "S": "Wagon", "Z": "SUV" if cls in _SUV else "Sedan"}.get(b, "SUV" if cls in _SUV else "Sedan")
    if cls == "GT" and b == "":
        body_name = "Coupe"
    powertrain = "BEV" if cls in _EV or (electrified and not amg) else ("PHEV" if electrified else "Combustion")
    out.update({"model": _MB_CLASS_MODEL.get(cls, cls), "trim": f"{'AMG ' if amg else ''}{cls} {num}{plus}".strip(),
                "body": body_name, "drivetrain": "4WD" if four == "4" or cls == "G" else "2WD", "powertrain": powertrain})
    return out


_DRIVE = {"4WD": "4WD", "AWD": "4WD", "4X4": "4WD", "FWD": "2WD", "RWD": "2WD", "2WD": "2WD"}


def norm_drive(v) -> str:
    if not v:
        return UNKNOWN
    return _DRIVE.get(str(v).strip().upper(), str(v).strip().upper())


def norm_power(v) -> str:
    if not v:
        return UNKNOWN
    s = str(v).strip()
    return {"ICE": "Combustion", "GAS": "Combustion", "GASOLINE": "Combustion", "DIESEL": "Combustion", "EV": "BEV"}.get(s.upper(), s)


_BODY_WORDS = ("Sedan", "Coupe", "Cabriolet", "Convertible", "Wagon", "Hatchback", "Roadster")
_DRIVE_TOKENS = re.compile(r"\b(4MATIC\+?|4Matic\+?|AWD|4WD|RWD|FWD|4x4|4X4)\b")
_MB_TRIM = re.compile(r"^(AMG\s+)?([A-Z]{1,3})\s?(\d{2,3})(\+?)([a-zA-Z]?)(\b|$)")


def clean_model(make: str, model: str) -> str:
    """'C-Class Sedan' -> 'C-Class' (body is its own dimension). Keeps 'EQS SUV' / 'Bronco 4-Door' (distinct models)."""
    m = (model or "").strip()
    for w in _BODY_WORDS:
        if m.endswith(" " + w) and len(m) > len(w) + 1:
            m = m[: -len(w) - 1].strip()
    return m or UNKNOWN


def clean_trim(make: str, trim, version) -> str:
    """Mercedes: 'C300' -> 'C 300'; 'Base' + version 'EQS 450+ 4MATIC' -> 'EQS 450+'. Others: trim, else version."""
    t = (trim or "").strip()
    v = (version or "").strip()
    is_mb = "MERC" in (make or "").upper()
    if is_mb:
        if (not t or t.lower() in ("base", "standard")) and v:
            t = v
        t = _DRIVE_TOKENS.sub("", t).replace("  ", " ").strip(" -")
        m = _MB_TRIM.match(t)
        if m:
            t = f"{m.group(1) or ''}{m.group(2)} {m.group(3)}{m.group(4)}{m.group(5).lower()}{t[m.end():]}".strip()
        t = t.replace("Mercedes-AMG ", "AMG ")
    if not t:
        t = v or UNKNOWN
    return t


def canonical(deal: dict, spec: dict) -> dict:
    """Return year, make, model, trim, body, drivetrain, powertrain, spec, mileage_band, decoded, ext_base."""
    year = deal.get("year")
    make = (deal.get("make") or (spec or {}).get("make") or UNKNOWN).upper()
    decoded = bool(spec) and spec.get("error") is None and bool(spec.get("model"))
    if decoded:
        model = clean_model(make, spec.get("model"))
        trim = clean_trim(make, spec.get("trim"), spec.get("version"))
        body = spec.get("body_type") or spec.get("vehicle_type") or UNKNOWN
        drive = norm_drive(spec.get("drivetrain"))
        power = norm_power(spec.get("powertrain_type"))
        ext = spec.get("ext_base") or UNKNOWN
    else:
        raw_model = deal.get("model") or UNKNOWN
        if "MERC" in make:
            mb = mb_from_code(raw_model)
            model, trim, body, drive, power = mb["model"], mb["trim"], mb["body"], mb["drivetrain"], mb["powertrain"]
        else:
            model, trim, body, drive, power = raw_model.title(), UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN
        ext = UNKNOWN
    spec_key = f"{drive} · {power}"
    return {"year": int(year) if year else 0, "make": make, "model": str(model), "trim": str(trim), "body": str(body),
            "drivetrain": drive, "powertrain": power, "spec": spec_key, "mileage_band": mileage_band(deal.get("mileage")),
            "decoded": decoded, "ext_base": ext}
