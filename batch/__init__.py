"""Weekly transfer-placement batch: MSSQL -> app ranking code (unchanged) -> Tableau Server.

app/config.py creates DATA_DIR and reads the repo-root .env on import. The batch keeps nothing there,
so point DATA_DIR at a scratch folder before anything imports `app`.
"""
import os
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", str(Path(tempfile.gettempdir()) / "fj-transfer-batch"))
