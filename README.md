# S&P 500 Toolkit

**Start here:** `index.html` puts every tool in one page behind a left-hand tab
rail. Run `python3 analyze_server.py` and open `http://localhost:8000/`. Each
tool also remains a working standalone page, listed below. See
**[Deployment](#deployment)** for the live URLs and the CI/CD pipeline.

This repository hosts multiple, independent S&P 500 apps and tools that share
the same downloaded price data:

1. **[Unified dashboard](#unified-dashboard)** (`index.html`) — left-hand tabs
   over the three tools below, with the screener able to hand a ticker
   straight to the analyzer tab.
2. **[S&P 500 Ledger simulator](#stock-picker-simulator)** (`ledger.html`;
   `sp500_simulator.html` is a byte-identical copy) — a browser-only backtest
   comparing monthly investing, lump-sum investing, and a rule-based stock
   picker. No build step, no server.
3. **[S&P 500 10-Year Monthly Price Downloader](#sp500-10-year-monthly-price-downloader)**
   (`download_*.py`) — Python scripts that fetch the price data the
   simulators consume.
4. **[Stock analyzer / Equity Dossier](#stock-analyzer-equity-dossier)**
   (`stock_analyzer.html` + `analyze_server.py`) — per-ticker value, analyst,
   news, technical, and macro analysis, backed by a small API server
   and Firestore.
5. **[Screening Desk](#screening-desk)** (`screener.html`, same server) — a
   live screener over established US listings that hands selected tickers
   straight to the analyzer.

---

## Unified dashboard

`index.html` is the single-page front end: a left tab rail with **The Ledger**,
**Screening Desk**, and **Equity Dossier**. Serve it from `analyze_server.py`
and open `http://localhost:8000/`.

- Each tool loads in its own frame on first visit and stays alive after that,
  so switching tabs never discards a screen you just ran or a dossier you
  just fetched.
- Deep links work and survive a reload: `/#screener`,
  `/#analyzer?ticker=AAPL`.
- Clicking a ticker in the Screening Desk switches to the Equity Dossier tab
  for that ticker instead of opening a new browser tab.
- The rail collapses to icons, responds to arrow keys, and shows whether
  the analysis API is reachable — the Ledger tab works without it, the
  other two do not.

Why frames rather than one merged document: `ledger.html` stays independently
linkable and works with no server at all, and the three tools have overlapping
element IDs and global names (`$`, `esc`, `api`, `#status`). Merging them into
one document would mean either maintaining the Ledger twice or renaming every
collision across ~2,800 lines of working JavaScript. A frame per tool keeps
each one exactly as it ships standalone.

---

## Stock Picker Simulator

A browser-based educational backtest comparing monthly investing, lump-sum
investing, and a rule-based stock picker over downloaded monthly S&P 500
constituent data. Open `ledger.html` — no server required.

### Features

- Select 1–50 top-ranked stocks.
- Rank and weight by market capitalization, trailing-year average daily volume, or trailing-year performance.
- Configure dip sales, take-profit sales, rebuy cooldowns, fees, and idle-cash interest.
- Apply a 25% tax to realized picker profits using average cost basis; losses receive no tax credit.
- Review every ticker held during the period, with end-of-period holdings shown in bold.
- Optimize rule parameters for maximum gain, minimum trades, or minimum contribution-adjusted volatility among rules that outperform monthly investing.
- Compare results with an equal-weight current-constituent benchmark proxy.

### Methodology warning

The backtest applies current S&P 500 membership and current ranking snapshots to historical prices. This introduces survivorship and look-ahead bias. The benchmark is an equal-weight proxy, not the official S&P 500 index. Optimizer results are in-sample and may overfit the selected historical window.

This project is for education and research only, not financial advice.

---

## S&P 500 10-Year Monthly Price Downloader

Downloads monthly adjusted-close prices for the current S&P 500 constituents
over the last 10 years, using the live Wikipedia constituents list and
Yahoo Finance via `yfinance`. This is the shared data source for the other
apps in this repo.

## Setup

```bash
cd ~/projects/sp500-data-downloader
python3 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python3 download_sp500.py
```

This takes a few minutes (it's pulling ~500 tickers). It creates:

- `sp500_monthly_prices_10yr.csv` — monthly adjusted-close prices for all available constituents.
- `sp500_top50_rankings.csv` — top-50 ranks and values for market capitalization, trailing-year average daily share volume, and trailing-year price performance.
- `sp500_top50_market_cap_monthly.csv` — 10-year monthly prices for the market-cap top 50.
- `sp500_top50_volume_monthly.csv` — 10-year monthly prices for the volume top 50.
- `sp500_top50_performance_monthly.csv` — 10-year monthly prices for the performance top 50.

The rankings use today's S&P 500 membership and therefore have survivorship bias
when compared with historical results. Market-cap metadata comes from Yahoo and
may be unavailable for individual symbols; those symbols are omitted from that
ranking.

## Download one constituent

Download 10 years of monthly OHLCV data for one current S&P 500 ticker:

```bash
python3 download_ticker.py AAPL
python3 download_ticker.py BRK.B
```

The script validates current membership, translates dot symbols to Yahoo's dash
format, and writes a file such as `AAPL_monthly_10yr.csv`. It includes Open,
High, Low, Close, adjusted Close when available, and Volume.

## Download all ranked tickers

After running `download_sp500.py`, download every unique ticker appearing in the
three top-50 rankings:

```bash
python3 download_ranked_tickers.py
```

By default, the script reads `sp500_top50_rankings.csv` and writes one monthly
OHLCV CSV per ticker under `top50_ticker_data/`. Tickers shared by multiple
rankings are downloaded only once. A failed ticker is reported without stopping
the remaining downloads.

Use another ranking file or output directory:

```bash
python3 download_ranked_tickers.py \
  --input sp500_top50_rankings.csv \
  --output-dir my_ticker_data
```

Download only one ranking:

```bash
python3 download_ranked_tickers.py --criterion market_cap
python3 download_ranked_tickers.py --criterion average_daily_volume
python3 download_ranked_tickers.py --criterion one_year_return
```

See all arguments:

```bash
python3 download_ranked_tickers.py --help
```

## Run the simulator

The simulator reads `sp500_monthly_prices_10yr.csv` and
`sp500_top50_rankings.csv` in the browser. Generate those files first, then serve
the project directory over HTTP:

```bash
python3 download_sp500.py
python3 -m http.server 8000
```

Open:

```text
http://localhost:8000/sp500_simulator.html
```

Do not open `sp500_simulator.html` directly with a `file://` URL: browsers block
its requests for the local CSV files. The page displays an actionable message if
this happens.

The dashboard supports a rolling window over the downloaded data and 1–50 ranked
stocks (10 by default). Market capitalization, trailing-year average daily
volume, and trailing-year performance each select their corresponding top-stock
universe and allocation weights. Its benchmark is an equal-weight proxy built
from the available monthly returns of current constituents, not the official
S&P 500 index. Applying current membership and current rankings to historical
data introduces survivorship and look-ahead bias.

Stop the server with `Ctrl+C` when finished.

## Stock analyzer (Equity Dossier)

Analyze any US-listed ticker: value grade from the trailing year of reports,
analyst consensus, news sentiment, technicals, and macro context. Bundles are
stored per day in Firestore, so repeat requests cost zero API calls and every
user of the shared Firebase project sees the same archive and history.

One-time setup:

1. `pip install -r requirements.txt` (adds `firebase-admin`).
2. Ensure `.env` contains `ALPHAVANTAGE_KEY`, `FMP_KEY`, and
   `FIREBASE_PROJECT_ID`.
3. Firebase console → Project settings → Service accounts → *Generate new
   private key* → save as `firebase-service-account.json` in the project root
   (gitignored — never commit it).
4. Firebase console → Build → Firestore Database → *Create database* (if not
   already enabled).
5. Add to `.env`:
   `GOOGLE_APPLICATION_CREDENTIALS=./firebase-service-account.json`

Run:

```bash
python3 analyze_server.py            # or --local-technicals, see below
```

Open `http://localhost:8000/stock_analyzer.html`, or `http://localhost:8000/`
for the tabbed shell. The server also serves the rest of the project
directory, so it replaces `python3 -m http.server` and `sp500_simulator.html`
keeps working. By default it binds to `127.0.0.1` only; pass `--bind 0.0.0.0`
to allow other machines on your network to reach it. See
[Deployment](#deployment) for running it publicly.

Free-tier quotas: Alpha Vantage allows 25 requests/day (5/minute) — about 4
freshly analyzed tickers/day. `--local-technicals` computes RSI/MACD/SMA
locally from Yahoo prices instead of Alpha Vantage, stretching that to ~20
tickers/day. FMP's 250 requests/day covers ~25 tickers. Already-analyzed
tickers are served from Firestore at no API cost. Without Firestore
credentials the server still runs with an in-memory store (nothing persists;
the page footnote shows the store status).

**When a ticker is not graded.** The value grade is the share of its ten
fundamental checks that pass, counting only the checks that had data — so a
thin denominator makes the letter an artifact of a single number rather than a
judgement. QQQ, an ETF with nine checks neutral, rode entirely on trailing P/E
and swung F → A in six days when that P/E was revised 30.63 → 29.30, crossing
the "< 30" line. Two guards prevent that. Fewer than five checks with data
returns **N/A**, names the shortfall, and records no pass ratio for the history
chart. And a fund — any Yahoo `quoteType` other than `EQUITY`, the same test
behind the screener's gate — is not graded at all: it reports no revenue,
margins, ROE, or leverage to grade, so its fundamentals are never fetched and
it keeps no sector (FMP files QQQ under "Financial Services", which describes
the trust, not what it holds). Technicals, news, and macro still apply.

Not investment advice.

## Screening Desk

`screener.html` (served by the same `analyze_server.py`, or the **Screening
Desk** tab of the `index.html` shell) screens established US listings live and
hands the ones you pick to the analyzer.

Filters, all applied server-side by Yahoo's screener: minimum market cap,
minimum 3-month average daily volume, sector, maximum trailing P/E, minimum
return on equity, minimum dividend yield, maximum beta, and venue (NasdaqGS,
NYSE, NasdaqGM, NasdaqCM, NYSE American — OTC venues are deliberately not
offered). Defaults are >$2B market cap and >500k average daily volume on
NasdaqGS + NYSE, ranked by market cap.

**The established-listing gate.** Yahoo has no IPO-date or listing-age filter,
so "at least N years of price history" cannot be pushed upstream. Every
screener row does carry `firstTradeDateMilliseconds`, so the gate is applied
locally after fetching, and the results line reports exactly what it removed
(*"23 shown of 25 fetched from 1,634 matching Yahoo's filters. Excluded 2: 2
under 1y of history…"*). Rows with no listing date are dropped rather than
kept — the gate exists to require a proven track record, and an unknown
listing date is not proof of one. So are rows whose `quoteType` is positively
not `EQUITY`.

**Scanning via the analyzer.** Tick rows and press *Scan N via analyzer* to
run each through the full Equity Dossier pipeline; grades land in the table's
Grade column. Scans run strictly sequentially and the confirm dialog
distinguishes tickers already in the Firestore archive (free) from fresh
fetches (~10 FMP + up to 5 Alpha Vantage calls each), because Alpha Vantage's
25 calls/day is only ~4 fresh tickers — ~20 with `--local-technicals`. A
running scan can be stopped between tickers. Tickers already in the archive
show their grade on load at no API cost.

Screen results are cached in memory for 15 minutes per distinct filter set and
are never written to Firestore: a screen is a snapshot of live market state,
not an analysis worth archiving. Criteria live in the URL, so a screen is
shareable and survives a reload.

Data source is the Yahoo Finance screener via `yfinance` — no API key, but an
unofficial endpoint, with the same firewall caveat as the rest of the Yahoo
data below. A column can read `—` for a metric the row was filtered on:
Yahoo populates its filter fields and its quote fields from different
pipelines, so a name with no trailing P/E (negative earnings) can still
satisfy a max-P/E filter.

Not investment advice.

---

## Deployment

Two targets, both driven by `.github/workflows/deploy.yml` on every push to
`main`. The `test` job runs the full pytest suite first and **both deploys
depend on it**, so a red suite blocks the release.

| Target | Serves | Works without the API? |
| --- | --- | --- |
| **Fly.io** | The whole toolkit: shell, all three tabs, CSVs **and** `/api/...` | — it *is* the API |
| **GitHub Pages** | Static mirror of the same pages | Ledger only; the other two tabs show "needs analyze_server.py" |

`analyze_server.py` is itself a static file server, so the Fly app serves the
pages and the API from one origin. That is why the pages keep their plain
`/api/...` paths with no CORS headers and no API base URL to configure. The
Pages mirror exists so the Ledger stays reachable for free even when the Fly
machine is stopped.

### One-time setup

**1. GitHub Pages** — repo *Settings → Pages → Build and deployment → Source:
**GitHub Actions***. No branch to pick; the workflow uploads the artifact.

**2. Fly.io** — install [`flyctl`](https://fly.io/docs/flyctl/install/), then:

```bash
flyctl auth login
# Fly app names are globally unique — pick your own and put it in fly.toml.
flyctl launch --no-deploy --copy-config --name your-app-name
```

Give the app its secrets. These are set **once on Fly** and persist across
deploys, so they never need to live in GitHub:

```bash
flyctl secrets set \
  ALPHAVANTAGE_KEY=... \
  FMP_KEY=... \
  FIREBASE_PROJECT_ID=... \
  FIREBASE_SERVICE_ACCOUNT_JSON="$(cat firebase-service-account.json)"
```

`docker-entrypoint.sh` writes that last one back to a file at boot, because
firebase-admin authenticates through Application Default Credentials and
requires a file path. Omit it and the server still starts — it falls back to
the in-memory store and the page footnote says so.

**3. The GitHub deploy token** — the only secret GitHub needs:

```bash
flyctl tokens create deploy --expiry 8760h    # 1 year
```

Add the output as repo secret **`FLY_API_TOKEN`** (*Settings → Secrets and
variables → Actions*).

Push to `main`, and both deploys run.

### Cost and quota

- The Fly app is configured to **scale to zero** (`min_machines_running = 0`),
  so an idle toolkit costs nothing; the first request after a stop pays a cold
  start while pandas and yfinance import.
- `fly.toml` sets `ANALYZE_ARGS = "--local-technicals"`, which computes
  RSI/MACD/SMA from Yahoo prices instead of Alpha Vantage — 1 AV call per
  ticker rather than 5, stretching the free 25/day quota from ~4 to ~20 fresh
  tickers. Remove the flag to go back to Alpha Vantage technicals.
- **A public deploy spends your API quota and writes to your shared Firestore
  project, for anyone who finds the URL.** This is the same tradeoff the
  `--bind 0.0.0.0` help text warns about locally, just with a wider audience.
  There is no auth in front of it. If that matters, put Fly's built-in
  [basic auth or an access-control proxy](https://fly.io/docs/) in front, or
  keep only the Pages mirror public.

### Deploying by hand

```bash
flyctl deploy                    # or --remote-only, as CI does
```

### Local container run

```bash
docker build -t sp500-toolkit .
docker run --rm -p 8080:8080 \
  -e ALPHAVANTAGE_KEY=... -e FMP_KEY=... \
  -e FIREBASE_PROJECT_ID=... \
  -e FIREBASE_SERVICE_ACCOUNT_JSON="$(cat firebase-service-account.json)" \
  sp500-toolkit
```

Then open `http://localhost:8080/`. `load_env()` reads `.env` when present and
lets real environment variables override it, which is what makes the same
`analyze_server.py` work both locally and in the container.

## Notes

- Requires outbound internet access to `en.wikipedia.org` and
  `query1/query2.finance.yahoo.com`. Corporate firewalls/VPNs sometimes
  block the Yahoo Finance endpoints — if the run finishes with very few
  columns, that's usually why.
- Some tickers may be missing entirely if they were delisted, renamed, or
  merged away before appearing in Yahoo's data; the script drops any
  ticker column that comes back completely empty.
- The date range is a rolling "today minus 10 years," so re-running the
  script later will shift the window forward.

## Next step

Once you have `sp500_monthly_prices_10yr.csv`, send it back to Claude to
wire it into the S&P 500 Ledger dashboard in place of the current
2013–2018 dataset.
