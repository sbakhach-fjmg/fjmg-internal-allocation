"""Command line helpers:  python -m app.cli load <file>   |   python -m app.cli decode [limit]   |   python -m app.cli status"""
from __future__ import annotations
import sys
from datetime import datetime
from app.db import init_db, db, connect
from app.ingest.sales import parse_sales, store_deals
from app.analysis.enrich import backfill, decode_counts
from app.decode import marketcheck as mc


def main(argv):
    init_db()
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "load":
        rows, stats = parse_sales(argv[2])
        dates = [r["sold_date"] for r in rows if r["sold_date"]]
        with db() as con:
            cur = con.execute("INSERT INTO uploads (filename, uploaded_at, rows_total, rows_used, rows_kept, sold_min, sold_max, stats) VALUES (?,?,?,?,?,?,?,?)",
                              (argv[2], datetime.now().strftime("%Y-%m-%d %H:%M"), stats["total"], stats["used"], stats["kept"],
                               min(dates) if dates else None, max(dates) if dates else None, str(stats)))
            store_deals(con, rows, cur.lastrowid)
        print(stats)
    elif cmd == "decode":
        limit = int(argv[2]) if len(argv) > 2 else 12000
        con = connect()
        prog = {}
        res = backfill(con, prog, limit=limit)
        print(res, mc.status())
    with connect() as con:
        print("decode counts:", decode_counts(con))


if __name__ == "__main__":
    main(sys.argv)
