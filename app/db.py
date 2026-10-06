"""SQLite storage (plain sqlite3). One file: data/used_sales.db."""
from __future__ import annotations
import sqlite3
import threading
from contextlib import contextmanager
from app.config import DATA_DIR

DB_PATH = DATA_DIR / "used_sales.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, uploaded_at TEXT,
  rows_total INTEGER, rows_used INTEGER, rows_kept INTEGER, sold_min TEXT, sold_max TEXT, stats TEXT);
CREATE TABLE IF NOT EXISTS deals (
  dealer_code TEXT NOT NULL, deal_number TEXT NOT NULL, vin TEXT NOT NULL,
  upload_id INTEGER, sale_type TEXT, sold_date TEXT, receive_date TEXT, stock_no TEXT,
  year INTEGER, make TEXT, model TEXT, mileage INTEGER, origin_code INTEGER,
  sold_price REAL, msrp REAL, net_cost REAL, initial_cost REAL,
  front_gross REAL, back_gross REAL, total_gross REAL, days_to_sell INTEGER,
  sales_mgr TEXT, salesperson TEXT,
  PRIMARY KEY (dealer_code, deal_number, vin));
CREATE INDEX IF NOT EXISTS ix_deals_vin ON deals(vin);
CREATE INDEX IF NOT EXISTS ix_deals_sold ON deals(sold_date);
CREATE TABLE IF NOT EXISTS vin_specs (
  vin TEXT PRIMARY KEY, source TEXT, decoded_at TEXT, error TEXT,
  year INTEGER, make TEXT, model TEXT, trim TEXT, version TEXT, body_type TEXT, vehicle_type TEXT,
  drivetrain TEXT, transmission TEXT, engine TEXT, cylinders INTEGER, fuel_type TEXT, powertrain_type TEXT,
  msrp REAL, ext_color TEXT, ext_base TEXT, int_color TEXT, int_base TEXT, packages TEXT, mfr_code TEXT, options TEXT, raw TEXT);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    with connect() as con:
        con.executescript(SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(vin_specs)")}
        for col in ("packages", "mfr_code", "options"):      # columns added after first release
            if col not in cols:
                con.execute(f"ALTER TABLE vin_specs ADD COLUMN {col} TEXT")
        _backfill_from_raw(con)


def _backfill_from_raw(con):
    """Fill newer columns (packages, mfr_code, options) from the stored raw decode for rows that predate them."""
    import json
    from app.decode.marketcheck import extract_packages, extract_options
    rows = con.execute("SELECT vin, raw FROM vin_specs WHERE raw IS NOT NULL AND (options IS NULL OR mfr_code IS NULL OR packages IS NULL)").fetchall()
    for vin, raw in rows:
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        con.execute("UPDATE vin_specs SET packages=COALESCE(packages, ?), mfr_code=COALESCE(mfr_code, ?), options=COALESCE(options, ?) WHERE vin=?",
                    (json.dumps(extract_packages(d)), d.get("manufacturer_code"), json.dumps(extract_options(d)), vin))
    if rows:
        con.commit()


@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def scalar(con, sql, *args):
    r = con.execute(sql, args).fetchone()
    return r[0] if r else None
