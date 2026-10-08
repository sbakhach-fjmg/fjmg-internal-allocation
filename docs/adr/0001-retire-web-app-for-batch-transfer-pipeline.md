# Retire the web app in favour of a weekly batch pipeline into Tableau

The store-ranking logic was built as a self-hosted web app (FastAPI, Docker, Caddy). Nobody on the analytics team runs web services, but everyone runs Alteryx, MSSQL and Tableau Server. So we run the unchanged ranking modules (`app/analysis/cohorts.py`, `app/decode/taxonomy.py`) from a weekly Python script that reads from MSSQL and publishes to Tableau, and we retire the web app. We give up the per-browser editable Logic tab, on-demand VIN lookups and the Models/Stores exploration pages. In return the user no longer pastes VINs or uploads sales files, and the ranking rule becomes a single YAML file owned by analytics.

## Considered options

- **Host the web app on switch-dagster.** The Docker Compose setup already existed, so deploying was cheap. Owning, patching and supporting it was the cost we didn't want.
- **Rewrite the ranking logic in SQL or Alteryx.** Rejected. The matching relies on exact normalized-string equality and has no tests, so a rewrite would drift without anyone noticing. We reuse the Python modules as they are instead.

## Consequences

- To change the ranking you edit `batch/ranking_rule.yaml`. There is one rule for everyone.
- Keep the web layer until the batch output matches the app's on the same data (see `docs/transfer-pipeline-plan.md`). Then delete it in one commit.
