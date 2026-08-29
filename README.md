# S&P 500 10-Year Monthly Price Downloader

Downloads monthly adjusted-close prices for the current S&P 500 constituents
over the last 10 years, using the live Wikipedia constituents list and
Yahoo Finance via `yfinance`.

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

Open `http://localhost:8000/stock_analyzer.html`. The server also serves the
rest of the project directory, so it replaces `python3 -m http.server` and
`sp500_simulator.html` keeps working.

Free-tier quotas: Alpha Vantage allows 25 requests/day (5/minute) — about 4
freshly analyzed tickers/day. `--local-technicals` computes RSI/MACD/SMA
locally from Yahoo prices instead of Alpha Vantage, stretching that to ~20
tickers/day. FMP's 250 requests/day covers ~25 tickers. Already-analyzed
tickers are served from Firestore at no API cost. Without Firestore
credentials the server still runs with an in-memory store (nothing persists;
the page footnote shows the store status).

Not investment advice.

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
