# Stock Analyzer ("Equity Dossier") — Design Spec

- **Date:** 2026-08-29
- **Status:** Approved in chat; spec pending user review
- **Project:** sp500-data-downloader

## Overview

A new standalone page, `stock_analyzer.html`, that takes a US stock ticker as
input and renders a full analysis: value analysis from the trailing year of
financial reports, analyst consensus data, and news, sentiment, technical and
macro-economic context. A small local Python server, `analyze_server.py`,
fetches the data (yfinance + Alpha Vantage), stores fetched bundles in the
user's Firebase project, and serves both the API and the static files.

## Goals

1. Value analysis based on the past year of reports (trailing 4 quarterly
   reports + latest annual report), summarized as a transparent letter grade.
2. Analyst data: consensus rating distribution, price targets, recent
   upgrades/downgrades.
3. News and trends: per-article sentiment, aggregate sentiment, topic tags.
4. Technical indicators (RSI, MACD, SMA-50/200) and economic indicators
   (10Y Treasury, CPI, Fed funds, unemployment) via Alpha Vantage.
5. Persist every fetched bundle to Firebase (Firestore) so repeat requests
   cost zero API calls and history accumulates over time.
6. Browse all previously analyzed tickers — shared across all users, since
   every app instance reads/writes the same Firestore project — and open any
   past day's analysis of a ticker (historical view with trend summary).
7. Always show when the displayed data was fetched/updated, per bundle and
   per section, with a manual Refresh to re-fetch within the same day.

## Non-goals

- Multi-year fundamental history or DCF modeling.
- Portfolio features, alerts, or scheduled refresh.
- Authentication and hosted deployment. Multiple users share one Firebase
  project through their own locally-run servers; anyone holding a service
  account key for `ticket-analyzer-202` is fully trusted (read/write). No
  per-user identity is recorded.
- Changes to the existing simulator or downloader scripts.

## Hard constraints

- **Alpha Vantage free tier: 25 requests/day, 5/minute.** The design budgets
  5 AV calls per ticker (1 news + 4 technicals) plus 4 shared macro calls per
  day (~4 fresh tickers/day). A `--local-technicals` server flag computes
  RSI/MACD/SMA locally with pandas from yfinance prices instead, reducing the
  cost to 1 AV call per ticker (~20 tickers/day). Default: Alpha Vantage.
- **FMP (Financial Modeling Prep) free tier: 250 requests/day.** The design
  budgets ~10 FMP calls per ticker (~25 fresh tickers/day). Endpoints that
  turn out to be gated on the free plan fall back to yfinance per section.
- **`ALPHAVANTAGE_KEY` and `FMP_KEY` live in `.env`** and must never reach
  the browser. All external fetching is server-side.
- **Firestore document limit is 1 MiB**: news is trimmed to the top ~15
  articles; technical series to the last ~250 points.

## Assumptions (approved)

1. Any US-listed ticker is accepted — no S&P 500 membership validation.
   Ticker validity is checked by a yfinance lookup; unknown tickers return a
   clear error.
2. The page follows the existing "Ledger" newspaper aesthetic and
   zero-framework approach of `sp500_simulator.html` (vanilla JS, canvas
   charts, no external libraries).
3. "Past year reports" = trailing 4 quarterly reports + latest annual report.
4. Firebase project: **`ticket-analyzer-202`** (project number 613350196191),
   used verbatim. Firestore is the chosen product.

## Architecture

```
browser ── stock_analyzer.html (static, vanilla JS + canvas)
   │  GET /api/analyze?ticker=AAPL
   ▼
analyze_server.py (stdlib http.server, port 8000)
   ├─ reads .env (ALPHAVANTAGE_KEY, GOOGLE_APPLICATION_CREDENTIALS)
   ├─ firestore_store.py ── Firestore (project ticket-analyzer-202)
   ├─ analysis_sources.py ── FMP (fundamentals, ratios, analyst data)
   │                      ├─ yfinance (quote, price history, fallback)
   │                      └─ Alpha Vantage (news, technicals, macro)
   └─ value_grade.py ── scoring of the fundamentals bundle
```

`analyze_server.py` also serves the project directory as static files, so it
replaces `python3 -m http.server` and the existing simulator keeps working.

### Modules

| File | Responsibility |
|---|---|
| `analyze_server.py` | HTTP server: static files + `/api/analyze`; orchestrates store-read → fetch → store-write → respond; CLI flags (`--port`, `--local-technicals`); tiny stdlib `.env` parser (no python-dotenv) |
| `analysis_sources.py` | All external fetching: FMP client (profile, quarterly/annual statements, TTM ratios & key metrics, price-target consensus, grades summary & history), yfinance (live quote, daily prices, per-section fallback when an FMP endpoint is gated or fails), and Alpha Vantage client (`NEWS_SENTIMENT`, `RSI`, `MACD`, `SMA`×2, `TREASURY_YIELD`, `CPI`, `FEDERAL_FUNDS_RATE`, `UNEMPLOYMENT`); response trimming; local technical computation for `--local-technicals` |
| `value_grade.py` | Pure functions: compute the 10 checks and letter grade from a fundamentals dict; no I/O |
| `firestore_store.py` | Firestore read/write of daily bundles via `firebase-admin`; in-memory dict fallback when Firestore is unreachable |
| `stock_analyzer.html` | The page: input, rendering, canvas charts, per-section degraded states |

Each module is independently testable; `value_grade.py` and the AV response
parsing take plain dicts and touch no network.

## Data sourcing

| Feature | Primary source | Fallback | Cost |
|---|---|---|---|
| Live quote, day change, 52-week range | yfinance | — | 0 |
| Company profile (name, sector, industry) | FMP `profile` | yfinance | 1 FMP |
| Value analysis (quarterly + annual income/balance/cash-flow statements) | FMP `income-statement`, `balance-sheet-statement`, `cash-flow-statement` (quarterly ×3 + annual income) | yfinance statements | 4 FMP |
| Ratios & key metrics (TTM: P/E, PEG, P/B, EV/EBITDA, ROE, debt/equity, current ratio, FCF yield) | FMP `ratios-ttm`, `key-metrics-ttm` | computed from statements | 2 FMP |
| Analyst data (price-target consensus, rating distribution, recent grade changes) | FMP `price-target-consensus`, `grades-consensus`, `grades` (historical) | yfinance `analyst_price_targets`, `recommendations_summary`, `upgrades_downgrades` | 3 FMP |
| News & sentiment | Alpha Vantage `NEWS_SENTIMENT` | — | 1 AV per ticker |
| Technicals (RSI, MACD, SMA-50, SMA-200) | Alpha Vantage (`RSI`, `MACD`, `SMA`×2); locally computed under `--local-technicals` | local computation | 4 AV per ticker (or 0) |
| Macro (10Y Treasury, CPI YoY, Fed funds, unemployment) | Alpha Vantage `TREASURY_YIELD`, `CPI`, `FEDERAL_FUNDS_RATE`, `UNEMPLOYMENT` | — | 4 AV per day, shared across tickers |

Per-ticker budget: ~10 FMP calls (250/day quota → ~25 tickers) and 5 AV
calls (25/day quota → ~4 tickers with AV technicals, ~20 with
`--local-technicals`). Any FMP endpoint returning a payment/permission error
is remembered for the process lifetime and routed to its yfinance fallback.

Technical parameters (standard defaults, identical for the AV and local
paths): daily interval on close prices; RSI period 14; MACD 12/26/9;
SMA periods 50 and 200.

Ticker normalization reuses the existing pattern (`strip().upper()`,
`.` → `-`); FMP and Alpha Vantage accept the same dash form.

## Storage — Firestore

**Data model:**

```
tickers/{TICKER}                      registry doc, one per analyzed ticker
    name, sector, latestDate, latestGrade, updatedAt,
    summaries: { "YYYY-MM-DD": { grade, passRatio, price,
                                 targetMean, sentimentScore } }

tickers/{TICKER}/daily/{YYYY-MM-DD}   one doc per ticker per day
    snapshot, fundamentals, verdict, analyst, news, technicals, meta

macro/{YYYY-MM-DD}                    one shared doc per day
    treasury10y, fedFunds, cpiYoY, unemployment, fetchedAt
```

The registry doc powers the shared archive list and per-ticker history in
one read each (no subcollection scans). Each date summary is ~100 bytes, so
years of daily entries stay far under the 1 MiB doc limit.

**Flow:** on `GET /api/analyze?ticker=X`, read `tickers/X/daily/{today}`.
Present → serve as-is (zero external calls). Missing (or `refresh=1`) →
fetch, write the daily doc, upsert the registry doc, serve. Same
read-through pattern for `macro/{today}`. Requests for a past date only ever
read the store — they never trigger external fetching.

**Auth:** `firebase-admin` SDK with a service account key. Manual
prerequisites (user, one-time):

1. Firebase console → Project settings → Service accounts → *Generate new
   private key* → save as `firebase-service-account.json` in the project root.
2. Firebase console → Build → Firestore Database → *Create database* (if not
   already enabled).
3. Add to `.env`: `GOOGLE_APPLICATION_CREDENTIALS=./firebase-service-account.json`

`.env` already contains `ALPHAVANTAGE_KEY`, `FMP_KEY`, `FIREBASE_PROJECT_ID`,
and `FIREBASE_PROJECT_NUMBER`; `firestore_store.py` reads the project ID from
`FIREBASE_PROJECT_ID` instead of hardcoding it.

A new `.gitignore` lists `.env`, `firebase-service-account.json`, `venv/`,
and `__pycache__/` so credentials can never be committed if the project is
ever placed under git.

**Degraded mode:** if Firestore is unreachable at startup or per-request, log
a warning, fall back to an in-memory dict for the process lifetime, and set
`meta.store` accordingly so the page footnote shows "store: unavailable".

## API contract

`GET /api/analyze?ticker=AAPL` → `200 application/json`:

```json
{
  "ticker": "AAPL",
  "date": "2026-08-29",
  "snapshot":     { "name", "sector", "industry", "price", "dayChangePct",
                    "marketCap", "peRatio", "week52Low", "week52High" },
  "verdict":      { "grade": "B", "passes": 7, "evaluated": 10,
                    "summary": "…", "checks": [ { "id", "label", "value",
                    "threshold", "result": "pass|fail|neutral" } ] },
  "fundamentals": { "quarters": [ … 4 items … ], "annual": { … },
                    "ratios": { … } },
  "analyst":      { "ratings": { "strongBuy", "buy", "hold", "sell",
                    "strongSell" }, "targets": { "low", "mean", "high",
                    "current" }, "upgradesDowngrades": [ … ] },
  "technicals":   { "prices": [ … ≤250 { "date", "close" } … ],
                    "sma50": [ … ], "sma200": [ … ],
                    "rsi": { "value", "state" },
                    "macd": { "macd", "signal", "state" } },
  "news":         { "aggregateScore", "aggregateLabel",
                    "articles": [ … ≤15 { "title", "source", "url",
                    "publishedAt", "sentimentScore", "sentimentLabel",
                    "topics" } … ] },
  "macro":        { "treasury10y", "fedFunds", "cpiYoY", "unemployment" },
  "meta":         { "fetchedAt", "fromStore": true, "store":
                    "firestore|memory", "avCallsUsed": 5, "fmpCallsUsed": 10,
                    "sections":
                    { "fundamentals": "fmp|yfinance|unavailable",
                      "analyst": "fmp|yfinance|unavailable",
                      "news": "ok|unavailable", "technicals": "…",
                      "macro": "…" } }
}
```

Query parameters on `/api/analyze`: `date=YYYY-MM-DD` returns that day's
stored bundle (store-only — `404` if absent, never fetches externally);
`refresh=1` bypasses today's stored doc and re-fetches (mutually exclusive
with `date`).

Additional endpoints:

- `GET /api/analyzed` → `{ "tickers": [ { "ticker", "name", "latestDate",
  "latestGrade", "updatedAt" } … ] }`, ordered by `updatedAt` descending —
  every ticker analyzed by any user (one Firestore collection read).
- `GET /api/history?ticker=X` → `{ "ticker", "summaries": { "YYYY-MM-DD":
  { "grade", "passRatio", "price", "targetMean", "sentimentScore" } … } }`
  from the registry doc (one read); `404` if the ticker was never analyzed.

Errors: unknown ticker → `404` with `{ "error": "…" }`; AV quota exhausted or
endpoint failure → still `200`, with the failed section omitted and marked
`"unavailable"` in `meta.sections`. Sections degrade independently; the
FMP/yfinance-backed sections (snapshot, fundamentals, verdict, analyst) work
even with zero AV quota, and each FMP-backed section falls back to yfinance
if its FMP endpoint is gated or exhausted.

## Page design — `stock_analyzer.html`

Same paper-and-ink Ledger styling as the simulator (CSS variables, Georgia
serif + Courier mono, double rules, `.stamp`, `.status`, canvas charts). Top
to bottom:

1. **Masthead + ticker input** — text field, Analyze button, status line
   (including the existing "serve over HTTP, don't open via file://" guard,
   plus a "server not running" hint when `/api/analyze` is unreachable).
2. **The Archive** — shown on landing (and collapsible afterwards): a ledger
   table of every analyzed ticker from `/api/analyzed` — ticker, name,
   latest grade, "updated N hours ago" (with absolute timestamp on hover).
   Clicking a row loads that ticker's latest analysis.
3. **Snapshot bar** — company name, sector/industry, price with day change,
   market cap, P/E, 52-week range; a **"Data as of {date time}"** badge
   (from `meta.fetchedAt`) and a **Refresh** button that re-fetches via
   `refresh=1` (with a quota-cost hint). When a past date is being viewed,
   the badge is replaced by a rotated **"HISTORICAL EDITION — {date}"**
   stamp (reusing the simulator's `.stamp` style) and Refresh is hidden.
4. **The Verdict** — stamped card with the letter grade and a 2–3 sentence
   plain-English summary naming the strongest and weakest checks.
5. **Past Editions** — per-ticker history from `/api/history`: a canvas
   line chart of value-grade pass-ratio and news-sentiment score across
   analysis dates, above a table (date, grade, price, consensus target,
   sentiment) where each date links to that day's stored bundle via
   `?date=`.
6. **Value Analysis** — two-column ledger table (growth & profitability;
   valuation & health), each row with a ✓/✗/– marker; canvas bar chart of
   revenue and EPS for the last 4 quarters.
7. **Analyst Desk** — horizontal bar of Strong Buy→Strong Sell counts;
   price-target gauge (low / consensus / high vs current price on one line);
   recent upgrades/downgrades table.
8. **Technicals** — 1-year price line with SMA-50/200 overlay on canvas; RSI
   and MACD readouts with plain-English state (e.g. "RSI 71 — overbought").
9. **News & Trends** — aggregate sentiment gauge (bearish↔bullish), then up
   to 15 articles with source, time, sentiment badge, topic tags.
10. **Macro Context ribbon** — 10Y Treasury, Fed funds, CPI YoY,
    unemployment, with its own "as of" timestamp (macro is fetched once per
    day, so it may be older than the ticker bundle).
11. **Footnotes** — data sources, store status, bundle and macro fetch
    timestamps, AV/FMP calls used, "not investment advice" disclaimer.

Every section renders independently: a missing section shows an inline
"unavailable (quota exhausted / source error)" note instead of blanking the
page.

## Value-grade methodology

Ten checks over the trailing-year reports; each is pass / fail / neutral
(neutral = required data missing):

| # | Check | Threshold |
|---|---|---|
| 1 | Revenue growth (TTM YoY) | > 0 |
| 2 | Net margin | positive **and** ≥ year-ago quarter |
| 3 | EPS growth (TTM YoY) | > 0 |
| 4 | Return on equity | > 10% |
| 5 | Free cash flow (trailing 4 quarters) | > 0 |
| 6 | Debt / equity | < 1.5 |
| 7 | Current ratio | > 1 |
| 8 | P/E (trailing) | < 30 |
| 9 | PEG | < 2 |
| 10 | FCF yield | > 3% |

Neutral checks are excluded from the denominator. Grade by pass ratio over
evaluated checks: ≥ 0.9 → A, ≥ 0.7 → B, ≥ 0.5 → C, ≥ 0.3 → D, else F. The
verdict summary names the strongest and weakest areas. All scoring is pure
and unit-tested — no black box.

## Testing

Pytest, following the existing flat `test_*.py` pattern. No live network or
Firestore in tests:

- `test_value_grade.py` — all 10 checks, neutral handling, grade boundaries,
  verdict text.
- `test_analysis_sources.py` — FMP and AV response parsing and trimming from
  fixture JSON (statements, ratios, price targets, grades; news, RSI/MACD/SMA,
  macro); FMP gated-endpoint fallback routing to yfinance; local technical
  computation; ticker normalization.
- `test_analyze_server.py` — orchestration with a stubbed store and stubbed
  fetchers: store hit serves without fetching, store miss fetches and writes
  both the daily doc and the registry upsert, `refresh=1` forces a re-fetch
  and overwrite, `date=` requests never fetch externally and 404 when the doc
  is absent, `/api/analyzed` and `/api/history` serve from the registry,
  per-section degradation, unknown-ticker 404, memory fallback when the store
  raises.

## Files

**New:** `analyze_server.py`, `analysis_sources.py`, `value_grade.py`,
`firestore_store.py`, `stock_analyzer.html`, `test_value_grade.py`,
`test_analysis_sources.py`, `test_analyze_server.py`, `.gitignore`,
this spec.

**Changed:** `requirements.txt` (+ `firebase-admin>=6.5` — the only new
dependency), `README.md` (setup prerequisites, run instructions:
`python3 analyze_server.py` then open
`http://localhost:8000/stock_analyzer.html`).

## Future data-source options (out of scope, recorded from design)

- **SEC EDGAR** XBRL company-facts API — authoritative filings, free, no key.
- **FRED** — economic indicators if AV quota hurts.
- **Finnhub** — analyst recommendations/news, free tier 60 calls/min.
- **Polygon.io / Tiingo** — higher-quality price data.
