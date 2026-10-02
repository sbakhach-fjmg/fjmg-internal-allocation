"""Background VIN decode backfill: decode every VIN in `deals` that has no cached spec yet.

Newest sales first. Stops at the per-run cap or when MarketCheck reports the monthly quota is
exhausted. Failed decodes are retried after 7 days.
"""
from __future__ import annotations
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from app.config import MARKETCHECK_MAX_PER_RUN, MARKETCHECK_WORKERS
from app.decode import marketcheck as mc


def pending_vins(con, limit: int) -> list:
    sql = """
    SELECT d.vin FROM deals d
    LEFT JOIN vin_specs s ON s.vin = d.vin
    WHERE s.vin IS NULL
       OR (s.error IS NOT NULL AND s.error NOT LIKE '%quota%' AND s.decoded_at < datetime('now', '-7 days'))
    GROUP BY d.vin
    ORDER BY MAX(d.sold_date) DESC
    LIMIT ?"""
    return [r[0] for r in con.execute(sql, (limit,)).fetchall()]


def decode_counts(con) -> dict:
    r = con.execute("""
      SELECT COUNT(*),
             SUM(CASE WHEN s.error IS NULL AND s.vin IS NOT NULL THEN 1 ELSE 0 END),
             SUM(CASE WHEN s.error IS NOT NULL THEN 1 ELSE 0 END)
      FROM (SELECT DISTINCT vin FROM deals) d LEFT JOIN vin_specs s ON s.vin = d.vin""").fetchone()
    total, ok, err = (r[0] or 0), (r[1] or 0), (r[2] or 0)
    return {"vins": total, "decoded": ok, "errors": err, "pending": total - ok - err}


def backfill(con, progress: dict, limit: int = MARKETCHECK_MAX_PER_RUN) -> dict:
    """Decode pending VINs with a small thread pool for the HTTP calls; all DB writes happen on this thread."""
    vins = pending_vins(con, limit)
    progress.update({"todo": len(vins), "done": 0, "ok": 0, "errors": 0, "started": datetime.now().strftime("%H:%M:%S"), "stopped": None})

    def fetch(vin):
        if progress.get("cancel") or not mc.enabled():
            return vin, None
        return vin, mc.decode_neovin(vin)

    with ThreadPoolExecutor(max_workers=max(1, MARKETCHECK_WORKERS)) as pool:
        for vin, payload in pool.map(fetch, vins):
            if payload is None:
                progress["stopped"] = "cancelled" if progress.get("cancel") else "marketcheck disabled or quota exhausted"
                break
            rec = mc.normalize(vin, payload)
            if rec.get("error") and "quota" in rec["error"]:
                progress["stopped"] = "quota exhausted"
                break
            mc.upsert_spec(con, rec)
            progress["done"] += 1
            if rec.get("error"):
                progress["errors"] += 1
            else:
                progress["ok"] += 1
            if progress["done"] % 25 == 0:
                con.commit()
    con.commit()
    progress["finished"] = datetime.now().strftime("%H:%M:%S")
    return dict(progress)
