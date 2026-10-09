# Used vehicle transfer pipeline: plan

**Status:** built, awaiting SQL checks and a first run on the Alteryx machine · **Owner:** Shafeek · **Decision record:** [ADR-0001](adr/0001-retire-web-app-for-batch-transfer-pipeline.md) · **Glossary:** [CONTEXT.md](../CONTEXT.md)

## What it's for

The author reviews used vehicles that are getting old on a store's lot (high **days in stock**). For each one they want to know which store has sold cars like it best, and why. The web app answered that, but only after someone pasted VINs and uploaded an Advent export, and we'd have to host it.

Instead, a weekly script ranks every used vehicle in stock against six months of sales, using the app's own ranking code unchanged. It publishes the results to Tableau Server. The author filters on days in stock, clicks a car, and sees a detail screen with its ranked stores, the reason for each rank, and the deals behind it.

## How it runs

```
Alteryx (weekly schedule)
  └─ Run Command: <repo>\.venv\Scripts\python.exe -m batch.run
       1. decode     new VINs (sales + stock) → MarketCheck → MSSQL vin_specs
       2. extract    MSSQL → sales.sql, transfer_candidates.sql, vin_specs
       3. snapshot   runs\<date>\inputs\*.parquet
       4. place      app/analysis/cohorts.py + app/decode/taxonomy.py (unchanged), rule from ranking_rule.yaml
       5. write      runs\<date>\transfer_placements.parquet + runs\transfer_placements_latest.parquet
       6. publish    one .hyper (pantab) → Tableau Server data source `transfer_placements`, overwrite
```

- **Credentials.** MSSQL uses Windows integrated auth through `mssql-python`, under the account that runs Alteryx (`MSSQL_CONNECTION` with `Trusted_Connection=yes`). The MarketCheck key and the Tableau personal access token go in the repo-root `.env` (see `.env.example`), readable only by that account.
- **Failures.**
  - A VIN that can't be decoded doesn't stop the run. It appears in the output with status `no decode`.
  - A failure to publish to Tableau returns a non-zero exit code, so Alteryx marks the run as failed. The local snapshot is still written.
- **History.** Tableau Server always holds the latest run only. Each week's inputs and outputs stay in `runs\<date>\` for audits ("why did it say X last Tuesday?").

## Code layout

The batch code lives in this repo, next to the code it reuses:

```
batch/
  run.py                    entry point, the six stages above; also `seed-decodes`
  rule.py                   loads ranking_rule.yaml strictly (typos are errors, not ignored)
  ranking_rule.yaml         the one ranking rule (loaded with cohorts.Logic.from_dict)
  place.py                  sales frame via the app's load_frame, rank_stores per candidate, one output table
  decode.py                 seed + top up vin_specs, decode coverage
  sources.py                MSSQL (production) or a folder of parquet files (--local)
  publish.py                build .hyper, publish to Tableau Server
  sql/sales.sql             sold used Retail/Lease deals → `deals` columns
  sql/transfer_candidates.sql   used vehicles in stock
  sql/vin_specs.sql         one-time DDL for the MSSQL decode table
  tests/test_rule.py        ranking-rule loading
  requirements.txt          pinned batch dependencies
```

Reuse these functions instead of rewriting them:

| Need | Existing code |
|---|---|
| Sales frame with decodes, canonical fields, 6-month window | `cohorts.load_frame` ([cohorts.py:223](../app/analysis/cohorts.py)). Give it a SQLite connection holding `deals` + `vin_specs` built from the parquet snapshot, so it runs exactly as in the app. |
| Decode → comparable vehicle | `cohorts.vehicle_from_spec(spec, mileage)` |
| Comparable pool + store ranking + reasons | `cohorts.rank_stores(df, vehicle, logic)` |
| Ranking rule from YAML | `cohorts.Logic.from_dict` |
| MarketCheck call and response → row | `marketcheck.decode_neovin`, `marketcheck.normalize` |
| `mfr_code` / `options` from `raw` for the author's older decode file | `db._backfill_from_raw` (`extract_packages`, `extract_options`, `raw["manufacturer_code"]`) |
| Placement loop to mirror (minus web parts) | `main.place` ([main.py:273](../app/main.py)) |

Import gotchas:
- `app/config.py` reads `.env` and creates `DATA_DIR` when imported. `batch/__init__.py` points `DATA_DIR` at a temp folder before anything imports `app`. `analysis_months` from the YAML is applied by `sales_frame` setting `cohorts.ANALYSIS_MONTHS`; the environment variable would be read too late.
- `Logic.from_dict` silently ignores an `order` that doesn't contain `sum_total`.

## The decode table

1. Create `vin_specs` in MSSQL with the app's columns ([db.py:26](../app/db.py)).
2. Seed it from the author's `decode-cache-2026-10-02.jsonl.gz` (8,931 VINs, all decoded without errors).
   - That file is from an older app version and **has no `mfr_code` or `options` columns**. Fill both from `raw` using the app's own logic before loading.
   - Without `mfr_code`, nothing ever matches at the Exact level. Without `options`, the similarity score ignores options.
   - The file is git-ignored. Keep it out of the repo.
3. Each run, decode every VIN in the sales extract **and** in stock that isn't in `vin_specs` yet. MarketCheck's quota is the limit; the app caps a run at 12,000 decodes.

Decoding the sales VINs matters as much as decoding the cars being placed. A sold deal without a decode falls back to the sales table's make/model text: its trim is a guess, and its version, manufacturer code, engine, colors and options are unknown. It can never join an Exact pool, so the candidate (always decoded) ends up compared against looser, partly guessed history.

- **Coverage check, every run:** after stage 1, log the share of in-window sales deals with a successful decode, per store.
- If coverage falls below **98%**, finish the run but flag it on the dashboard ("decode coverage 91% — rankings may be loose"). Usually that means the MarketCheck quota ran out partway through the top-up.
- Decode failures (bad VINs, nothing returned) are retried on the next run but stop counting after 3 attempts (one extra `decode_attempts` column on the MSSQL table, which the app doesn't have), so they don't drag coverage down forever.

## Output: one table

Tableau Server gets **one data source, `transfer_placements`**, so everything can be filtered and sliced in one place.

**Grain: one row per transfer candidate × comparable deal.** The candidate and store columns repeat on every deal row of that store. A candidate with status `no decode` or `no comparables` keeps a single row with empty store and deal columns.

| Group | Columns |
|---|---|
| Run | `run_date`, `decode_coverage`, `coverage_warning` |
| Vehicle (inventory) | `vin`, `stock_no`, `current_store`, `current_store_name`, `days_in_stock`, `receive_date`, `mileage`, `year`, `make`, `model`, `dealer_cost` |
| Vehicle (decode) | `trim`, `version`, `mfr_code`, `engine`, `ext_color`, `int_color` |
| Pool | `status` (`placed` / `no decode` / `no comparables`), `match_level`, `exact`, `thin`, `pool_n`, `stores_in_pool`, `relaxed_fields`, `top_store`, `action` ("Keep at current store" / "Transfer to X" / "No ranked store"), `current_store_rank` |
| Store | `store`, `store_row`, `store_name`, `rank` (null = not ranked, too few deals), `is_current_store`, `n`, `total_hat`, `front_hat`, `back_hat`, `sum_total`, `sum_front`, `days_hat`, `days` (median), `annual`, `similar`, `why` |
| Comparable deal | `deal_vin`, `deal_sold_date`, `deal_year`, `deal_version`, `deal_mileage`, `deal_front_gross`, `deal_back_gross`, `deal_total_gross`, `deal_days_to_sell`, `deal_sold_price` |

**Summing store measures.** Columns like `n`, `sum_total` and `total_hat` repeat once per deal. `store_row` is true on exactly one row per candidate × store. Filter on `store_row = True` (or use `MIN`/`ATTR`) before summing or averaging them, or they're multiplied by the number of deals. Deal columns (`deal_*`) can be summed on any filter.

The **detail screen** shows:
- A headline, "Keep at current store" or "Transfer to X"
- The ranked stores with `why`
- The match level, with a warning when `thin`
- The comparable deals
- A fixed note: *"Comparable deals are cars that sold. This car hasn't yet; the ranking shows where cars like it sold well, not why this one is slow."*

## SQL: checks before trusting it

Both queries are in `batch/sql/`. Changes from your drafts:
- **Sales query**
  - `total_gross` now handles nulls. Before, plain `a + b` was null whenever either side was null.
  - `days_to_sell` is limited to 0–2000.
  - `sale_type` is forced to exactly `Retail`/`Lease`.
  - Codes and VINs are upper-cased and trimmed.
  - VINs must be 17 characters.
  - Duplicates on store + deal + VIN are removed.
- **Candidates query**
  - Parses `mm/dd/yy` with `try_convert(date, …, 1)`.
  - Adds `days_in_stock`.
  - Keeps used vehicles only (`[New/Used] = 'U'`).
  - Excludes the former stores.

Confirm these against the data. Each one silently breaks matching if it's wrong:

1. **`StoreID` uses the same codes as `FactSales.dealercode`** (NBMBN, BHMBN, …). If not, `is_current_store` never matches.
2. **`make` is spelled the same in FactSales as in the decodes** ("MERCEDES-BENZ" vs "MERCEDES BENZ"). Until the make/year change below lands, deals take their make from FactSales while candidates take it from the decode, and one spelling difference empties every pool for that brand. Check with `select distinct upper(make)` on both.
3. **`[RS Date]` is the sold date**, and **`[Age In Invt]` on a sale is sold − received days**.
4. **Whether FactSales has its own total-gross column.** Advent's "Total Deal Gross" was used when present. If it includes items beyond front + back, use it.
5. **Duplicates.** Compare `count(*)` with `count(distinct dealercode + dealnumber + vin)` before relying on `row_number`.

The sales query cuts at 6 months before today. The script then keeps 6 months before the newest sold date, so in practice the two windows match.

## Proving it matches the app

The ranking code is reused unchanged, so the risk is in the data path, not the logic.

- **If the author sends `used_sales.db`:**
  1. Run the app's `place` on about 50 VINs from it and save the result as the reference.
  2. Export the same database to parquet, run `batch/run.py` stages 4–5 on it, and require identical store order, `match_level`, `pool_n` and `why` for every VIN.
  3. Diff the MSSQL sales extract against its `deals` table (row counts per store and month, gross totals). This catches errors in the SQL mapping.
- **If not:**
  1. Load the MSSQL extract and the decode file into a scratch SQLite database using the app's schema.
  2. Run the reference through the app's code on it. The batch script must match.
  3. Then the author checks about 10 VINs on their live site against the dashboard. Small differences are expected because the data is newer. A different top store **with a different match level** points to a mapping error.

Only the ranking-rule loading has automated tests (`batch/tests/test_rule.py`). The parity comparison is a manual step when the data arrives. A `--local` run against a past `runs/<date>/inputs/` folder replays that week exactly.

## One deliberate change after parity: make and year from the decode

`taxonomy.canonical` ([taxonomy.py:117](../app/decode/taxonomy.py)) takes `make` and `year` from the sales row first and only falls back to the decode. Transfer candidates have no sales row, so they always get the decode's values. A spelling difference between FactSales and MarketCheck therefore stops a brand from matching at all. This has been in the code since the first commit, probably because the sales file was the only source of make for undecoded deals.

The fix: when the deal is decoded, use the decode's make and year; otherwise fall back to the sales row. This is about a two-line change. Make it **after** the parity check passes, as its own commit, and re-run the comparison. Expect differences only where FactSales and the decode disagreed.

## Build order

1. Run the five SQL checks above and fix the queries.
2. Create MSSQL `vin_specs` and seed it from the decode file, filling `mfr_code` and `options` from `raw`.
3. Write `batch/run.py` stages 2–5 (no decoding or publishing yet) and pass the parity check. Then make the make/year change above in its own commit.
4. Add stage 1 (decoding new VINs) with the MarketCheck key, and do a dry run with a small cap.
5. Add stage 6 (publishing). Publish the data source to a test project on Tableau Server.
6. Build the workbook on the one data source: a list filtered by days in stock (one row per `vin`), and a detail screen filtered to the selected `vin`.
7. Schedule it in Alteryx with Run Command, then hand over to the author.
8. After they've signed off, delete the web layer in one commit: `app/main.py` routes, templates, Dockerfile, Compose, Caddyfile and `railway.json`.

## Running it

On the Alteryx machine (Python 3.14 tested; any 3.11+ should do):

```
python -m venv .venv
.venv\Scripts\pip install -r batch\requirements.txt
copy .env.example .env                      (fill in MSSQL_CONNECTION, MARKETCHECK_API_KEY, TABLEAU_*)
```

One-time: run `batch/sql/vin_specs.sql` in MSSQL, then
`.venv\Scripts\python -m batch.run seed-decodes .claude\decode-cache-2026-10-02.jsonl.gz`.

Weekly, from the repo folder (Alteryx Run Command, working directory = repo):
`.venv\Scripts\python.exe -m batch.run` → exit code 0 ok, 1 publish failed, 2 anything else; details in `runs\<date>\run.log`.

Useful flags: `--skip-decode`, `--skip-publish`, `--decode-cap N`, `--local DIR` (read DIR\sales.parquet, transfer_candidates.parquet, vin_specs.parquet instead of MSSQL).

## Not in the first version

- Transfer cost, distance or time in transit. The ranking ignores them; the reviewer decides.
- A "minimum advantage to transfer" threshold.
- Rule changes by the author. Changes go through analytics as edits to `ranking_rule.yaml`.
- Keeping run history on Tableau Server. It lives in local parquet snapshots only.
