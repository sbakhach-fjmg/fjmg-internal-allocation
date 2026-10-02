"""App settings. Reads .env in the project root (KEY=value lines) and the environment."""
from __future__ import annotations
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DATA_DIR = Path(os.environ.get("DATA_DIR") or (ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "uploads").mkdir(exist_ok=True)

# Fletcher Jones stores by Advent DealerCode. Override names/zips in data/stores.json ({"CODE": {"name": "..."}}).
# Stores no longer with the group (their deals are dropped at ingest):
EXCLUDED_STORES = {"WCPOR", "FRMBN", "AUDFR", "FRPOR"}
STORES = {
    "NBMBN": {"name": "Fletcher Jones Motorcars", "zip": "92660", "brand": "Mercedes-Benz"},
    "BHMBN": {"name": "Mercedes-Benz of Beverly Hills", "zip": "90210", "brand": "Mercedes-Benz"},
    "ONMBN": {"name": "Mercedes-Benz of Ontario", "zip": "91761", "brand": "Mercedes-Benz"},
    "LVMBN": {"name": "Fletcher Jones Imports · Las Vegas", "zip": "89117", "brand": "Mercedes-Benz"},
    "HEMBN": {"name": "Mercedes-Benz of Henderson", "zip": "89014", "brand": "Mercedes-Benz"},
    "AUDFJ": {"name": "Audi Fletcher Jones · Costa Mesa", "zip": "92626", "brand": "Audi"},
    "BHAUD": {"name": "Audi Beverly Hills", "zip": "90211", "brand": "Audi"},
    "AUDLB": {"name": "Audi Long Beach", "zip": "90815", "brand": "Audi"},
    "CATOY": {"name": "Fletcher Jones Toyota", "zip": "90745", "brand": "Toyota"},
    "SMBMW": {"name": "Santa Monica BMW", "zip": "90404", "brand": "BMW"},
    "LBPOR": {"name": "Porsche Long Beach", "zip": "90815", "brand": "Porsche"},
}
_override = DATA_DIR / "stores.json"
if _override.exists():
    try:
        for code, v in json.loads(_override.read_text()).items():
            STORES.setdefault(code.upper(), {"name": code, "zip": "", "brand": ""}).update(v)
            STORES[code.upper()].pop("verify", None)
    except Exception:
        pass


def store_name(code: str) -> str:
    return STORES.get(code, {}).get("name", code)


MARKETCHECK_API_KEY = os.environ.get("MARKETCHECK_API_KEY", "")
MARKETCHECK_RATE_PER_SEC = float(os.environ.get("MARKETCHECK_RATE_PER_SEC", "5"))
MARKETCHECK_MAX_PER_RUN = int(os.environ.get("MARKETCHECK_MAX_PER_RUN", "12000"))   # cap on decodes per backfill run
MARKETCHECK_WORKERS = int(os.environ.get("MARKETCHECK_WORKERS", "4"))              # parallel decode requests (global rate cap still applies)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")      # shared sign-in; empty = open (local use only)
APP_SECRET = os.environ.get("APP_SECRET", "")          # signs the session cookie
DECODE_PASSWORD = os.environ.get("DECODE_PASSWORD", "") or APP_PASSWORD   # required to start a MarketCheck backfill; falls back to the sign-in password

# Analysis knobs
ANALYSIS_MONTHS = int(os.environ.get("ANALYSIS_MONTHS", "6"))  # rolling window back from the newest sold date; older deals stay stored but are not ranked
PRIOR_K = float(os.environ.get("PRIOR_K", "8"))        # shrinkage strength: a store with n deals is weighted n/(n+K) vs the cohort mean
MIN_N_STORE = int(os.environ.get("MIN_N_STORE", "3"))  # deals a store needs at a cohort level to be ranked
MIN_N_BEST = int(os.environ.get("MIN_N_BEST", "5"))    # deals a store needs before its gross can override a higher-volume store
GROSS_OVERRIDE_PCT = float(os.environ.get("GROSS_OVERRIDE_PCT", "0"))      # optional extra: challenger must also beat the leader by this share (0 = off)
GROSS_OVERRIDE_ABS = float(os.environ.get("GROSS_OVERRIDE_ABS", "2000"))  # ...and by at least this many dollars per unit
MIN_N_COHORT = int(os.environ.get("MIN_N_COHORT", "5"))  # retail deals a cohort level needs before we trust it
MIN_DAYS = 10                                          # floor on days-to-sell when annualizing (avoids divide-by-tiny)
