"""Parse the Advent group sales workbook ('sales' sheet) into used-car deal rows.

Keeps every active store and every make. Keeps new/used == Used and Sale Type in Retail / Lease only —
wholesale deals are not retail outcomes and are dropped, as are new-car rows and stores no longer in the group.

Expected columns (header names are matched case/space-insensitively):
  DealerCode, DealNumber, new/used, Sale Type, Sold Date, VIN, StockNumber, Front Gross, Back Gross,
  Total Deal Gross, Sold Price, MSRP, Net Cost, Initial Cost, Receive Date, Mileage, Origin Code,
  Year, Make, Model, Sale Mgr Full Name, Salesperson1
"""
from __future__ import annotations
import math
import re
from pathlib import Path
import pandas as pd
from app.config import EXCLUDED_STORES

KEEP_TYPES = {"retail", "lease"}
ORIGIN = {1: "Trade in", 2: "Lease return", 3: "Auction", 4: "Factory", 5: "FJ internal", 6: "FJ store transfer", 7: "Consignment",
          9: "Street purchase", 10: "Other", 11: "External source", 13: "Group purchase", 14: "Service purchase", 15: "Lease trade",
          20: "Aftersale", 30: "CVP"}


def _key(h) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h).lower())


def _col(df: pd.DataFrame, *names: str):
    keys = {_key(c): c for c in df.columns}
    for n in names:
        if _key(n) in keys:
            return keys[_key(n)]
    return None


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else float(v)
    s = str(v).replace("$", "").replace(",", "").strip()
    if not s or s.lower() in ("nan", "none", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    try:
        f = float(s.strip("()"))
    except ValueError:
        return None
    return -f if neg else f


def _int(v):
    f = _num(v)
    return int(f) if f is not None else None


def _date(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        d = pd.to_datetime(v, errors="coerce")
    except Exception:  # noqa: BLE001
        return None
    return None if pd.isna(d) else d.to_pydatetime()


def _str(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    s = str(v).strip()
    return s or None


def load_sales_sheet(path) -> pd.DataFrame:
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        xl = pd.ExcelFile(path)
        sheet = next((s for s in xl.sheet_names if s.strip().lower() == "sales"), xl.sheet_names[0])
        return xl.parse(sheet)
    return pd.read_csv(path)


def parse_sales(path) -> tuple:
    df = load_sales_sheet(path)
    c = lambda *n: _col(df, *n)  # noqa: E731
    need = {"DealerCode": c("DealerCode", "Dealer"), "VIN": c("VIN"), "Sold Date": c("Sold Date"), "new/used": c("new/used", "NewUsed")}
    missing = [k for k, v in need.items() if v is None]
    if missing:
        raise ValueError(f"This does not look like the Advent sales export. Missing columns: {missing}. Found: {list(df.columns)[:14]}")
    cols = dict(
        dealer=need["DealerCode"], deal=c("DealNumber", "Deal Number"), nu=need["new/used"], stype=c("Sale Type"), sold=need["Sold Date"],
        vin=need["VIN"], stock=c("StockNumber", "Stock Number", "Stock#"), front=c("Front Gross"), back=c("Back Gross"),
        total=c("Total Deal Gross", "Total Gross"), price=c("Sold Price", "Sale Price"), msrp=c("MSRP"), net=c("Net Cost"),
        init=c("Initial Cost"), recv=c("Receive Date", "Received Date"), miles=c("Mileage", "Odometer"), origin=c("Origin Code"),
        year=c("Year"), make=c("Make"), model=c("Model"), mgr=c("Sale Mgr Full Name", "Sales Manager"), sp=c("Salesperson1", "Salesperson"),
    )
    stats = {"total": len(df), "new": 0, "used": 0, "kept": 0, "retail": 0, "lease": 0, "wholesale": 0, "other_type": 0, "bad_vin": 0, "dupes": 0, "excluded_store": 0}
    rows, seen = [], set()
    for rec in df.to_dict(orient="records"):
        g = lambda k: rec.get(cols[k]) if cols.get(k) else None  # noqa: E731
        if (_str(g("nu")) or "").lower() != "used":
            stats["new"] += 1
            continue
        stats["used"] += 1
        sale_type = (_str(g("stype")) or "").title()
        if sale_type.lower() == "wholesale":
            stats["wholesale"] += 1
            continue
        if sale_type.lower() not in KEEP_TYPES:
            stats["other_type"] += 1
            continue
        dealer = (_str(g("dealer")) or "").upper()
        if dealer in EXCLUDED_STORES:
            stats["excluded_store"] += 1
            continue
        vin = (_str(g("vin")) or "").upper()
        if len(vin) != 17:
            stats["bad_vin"] += 1
            continue
        deal = _str(g("deal")) or ""
        if deal.endswith(".0"):
            deal = deal[:-2]
        key = (dealer, deal, vin)
        if key in seen:
            stats["dupes"] += 1
            continue
        seen.add(key)
        sold, recv = _date(g("sold")), _date(g("recv"))
        front, back, total = _num(g("front")), _num(g("back")), _num(g("total"))
        if total is None and (front is not None or back is not None):
            total = (front or 0) + (back or 0)
        days = (sold - recv).days if sold and recv else None
        if days is not None and not (0 <= days <= 2000):
            days = None
        rows.append({
            "dealer_code": dealer, "deal_number": deal, "vin": vin, "sale_type": sale_type,
            "sold_date": sold.strftime("%Y-%m-%d") if sold else None, "receive_date": recv.strftime("%Y-%m-%d") if recv else None,
            "stock_no": _str(g("stock")), "year": _int(g("year")), "make": (_str(g("make")) or "").upper() or None,
            "model": (_str(g("model")) or "").upper() or None, "mileage": _int(g("miles")), "origin_code": _int(g("origin")),
            "sold_price": _num(g("price")), "msrp": _num(g("msrp")), "net_cost": _num(g("net")), "initial_cost": _num(g("init")),
            "front_gross": front, "back_gross": back, "total_gross": total, "days_to_sell": days,
            "sales_mgr": _str(g("mgr")), "salesperson": _str(g("sp")),
        })
        stats["kept"] += 1
        stats[sale_type.lower()] += 1
    return rows, stats


DEAL_COLS = ["dealer_code", "deal_number", "vin", "upload_id", "sale_type", "sold_date", "receive_date", "stock_no", "year", "make", "model",
             "mileage", "origin_code", "sold_price", "msrp", "net_cost", "initial_cost", "front_gross", "back_gross", "total_gross",
             "days_to_sell", "sales_mgr", "salesperson"]


def store_deals(con, rows: list, upload_id: int) -> int:
    sql = f"INSERT OR REPLACE INTO deals ({','.join(DEAL_COLS)}) VALUES ({','.join('?' for _ in DEAL_COLS)})"
    con.executemany(sql, [[{**r, "upload_id": upload_id}.get(c) for c in DEAL_COLS] for r in rows])
    return len(rows)
