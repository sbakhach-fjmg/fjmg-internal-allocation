# FJ Used Sales Analyzer

Internal web app for Fletcher Jones: upload the Advent group sales export, decode every used VIN
through MarketCheck, and see which used cars make the most gross and turn fastest at which store.
Each week, paste the list of incoming vehicles and get a ranked store recommendation per VIN.

Separate from every other FJ tool: its own code, database, MarketCheck cache and deployment.

## What it does

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
   - **Ranking rule**: the store that has sold the most of that car is #1, and the rest follow in volume
     order. Another store takes #1 only when its adj gross per unit beats the volume leader's by at least
     `GROSS_OVERRIDE_ABS` ($2,000) — optionally also by `GROSS_OVERRIDE_PCT` (default off) — with at least
     `MIN_N_BEST` (5) sales of its own.
4. **Logic tab** — the user puts the criteria in priority order (remembered per browser): most units sold
   (default first, with the gross override), best total / front / back gross, gross per slot per year,
   fastest turn, most similar units sold (attribute + package overlap with the vehicle being placed).
   The first criterion decides; stores within the tie band on it (units within 15%, gross within $500,
   days within 5) count as tied and the next criterion breaks the tie, and so on. Tie bands, min deals,
   shrinkage K and override margins are editable there too. The page also documents every formula.
5. **Place** this week's list: each VIN is decoded, matched to the most specific cohort with enough
   history (`MIN_N_COHORT` deals, `MIN_N_STORE` at a store), and the stores are ranked. Export to Excel.

Pages (in nav order): **Placement** (home) · **Models** (cohort × store matrix, filter by year / make /
model, group by model / trim / year / miles / spec) · model detail (year / trim / mileage / spec / body /
color / factory package breakdowns) · **Stores** (store summary, where each store wins, units by month)
· **Logic** (ranking criterion + thresholds + formulas) · **Data** (uploads, decode status/quota) · `/vin/<VIN>` lookup · `/export.xlsx` full analysis workbook.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # set MARKETCHECK_API_KEY (and APP_PASSWORD/APP_SECRET if exposed)
./run.sh                 # http://localhost:8010
```

CLI: `python -m app.cli load <file>` · `python -m app.cli decode [limit]` · `python -m app.cli status`.

## Deploy to Railway

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

## Layout

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
