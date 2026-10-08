"""Where the batch reads sales, candidates and decodes from, and saves new decodes to.

Mssql is the production source. LocalFolder reads the same tables from parquet files, for trying the
pipeline without database access and for re-running a past run from its snapshot.
"""
from __future__ import annotations
from decimal import Decimal
from pathlib import Path

import pandas as pd

from batch.decode import SPEC_TABLE_COLS

SQL_DIR = Path(__file__).resolve().parent / "sql"
SPECS_TABLE = "dbo.vin_specs"
_COLS = ", ".join(f"[{c}]" for c in SPEC_TABLE_COLS)   # year / trim are T-SQL function names
_READ_COLS = ", ".join(f"[{c}]" for c in SPEC_TABLE_COLS if c != "raw")


class Mssql:
    def __init__(self, connection_string: str):
        from mssql_python import connect
        self.con = connect(connection_string)

    def read(self, name: str) -> pd.DataFrame:
        """Run batch/sql/<name>.sql."""
        return self._query((SQL_DIR / f"{name}.sql").read_text())

    def read_specs(self) -> pd.DataFrame:
        """Every decode except the bulky raw payload, which the ranking never reads."""
        df = self._query(f"SELECT {_READ_COLS} FROM {SPECS_TABLE}")
        df["raw"] = None
        return df[SPEC_TABLE_COLS]

    def save_specs(self, rows: pd.DataFrame):
        if rows.empty:
            return
        cur = self.con.cursor()
        recs = rows[SPEC_TABLE_COLS].astype(object).where(rows[SPEC_TABLE_COLS].notna(), None).values.tolist()
        for i in range(0, len(recs), 500):
            chunk = recs[i:i + 500]
            cur.execute(f"DELETE FROM {SPECS_TABLE} WHERE vin IN ({', '.join('?' for _ in chunk)})", [r[0] for r in chunk])
            cur.executemany(f"INSERT INTO {SPECS_TABLE} ({_COLS}) VALUES ({', '.join('?' for _ in SPEC_TABLE_COLS)})", chunk)
        self.con.commit()

    def _query(self, sql: str) -> pd.DataFrame:
        cur = self.con.cursor()
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        df = pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()], columns=cols)
        for c in df.columns:   # money/decimal columns arrive as Decimal, which SQLite and parquet consumers choke on
            s = df[c].dropna()
            if len(s) and s.map(lambda v: isinstance(v, Decimal)).all():
                df[c] = df[c].astype(float)
        return df


class LocalFolder:
    """<dir>/sales.parquet, transfer_candidates.parquet, vin_specs.parquet."""

    def __init__(self, folder: Path):
        self.dir = Path(folder)

    def read(self, name: str) -> pd.DataFrame:
        return pd.read_parquet(self.dir / f"{name}.parquet")

    def read_specs(self) -> pd.DataFrame:
        p = self.dir / "vin_specs.parquet"
        return pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=SPEC_TABLE_COLS)

    def save_specs(self, rows: pd.DataFrame):
        if rows.empty:
            return
        from batch.decode import merge_specs
        merge_specs(self.read_specs(), rows).to_parquet(self.dir / "vin_specs.parquet", index=False)
