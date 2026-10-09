# FJ used vehicle transfer placement

Weekly batch that tells Fletcher Jones which store a used car aging on one lot should go to. For every used
car in stock it finds comparable sold cars (exact make/model/version/engine match first, widening only when
too few), scores each store on how those cars sold there, ranks the stores with one fixed ranking rule, and
publishes the result to Tableau Server. The author filters on days in stock, clicks a car, and sees its
ranked stores, the reason for each rank, and the deals behind it.

It started as a web app (still in `app/`, being retired: [ADR-0001](docs/adr/0001-retire-web-app-for-batch-transfer-pipeline.md)).
The batch reuses the app's ranking code unchanged. Terms: [CONTEXT.md](CONTEXT.md). Full design and the open
checks: [docs/transfer-pipeline-plan.md](docs/transfer-pipeline-plan.md).

## How a run works

`python -m batch.run` (Alteryx Run Command, weekly):

1. **Read** sales (last 6 months of used Retail/Lease deals), used cars in stock, and stored VIN decodes from
   MSSQL: `batch/sql/sales.sql`, `batch/sql/transfer_candidates.sql`, table `dbo.vin_specs`.
2. **Decode** VINs (sales and stock) not yet in `vin_specs` through MarketCheck and store them. Stops cleanly
   at the monthly quota; a VIN that fails 3 times is given up.
3. **Snapshot** the inputs to `runs/<date>/inputs/` so any week can be replayed.
4. **Rank** stores for every car with `app/analysis/cohorts.py` and the rule in `batch/ranking_rule.yaml`.
5. **Write** one table to `runs/<date>/transfer_placements.parquet`, plus a copy at a fixed path,
   `runs/transfer_placements_latest.parquet`, overwritten each run (see *Exploring the output*).
6. **Publish** it as the `transfer_placements` data source on Tableau Server (overwritten each week).

Exit code `0` ok, `1` publishing failed (local outputs still written), `2` anything else. Details in
`runs/<date>/run.log`, including decode coverage: below 98% of in-window sales decoded, the output carries a
`coverage_warning`.

## The Tableau table

One data source, one row per **car × comparable deal**. Car and store columns repeat on each deal row; cars
with status `no decode` or `no comparables` keep one row with empty store and deal columns.

- Car: `vin`, `stock_no`, `current_store`, `days_in_stock`, `mileage`, `year`/`make`/`model`, decoded `trim`/`version`/...
- Result: `status`, `action` ("Keep at current store" / "Transfer to X" / "No ranked store"), `top_store`,
  `current_store_rank`, `match_level`, `thin`, `pool_n`
- Store: `store`, `rank` (empty = fewer than 3 deals, not ranked), `is_current_store`, `n`, `sum_total`,
  `total_hat`, `days_hat`, `why`, ...
- Deal: `deal_vin`, `deal_sold_date`, `deal_total_gross`, `deal_days_to_sell`, ...

**Store measures repeat once per deal.** Filter on `store_row = True` (one row per car × store), or use
`MIN`/`ATTR`, before summing or averaging `n`, `sum_total`, `total_hat` and the other store columns.
Column list: [plan, "Output: one table"](docs/transfer-pipeline-plan.md#output-one-table).

## Exploring the output

The table published to Tableau is also saved locally as parquet, identical column for column:

- `runs/transfer_placements_latest.parquet`: always the latest run. Point notebooks, Alteryx or Power BI here.
  Move it with `--latest PATH` (e.g. a shared drive).
- `runs/<date>/transfer_placements.parquet`: that week's copy, kept for history.

```python
import pandas as pd
t = pd.read_parquet(r"runs\transfer_placements_latest.parquet")
cars = t.drop_duplicates("vin")                     # one row per car: action, top_store, match_level, ...
stores = t[t["store_row"]]                          # one row per car x store: rank, n, sum_total, why, ...
```

## Changing the ranking rule

Edit [batch/ranking_rule.yaml](batch/ranking_rule.yaml) (criteria order, shrinkage `k`, minimum deal counts,
tie bands, sales window). Every value is commented. Typos and unknown criteria stop the run instead of being
ignored. Changes go through analytics; run the tests after editing:

```
.venv\Scripts\python -m pytest batch
```

## Setup (Alteryx machine)

```
python -m venv .venv
.venv\Scripts\pip install -r batch\requirements.txt
copy .env.example .env
```

Fill in `.env`: `MSSQL_CONNECTION` (Windows integrated auth, `Trusted_Connection=yes`), `MARKETCHECK_API_KEY`,
`TABLEAU_SERVER_URL`, `TABLEAU_SITE`, `TABLEAU_PROJECT`, `TABLEAU_PAT_NAME`, `TABLEAU_PAT_SECRET`.

One-time: run `batch/sql/vin_specs.sql` in MSSQL, then load the existing decodes:

```
.venv\Scripts\python -m batch.run seed-decodes path\to\decode-cache-2026-10-02.jsonl.gz
```

Before relying on the output, work through the SQL checks in the plan (store codes match between sales and
inventory, make spelling matches the decodes, `RS Date` / `Age In Invt` meanings).

## Running it

From the repo folder (Alteryx Run Command: working directory = repo):

```
.venv\Scripts\python.exe -m batch.run
```

| Flag | Use |
|---|---|
| `--skip-decode` | no MarketCheck calls this run |
| `--skip-publish` | write `runs/<date>/` only, don't touch Tableau |
| `--decode-cap N` | decode at most N VINs (default 12,000) |
| `--latest PATH` | where to write the fixed-path parquet copy (default `runs/transfer_placements_latest.parquet`) |
| `--local DIR` | read `DIR/sales.parquet`, `transfer_candidates.parquet`, `vin_specs.parquet` instead of MSSQL; `--local runs/<date>/inputs` replays that week |

## Batch layout

```
batch/run.py               entry point: the six steps, plus seed-decodes
batch/rule.py              loads ranking_rule.yaml strictly
batch/ranking_rule.yaml    the one ranking rule
batch/place.py             app ranking code per car, builds the one output table
batch/decode.py            seed / top up vin_specs, decode coverage
batch/sources.py           MSSQL or a folder of parquet files
batch/publish.py           .hyper (pantab) and Tableau Server publish (tableauserverclient)
batch/sql/                 sales, transfer candidates, vin_specs DDL
batch/tests/               ranking-rule tests
runs/                      weekly snapshots, logs and the latest parquet (git-ignored)
```

## Legacy web app (being retired)

Kept until the batch output is confirmed to match it, then removed in one commit.

### What the web app does

1. **Upload** `inventory_sales_report.xlsx` (Advent, group-wide, `sales` sheet). Keeps used
   **Retail + Lease** deals from active stores only. Wholesale deals, new cars and former stores
   (WCPOR, FRMBN, AUDFR, FRPOR) are dropped. Deals are keyed on store + deal number + VIN, so
   re-uploading each month just merges; the analysis uses a rolling window (`ANALYSIS_MONTHS`, default 6)
   ending at the newest sold date.
2. **Decode** every VIN once with MarketCheck NeoVIN: model, trim, body, drivetrain, powertrain,
   colors, MSRP, and factory package names (Night Package, Premium Package, AMG Line, ...). Cached forever in `vin_specs`; only new VINs cost API calls. The backfill runs in the
   background after an upload (or from the Data page) and stops cleanly if the monthly quota runs out.
3. **Analyze** cohorts (year · make · model · trim · mileage band · spec) by store:
   - *Adj total* — average total deal gross (front + back), shrunk toward the cohort mean with prior
     weight `PRIOR_K` so thin samples cannot win on luck
   - *Adj days* — average receive→sold days, shrunk the same way
   - *Gross / slot / yr* — adj total × 365 / adj days: what one inventory slot earns per year (information)
   - **Ranking rule (default)**: the store with the most total gross dollars on that car (avg × units) is #1;
     most units sold breaks ties. If "Most units sold" is put first, another store takes #1 only when its adj
     gross per unit beats the volume leader's by `GROSS_OVERRIDE_ABS` ($2,000) with `MIN_N_BEST` (5) sales.
4. **Logic tab** — the user puts the criteria in priority order (remembered per browser): most units sold
   (default first, with the gross override), best total / front / back gross, gross per slot per year,
   fastest turn, most similar units sold (attribute + package overlap with the vehicle being placed).
   The first criterion decides; stores within the tie band on it (units within 15%, gross within $500,
   days within 5) count as tied and the next criterion breaks the tie, and so on. Tie bands, min deals,
   shrinkage K and override margins are editable there too. The page also documents every formula.
5. **Place** this week's list (key packages such as AMG Line / Night / Premium must match first; options are
   kept while miles, spec and year are relaxed, then dropped): each VIN is decoded, matched to the most specific cohort with enough
   history (`MIN_N_COHORT` deals, `MIN_N_STORE` at a store), and the stores are ranked. Export to Excel.

Pages (in nav order): **Placement** (home) · **Models** (cohort × store matrix, filter by year / make /
model, group by model / trim / year / miles / spec) · model detail (year / trim / mileage / spec / body /
color / factory package breakdowns) · **Stores** (store summary, where each store wins, units by month)
· **Logic** (priority order + settings) · **Rules** (every rule and formula, with dropdown descriptions) · **Data** (uploads, decode status/quota) · `/vin/<VIN>` lookup · `/export.xlsx` full analysis workbook.

### Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # set MARKETCHECK_API_KEY (and APP_PASSWORD/APP_SECRET if exposed)
./run.sh                 # http://localhost:8010
```

CLI: `python -m app.cli load <file>` · `python -m app.cli decode [limit]` · `python -m app.cli status`.

### Deploy to Railway

1. Push this folder to a Git repo (`.env` and `data/` are ignored) and create a Railway project from it.
   The `Dockerfile` + `railway.json` are picked up automatically (health check `/healthz`).
2. **Volume**: add a volume mounted at `/data` (SQLite db, uploads, MarketCheck cache live there).
3. **Variables**: `MARKETCHECK_API_KEY`, `APP_PASSWORD` (sign-in), `APP_SECRET` (long random string),
   `DECODE_PASSWORD` (needed to start a VIN backfill from the Data page; defaults to `APP_PASSWORD`),
   `DATA_DIR=/data`. Optional: `ANALYSIS_MONTHS`, `PRIOR_K`, `MIN_N_STORE`, `MIN_N_BEST`, `MIN_N_COHORT`,
   `GROSS_OVERRIDE_PCT`, `GROSS_OVERRIDE_ABS`, `MARKETCHECK_WORKERS`,
   `MARKETCHECK_RATE_PER_SEC`, `MARKETCHECK_MAX_PER_RUN`.
4. **Domain**: in Railway add a custom domain (e.g. `used.<your-domain>`), then add the CNAME it gives you
   in the WordPress.com DNS for the domain. Link to it from a WordPress page or menu item.
5. First visit: sign in, upload the Advent file on **Data**, let the decode backfill finish.

Never run it on the internet with `APP_PASSWORD` empty.

### Run on the internal VM (Docker Compose)

Runs on `switch-dagster` with Docker Compose: the app plus Caddy in front for HTTPS at
`https://switch-dagster`. The app's own port is not published.

1. **Clone** with a read-only deploy key, then `cd` into the repo.
2. **`.env`**: `cp .env.example .env`, set `MARKETCHECK_API_KEY`, `APP_PASSWORD`, `DECODE_PASSWORD` and
   `APP_SECRET` (`openssl rand -hex 32`), then `chmod 600 .env`. HTTPS is required: the login cookie is
   `Secure` whenever `APP_SECRET` is set, so plain `http://` sign-in loops back to `/login`.
3. **Start**: `docker compose up -d --build`. Check with `docker compose ps` (app healthy) and
   `curl -k --resolve switch-dagster:443:127.0.0.1 https://switch-dagster/healthz`.
4. **Trust the certificate** (Caddy `tls internal`, no IT needed). On the VM:
   `docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt`.
   On each user's Windows machine (no admin): `certutil -user -addstore Root caddy-root.crt`.
   Edge and Chrome use this store; Firefox needs its own import.
5. **Update**: `git pull && docker compose up -d --build`.

Keep `./data` (SQLite db, uploads, MarketCheck cache) and the `caddy_data` volume (Caddy's certificate
authority). Never run `docker compose down -v`: it deletes `caddy_data`, and every user would have to trust
a new certificate.

### Web app layout

```
app/main.py              routes, placement recommender, Excel exports
app/config.py            .env settings, store list (override names in data/stores.json), excluded stores
app/db.py                SQLite schema: uploads, deals, vin_specs
app/ingest/sales.py      Advent sales parser (used · retail/lease · active stores)
app/decode/marketcheck.py MarketCheck client: NeoVIN decode, throttle, quota, cache
app/decode/taxonomy.py   canonical year/make/model/trim/body/drivetrain/powertrain/mileage band/spec
app/analysis/cohorts.py  cohort × store scoring, shrinkage, ranking, matrices, recommender
app/analysis/enrich.py   background VIN decode backfill
app/cli.py               load / decode / status from the command line
app/templates/           Jinja2 pages (Tailwind via CDN)
data/                    SQLite db, uploads (not source; on Railway this is the /data volume)
```
