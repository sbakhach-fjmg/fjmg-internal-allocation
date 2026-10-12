"""Write the outputs as .hyper extracts and publish them to Tableau Server, overwriting the previous run."""
from __future__ import annotations
import os
from pathlib import Path

import pandas as pd


def write_hyper(df: pd.DataFrame, path: Path):
    import pantab
    pantab.frame_to_hyper(df, path, table=("Extract", "Extract"))


def publish(paths: list):
    """Publish each .hyper as a data source named after the file, into TABLEAU_PROJECT."""
    import tableauserverclient as TSC
    env = os.environ
    auth = TSC.PersonalAccessTokenAuth(env["TABLEAU_PAT_NAME"], env["TABLEAU_PAT_SECRET"], site_id=env.get("TABLEAU_SITE", ""))
    server = TSC.Server(env["TABLEAU_SERVER_URL"], use_server_version=True)
    with server.auth.sign_in(auth):
        projects = list(TSC.Pager(server.projects))
        want = env["TABLEAU_PROJECT"].strip().casefold()   # the built-in project shows as "Default"
        project = next((p for p in projects if p.name.casefold() == want), None)
        if project is None:
            raise RuntimeError(f"Tableau project {env['TABLEAU_PROJECT']!r} not found; projects visible to this token: "
                               f"{sorted(p.name for p in projects)}")
        for p in paths:
            item = TSC.DatasourceItem(project.id, name=Path(p).stem)
            server.datasources.publish(item, str(p), TSC.Server.PublishMode.Overwrite)
