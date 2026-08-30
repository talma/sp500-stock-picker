# Stock Analyzer ("Equity Dossier") Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone `stock_analyzer.html` page plus a local Python server that analyzes any US ticker — value grade from trailing-year reports, analyst consensus, news sentiment, technicals, macro — and stores every fetched bundle in Firestore so repeats are free and history accumulates.

**Architecture:** `analyze_server.py` (stdlib `http.server`) serves static files and a JSON API. `analysis_sources.py` does all external fetching (FMP primary for fundamentals/analyst, yfinance for quotes/prices/fallback, Alpha Vantage for news/technicals/macro). `value_grade.py` is pure scoring. `firestore_store.py` is a read-through daily-doc cache with a per-ticker registry doc powering the shared archive and history. The page is vanilla JS + canvas in the existing "Ledger" aesthetic.

**Tech Stack:** Python 3 stdlib (`http.server`, `urllib`), pandas, yfinance (existing deps), `firebase-admin>=6.5` (only new dep), pytest for tests; vanilla JS/CSS/canvas in the browser.

**Spec:** `docs/superpowers/specs/2026-08-29-stock-analyzer-design.md`

## Global Constraints

- Alpha Vantage free tier: **25 requests/day, 5/minute**. Budget: 5 AV calls/ticker (1 news + 4 technicals) + 4 shared macro calls/day. `--local-technicals` flag computes RSI/MACD/SMA locally → 1 AV call/ticker.
- FMP free tier: **250 requests/day**. Budget ~10 FMP calls/ticker. An FMP endpoint returning a payment/permission error is remembered for the process lifetime and routed to its yfinance fallback.
- `ALPHAVANTAGE_KEY` and `FMP_KEY` live in `.env` and must **never reach the browser**. All external fetching is server-side. Never print or commit key values.
- Firestore doc limit 1 MiB: trim news to ≤15 articles, technical/price series to ≤250 points.
- Only new dependency: `firebase-admin>=6.5`. No python-dotenv, no requests — stdlib `urllib` and a tiny `.env` parser.
- Ticker normalization everywhere: `ticker.strip().upper().replace(".", "-")`.
- Technical parameters: daily close prices; RSI period 14; MACD 12/26/9; SMA 50 and 200.
- Grade bands over evaluated (non-neutral) checks: ≥0.9 A, ≥0.7 B, ≥0.5 C, ≥0.3 D, else F. Neutral checks excluded from the denominator.
- Tests: pytest, flat `test_*.py` at repo root, **no live network and no live Firestore**. Run with `python3 -m pytest` (install pytest into the venv if missing; it is a dev tool, not a requirements.txt entry).
- **Git note:** this project is not currently a git repository. Run `git init` once before Task 1, or skip every "Commit" step. If you do init, complete Task 4's `.gitignore` before the first commit that could touch `.env`.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `value_grade.py` (new) | Pure scoring: 10 checks + letter grade + verdict text. No I/O. | 1 |
| `analysis_sources.py` (new) | Ticker normalization; FMP client + parsers; yfinance quote/prices/fallbacks; AV client + news/technicals/macro parsers; local technical computation. | 2, 3 |
| `firestore_store.py` (new) | `Store` facade over `FirestoreBackend`/`MemoryBackend`: daily docs, macro docs, registry docs. Falls back to memory. | 4 |
| `analyze_server.py` (new) | `.env` parser, `BundleFetcher` (per-section degradation), `Analyzer` (store read-through, registry upsert), `handle_api` routing, HTTP server + CLI. | 5 |
| `stock_analyzer.html` (new) | The page: archive, snapshot, verdict, history, value, analyst, technicals, news, macro, footnotes. | 6 |
| `test_value_grade.py`, `test_analysis_sources.py`, `test_analyze_server.py` (new) | Unit tests per module. | 1–5 |
| `.gitignore` (new), `requirements.txt` (modify) | Credentials hygiene; add firebase-admin. | 4 |
| `README.md` (modify) | Setup + run instructions. | 7 |

---

### Task 1: `value_grade.py` — pure scoring

**Files:**
- Create: `value_grade.py`
- Test: `test_value_grade.py`

**Interfaces:**
- Consumes: nothing (pure module).
- Produces: `compute_verdict(metrics: dict) -> dict` returning
  `{"grade": "A".."F"|"N/A", "passes": int, "evaluated": int, "summary": str, "checks": [{"id","label","value","threshold","result": "pass"|"fail"|"neutral"}]}`.
  `metrics` keys (any may be missing/None → that check is neutral):
  `revenueGrowth, epsGrowth, netMargin, netMarginYearAgo, roe, fcfTTM, debtToEquity, currentRatio, peTTM, peg, fcfYield`. Growth/margin/yield values are fractions (0.12 = 12%).

- [ ] **Step 1: Write the failing tests**

```python
# test_value_grade.py
import pytest

import value_grade


def good_metrics(**overrides):
    metrics = {
        "revenueGrowth": 0.12,
        "netMargin": 0.25,
        "netMarginYearAgo": 0.22,
        "epsGrowth": 0.10,
        "roe": 0.30,
        "fcfTTM": 90e9,
        "debtToEquity": 1.2,
        "currentRatio": 1.4,
        "peTTM": 22.0,
        "peg": 1.5,
        "fcfYield": 0.045,
    }
    metrics.update(overrides)
    return metrics


# (metric override, failing value, check id) for every check
FAILING = [
    ("revenueGrowth", -0.05, "revenue_growth"),
    ("netMargin", -0.01, "net_margin"),
    ("epsGrowth", -0.02, "eps_growth"),
    ("roe", 0.05, "roe"),
    ("fcfTTM", -1e9, "fcf"),
    ("debtToEquity", 2.0, "debt_equity"),
    ("currentRatio", 0.8, "current_ratio"),
    ("peTTM", 45.0, "pe"),
    ("peg", 3.0, "peg"),
    ("fcfYield", 0.01, "fcf_yield"),
]


def check_by_id(verdict, check_id):
    return next(c for c in verdict["checks"] if c["id"] == check_id)


def test_all_pass_is_grade_a():
    verdict = value_grade.compute_verdict(good_metrics())
    assert verdict["grade"] == "A"
    assert verdict["passes"] == 10
    assert verdict["evaluated"] == 10
    assert all(c["result"] == "pass" for c in verdict["checks"])


@pytest.mark.parametrize("key,bad,check_id", FAILING)
def test_each_check_fails_on_bad_value(key, bad, check_id):
    verdict = value_grade.compute_verdict(good_metrics(**{key: bad}))
    assert check_by_id(verdict, check_id)["result"] == "fail"


def test_missing_value_is_neutral_and_excluded():
    verdict = value_grade.compute_verdict(good_metrics(peg=None))
    assert check_by_id(verdict, "peg")["result"] == "neutral"
    assert verdict["evaluated"] == 9
    assert verdict["passes"] == 9
    assert verdict["grade"] == "A"  # 9/9


def test_net_margin_needs_year_ago_value():
    verdict = value_grade.compute_verdict(good_metrics(netMarginYearAgo=None))
    assert check_by_id(verdict, "net_margin")["result"] == "neutral"


def test_positive_margin_below_year_ago_fails():
    verdict = value_grade.compute_verdict(
        good_metrics(netMargin=0.10, netMarginYearAgo=0.20)
    )
    assert check_by_id(verdict, "net_margin")["result"] == "fail"


def test_negative_pe_fails_not_passes():
    verdict = value_grade.compute_verdict(good_metrics(peTTM=-12.0))
    assert check_by_id(verdict, "pe")["result"] == "fail"


def fail_n(n):
    """good_metrics with the first n checks failing."""
    overrides = {key: bad for key, bad, _ in FAILING[:n]}
    return value_grade.compute_verdict(good_metrics(**overrides))


@pytest.mark.parametrize("fails,grade", [
    (0, "A"), (1, "A"),          # 10/10, 9/10 = 0.9
    (3, "B"),                    # 7/10
    (5, "C"),                    # 5/10
    (7, "D"),                    # 3/10
    (8, "F"),                    # 2/10
])
def test_grade_boundaries(fails, grade):
    assert fail_n(fails)["grade"] == grade


def test_all_neutral_is_not_applicable():
    verdict = value_grade.compute_verdict({})
    assert verdict["grade"] == "N/A"
    assert verdict["evaluated"] == 0
    assert "insufficient" in verdict["summary"].lower()


def test_summary_names_grade_and_weakest_check():
    verdict = value_grade.compute_verdict(good_metrics(peg=3.0))
    assert verdict["grade"] in "ABCDF"
    assert verdict["grade"] in verdict["summary"]
    assert "PEG" in verdict["summary"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_value_grade.py -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'value_grade'`

- [ ] **Step 3: Write the implementation**

```python
# value_grade.py
"""Pure value-grade scoring over trailing-year fundamentals. No I/O."""

GRADE_BANDS = [(0.9, "A"), (0.7, "B"), (0.5, "C"), (0.3, "D")]


def _compare(value, result_if):
    if value is None:
        return "neutral", None
    return ("pass" if result_if(value) else "fail"), value


def _net_margin(metrics):
    margin = metrics.get("netMargin")
    year_ago = metrics.get("netMarginYearAgo")
    if margin is None or year_ago is None:
        return "neutral", margin
    return ("pass" if margin > 0 and margin >= year_ago else "fail"), margin


CHECKS = [
    ("revenue_growth", "Revenue growth (TTM YoY)", "> 0",
     lambda m: _compare(m.get("revenueGrowth"), lambda v: v > 0)),
    ("net_margin", "Net margin", "positive and >= year-ago", _net_margin),
    ("eps_growth", "EPS growth (TTM YoY)", "> 0",
     lambda m: _compare(m.get("epsGrowth"), lambda v: v > 0)),
    ("roe", "Return on equity", "> 10%",
     lambda m: _compare(m.get("roe"), lambda v: v > 0.10)),
    ("fcf", "Free cash flow (TTM)", "> 0",
     lambda m: _compare(m.get("fcfTTM"), lambda v: v > 0)),
    ("debt_equity", "Debt / equity", "< 1.5",
     lambda m: _compare(m.get("debtToEquity"), lambda v: v < 1.5)),
    ("current_ratio", "Current ratio", "> 1",
     lambda m: _compare(m.get("currentRatio"), lambda v: v > 1)),
    ("pe", "P/E (trailing)", "0 < P/E < 30",
     lambda m: _compare(m.get("peTTM"), lambda v: 0 < v < 30)),
    ("peg", "PEG", "0 < PEG < 2",
     lambda m: _compare(m.get("peg"), lambda v: 0 < v < 2)),
    ("fcf_yield", "FCF yield", "> 3%",
     lambda m: _compare(m.get("fcfYield"), lambda v: v > 0.03)),
]


def compute_verdict(metrics):
    checks = []
    for check_id, label, threshold, evaluate in CHECKS:
        result, value = evaluate(metrics or {})
        checks.append({"id": check_id, "label": label, "value": value,
                       "threshold": threshold, "result": result})

    evaluated = [c for c in checks if c["result"] != "neutral"]
    passes = [c for c in checks if c["result"] == "pass"]
    fails = [c for c in checks if c["result"] == "fail"]

    if not evaluated:
        return {"grade": "N/A", "passes": 0, "evaluated": 0,
                "summary": "Insufficient data to grade this ticker.",
                "checks": checks}

    ratio = len(passes) / len(evaluated)
    grade = next((g for floor, g in GRADE_BANDS if ratio >= floor), "F")

    strongest = ", ".join(c["label"] for c in passes[:2]) or "none"
    weakest = ", ".join(c["label"] for c in fails[:2])
    summary = (f"Grade {grade}: {len(passes)} of {len(evaluated)} evaluated "
               f"checks pass. Strongest: {strongest}."
               + (f" Weakest: {weakest}." if weakest else " No failed checks."))
    return {"grade": grade, "passes": len(passes),
            "evaluated": len(evaluated), "summary": summary, "checks": checks}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_value_grade.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add value_grade.py test_value_grade.py
git commit -m "feat: add pure value-grade scoring (10 checks, letter grade)"
```

---

### Task 2: `analysis_sources.py` part 1 — normalization, FMP client + parsers, yfinance fallbacks

**Files:**
- Create: `analysis_sources.py`
- Test: `test_analysis_sources.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (used by Task 5's `BundleFetcher` and Task 3):
  - `normalize_ticker(ticker: str) -> str`
  - `class GatedEndpoint(Exception)` — FMP endpoint unavailable on the free plan.
  - `class FMPClient(api_key, fetch_json=_http_get_json)` with `.get(endpoint: str, **params) -> list|dict` (raises `GatedEndpoint`, remembers gated endpoints in `.gated: set`), `.calls_used: int`.
  - `parse_profile(rows: list) -> dict` → `{"name","sector","industry"}`
  - `build_fundamentals(income_q, balance_q, cashflow_q, income_a, ratios_ttm, key_metrics_ttm) -> (fundamentals: dict, metrics: dict)` — `fundamentals` is the API section `{"quarters":[≤4 {"date","revenue","netIncome","eps","netMargin"}], "annual":{"date","revenue","netIncome","eps"}, "ratios":{...}}`; `metrics` is Task 1's grading input.
  - `parse_analyst(price_target_rows, grades_consensus_rows, grades_rows) -> dict` → `{"ratings":{"strongBuy","buy","hold","sell","strongSell"}, "targets":{"low","mean","high"}, "upgradesDowngrades":[≤10 {"date","firm","fromGrade","toGrade","action"}]}`
  - yfinance (network; `ticker_factory` injectable for tests): `yf_quote(ticker, ticker_factory=yf.Ticker) -> dict` (snapshot: `name, sector, industry, price, dayChangePct, marketCap, peRatio, week52Low, week52High`; raises `LookupError` for unknown tickers), `yf_prices(ticker, ticker_factory=yf.Ticker) -> pandas.Series` (1y daily close, DatetimeIndex), `yf_fundamentals(ticker, ticker_factory=yf.Ticker) -> (fundamentals, metrics)`, `yf_analyst(ticker, ticker_factory=yf.Ticker) -> dict` (same shape as `parse_analyst`).

FMP field names differ between API generations, so every parser goes through a tolerant `_first(row, *keys)` getter with candidate key lists. The fixtures below define the canonical expectations; if a live response uses different keys, add them to the candidate list — do not change the output shape.

- [ ] **Step 1: Write the failing tests**

Append to a new `test_analysis_sources.py`:

```python
# test_analysis_sources.py
import urllib.error

import pytest

import analysis_sources as src


# ---------- normalization ----------

@pytest.mark.parametrize("raw,expected", [
    ("aapl", "AAPL"), (" msft ", "MSFT"), ("brk.b", "BRK-B"), ("BF.B", "BF-B"),
])
def test_normalize_ticker(raw, expected):
    assert src.normalize_ticker(raw) == expected


# ---------- FMP client ----------

def test_fmp_client_counts_calls_and_builds_url():
    seen = []
    def fake_fetch(url):
        seen.append(url)
        return [{"ok": True}]
    client = src.FMPClient("SECRET", fetch_json=fake_fetch)
    payload = client.get("profile", symbol="AAPL")
    assert payload == [{"ok": True}]
    assert client.calls_used == 1
    assert "profile" in seen[0] and "symbol=AAPL" in seen[0] and "apikey=SECRET" in seen[0]


def test_fmp_http_402_gates_endpoint_for_process_lifetime():
    calls = {"n": 0}
    def fake_fetch(url):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 402, "Payment Required", {}, None)
    client = src.FMPClient("K", fetch_json=fake_fetch)
    with pytest.raises(src.GatedEndpoint):
        client.get("grades", symbol="AAPL")
    with pytest.raises(src.GatedEndpoint):
        client.get("grades", symbol="MSFT")   # remembered: no second HTTP call
    assert calls["n"] == 1
    assert "grades" in client.gated


def test_fmp_error_message_payload_gates_endpoint():
    client = src.FMPClient("K", fetch_json=lambda url: {
        "Error Message": "Exclusive Endpoint: this endpoint requires an upgrade"})
    with pytest.raises(src.GatedEndpoint):
        client.get("ratios-ttm", symbol="AAPL")
    assert "ratios-ttm" in client.gated


# ---------- FMP parsers ----------

PROFILE = [{"companyName": "Apple Inc.", "sector": "Technology",
            "industry": "Consumer Electronics"}]

def quarter(date, revenue, net_income, eps):
    return {"date": date, "revenue": revenue, "netIncome": net_income, "eps": eps}

INCOME_Q = [  # newest first, 8 quarters; recent TTM rev 400, prior TTM 360
    quarter("2026-06-28", 100, 25, 1.60), quarter("2026-03-29", 100, 24, 1.55),
    quarter("2025-12-28", 110, 30, 1.90), quarter("2025-09-27", 90, 21, 1.35),
    quarter("2025-06-28", 95, 22, 1.40), quarter("2025-03-29", 90, 20, 1.30),
    quarter("2024-12-28", 95, 24, 1.55), quarter("2024-09-27", 80, 18, 1.15),
]
BALANCE_Q = [{"date": "2026-06-28", "totalCurrentAssets": 140,
              "totalCurrentLiabilities": 100, "totalDebt": 110,
              "totalStockholdersEquity": 80}]
CASHFLOW_Q = [{"date": "2026-06-28", "freeCashFlow": 30},
              {"date": "2026-03-29", "freeCashFlow": 28},
              {"date": "2025-12-28", "freeCashFlow": 35},
              {"date": "2025-09-27", "freeCashFlow": 22}]
INCOME_A = [{"date": "2025-09-27", "revenue": 390, "netIncome": 97, "eps": 6.10}]
RATIOS_TTM = [{"peRatioTTM": 27.5, "pegRatioTTM": 1.9, "priceToBookRatioTTM": 40.0,
               "currentRatioTTM": 1.4, "debtEquityRatioTTM": 1.375,
               "netProfitMarginTTM": 0.25}]
KEY_METRICS_TTM = [{"roeTTM": 0.30, "evToEBITDATTM": 21.0,
                    "freeCashFlowYieldTTM": 0.038}]


def test_build_fundamentals_section_and_metrics():
    fundamentals, metrics = src.build_fundamentals(
        INCOME_Q, BALANCE_Q, CASHFLOW_Q, INCOME_A, RATIOS_TTM, KEY_METRICS_TTM)
    assert len(fundamentals["quarters"]) == 4
    assert fundamentals["quarters"][0] == {
        "date": "2026-06-28", "revenue": 100, "netIncome": 25,
        "eps": 1.60, "netMargin": 0.25}
    assert fundamentals["annual"]["revenue"] == 390
    assert fundamentals["ratios"]["peTTM"] == 27.5
    # TTM revenue 100+100+110+90=400 vs prior 95+90+95+80=360
    assert metrics["revenueGrowth"] == pytest.approx(400 / 360 - 1)
    assert metrics["epsGrowth"] == pytest.approx(6.40 / 5.40 - 1)
    assert metrics["netMargin"] == pytest.approx(0.25)          # from ratios-ttm
    assert metrics["netMarginYearAgo"] == pytest.approx(22 / 95)  # quarter index 4
    assert metrics["roe"] == 0.30
    assert metrics["fcfTTM"] == 115                              # 30+28+35+22
    assert metrics["debtToEquity"] == 1.375
    assert metrics["currentRatio"] == 1.4
    assert metrics["peTTM"] == 27.5
    assert metrics["peg"] == 1.9
    assert metrics["fcfYield"] == 0.038


def test_build_fundamentals_tolerates_missing_pieces():
    fundamentals, metrics = src.build_fundamentals(
        INCOME_Q[:4], [], [], [], [], [])
    assert metrics["revenueGrowth"] is None      # fewer than 8 quarters
    assert metrics["fcfTTM"] is None
    assert metrics["peTTM"] is None
    assert fundamentals["annual"] is None


PRICE_TARGET = [{"targetLow": 180.0, "targetConsensus": 245.5, "targetHigh": 310.0}]
GRADES_CONSENSUS = [{"strongBuy": 12, "buy": 20, "hold": 8, "sell": 2, "strongSell": 1}]
GRADES = [{"date": "2026-08-20", "gradingCompany": "Morgan Stanley",
           "previousGrade": "Equal-Weight", "newGrade": "Overweight",
           "action": "upgrade"}] * 12


def test_parse_analyst():
    analyst = src.parse_analyst(PRICE_TARGET, GRADES_CONSENSUS, GRADES)
    assert analyst["targets"] == {"low": 180.0, "mean": 245.5, "high": 310.0}
    assert analyst["ratings"]["strongBuy"] == 12
    assert len(analyst["upgradesDowngrades"]) == 10   # trimmed
    first = analyst["upgradesDowngrades"][0]
    assert first == {"date": "2026-08-20", "firm": "Morgan Stanley",
                     "fromGrade": "Equal-Weight", "toGrade": "Overweight",
                     "action": "upgrade"}


def test_parse_profile():
    assert src.parse_profile(PROFILE) == {
        "name": "Apple Inc.", "sector": "Technology",
        "industry": "Consumer Electronics"}


# ---------- yfinance fallbacks (stubbed Ticker) ----------

class FakeTicker:
    def __init__(self, symbol):
        self.info = {"longName": "Apple Inc.", "sector": "Technology",
                     "industry": "Consumer Electronics",
                     "regularMarketPrice": 230.0,
                     "regularMarketPreviousClose": 225.0,
                     "marketCap": 3.5e12, "trailingPE": 28.1,
                     "fiftyTwoWeekLow": 164.0, "fiftyTwoWeekHigh": 260.0}


def test_yf_quote_maps_info_fields():
    quote = src.yf_quote("AAPL", ticker_factory=FakeTicker)
    assert quote["name"] == "Apple Inc."
    assert quote["price"] == 230.0
    assert quote["dayChangePct"] == pytest.approx((230.0 / 225.0 - 1) * 100)
    assert quote["week52High"] == 260.0


def test_yf_quote_unknown_ticker_raises_lookup_error():
    class EmptyTicker:
        def __init__(self, symbol):
            self.info = {}
    with pytest.raises(LookupError):
        src.yf_quote("ZZZZZZ", ticker_factory=EmptyTicker)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_analysis_sources.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analysis_sources'`

- [ ] **Step 3: Write the implementation**

```python
# analysis_sources.py
"""All external data access: FMP, yfinance, Alpha Vantage. Parsers are pure."""
import json
import urllib.error
import urllib.parse
import urllib.request

import yfinance as yf

FMP_BASE = "https://financialmodelingprep.com/stable"


def normalize_ticker(ticker):
    return ticker.strip().upper().replace(".", "-")


def _http_get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "sp500-analyzer"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _first(row, *keys):
    """Tolerant getter: first present, non-None key. FMP names drift between
    API generations, so parsers list candidates instead of one exact key."""
    for key in keys:
        if row and row.get(key) is not None:
            return row[key]
    return None


class GatedEndpoint(Exception):
    """FMP endpoint not available on the current (free) plan."""


class FMPClient:
    def __init__(self, api_key, fetch_json=_http_get_json):
        self.api_key = api_key
        self.fetch_json = fetch_json
        self.calls_used = 0
        self.gated = set()

    def get(self, endpoint, **params):
        if endpoint in self.gated:
            raise GatedEndpoint(endpoint)
        params["apikey"] = self.api_key
        url = f"{FMP_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
        try:
            payload = self.fetch_json(url)
        except urllib.error.HTTPError as error:
            if error.code in (401, 402, 403):
                self.gated.add(endpoint)
                raise GatedEndpoint(endpoint) from error
            raise
        self.calls_used += 1
        if isinstance(payload, dict) and payload.get("Error Message"):
            message = payload["Error Message"]
            if "exclusive" in message.lower() or "upgrade" in message.lower():
                self.gated.add(endpoint)
                raise GatedEndpoint(f"{endpoint}: {message}")
            raise RuntimeError(f"FMP {endpoint}: {message}")
        return payload


def parse_profile(rows):
    row = rows[0] if rows else {}
    return {"name": _first(row, "companyName", "name"),
            "sector": _first(row, "sector"),
            "industry": _first(row, "industry")}


def _quarter_row(row):
    revenue = _first(row, "revenue", "totalRevenue")
    net_income = _first(row, "netIncome")
    return {"date": _first(row, "date"),
            "revenue": revenue,
            "netIncome": net_income,
            "eps": _first(row, "eps", "epsdiluted", "epsDiluted"),
            "netMargin": (net_income / revenue) if revenue and net_income is not None else None}


def _ttm_growth(quarters, field):
    """(sum of quarters 0-3) / (sum of quarters 4-7) - 1, or None."""
    values = [_first(q, field, "epsdiluted" if field == "eps" else field)
              for q in quarters[:8]]
    if len(values) < 8 or any(v is None for v in values):
        return None
    recent, prior = sum(values[:4]), sum(values[4:8])
    return (recent / prior - 1) if prior else None


def build_fundamentals(income_q, balance_q, cashflow_q, income_a,
                       ratios_ttm, key_metrics_ttm):
    income_q = income_q or []
    quarters = [_quarter_row(row) for row in income_q[:4]]
    annual_row = (income_a or [None])[0]
    annual = None
    if annual_row:
        annual = {"date": _first(annual_row, "date"),
                  "revenue": _first(annual_row, "revenue"),
                  "netIncome": _first(annual_row, "netIncome"),
                  "eps": _first(annual_row, "eps", "epsdiluted")}

    ratios = (ratios_ttm or [None])[0] or {}
    key_metrics = (key_metrics_ttm or [None])[0] or {}
    balance = (balance_q or [None])[0] or {}
    fcf_values = [_first(row, "freeCashFlow") for row in (cashflow_q or [])[:4]]
    fcf_ttm = sum(fcf_values) if len(fcf_values) == 4 and all(
        v is not None for v in fcf_values) else None

    pe = _first(ratios, "peRatioTTM", "priceToEarningsRatioTTM")
    current_ratio = _first(ratios, "currentRatioTTM") or _current_ratio(balance)
    debt_equity = _first(ratios, "debtEquityRatioTTM", "debtToEquityTTM") \
        or _debt_equity(balance)

    year_ago = income_q[4] if len(income_q) > 4 else None
    metrics = {
        "revenueGrowth": _ttm_growth(income_q, "revenue"),
        "epsGrowth": _ttm_growth(income_q, "eps"),
        "netMargin": _first(ratios, "netProfitMarginTTM", "netIncomePerRevenueTTM"),
        "netMarginYearAgo": _quarter_row(year_ago)["netMargin"] if year_ago else None,
        "roe": _first(key_metrics, "roeTTM", "returnOnEquityTTM"),
        "fcfTTM": fcf_ttm,
        "debtToEquity": debt_equity,
        "currentRatio": current_ratio,
        "peTTM": pe,
        "peg": _first(ratios, "pegRatioTTM", "priceEarningsToGrowthRatioTTM"),
        "fcfYield": _first(key_metrics, "freeCashFlowYieldTTM"),
    }
    fundamentals = {
        "quarters": quarters,
        "annual": annual,
        "ratios": {"peTTM": pe,
                   "peg": metrics["peg"],
                   "priceToBook": _first(ratios, "priceToBookRatioTTM", "pbRatioTTM"),
                   "evToEBITDA": _first(key_metrics, "evToEBITDATTM",
                                        "enterpriseValueOverEBITDATTM"),
                   "roe": metrics["roe"],
                   "debtToEquity": debt_equity,
                   "currentRatio": current_ratio,
                   "fcfYield": metrics["fcfYield"],
                   "netMargin": metrics["netMargin"]},
    }
    return fundamentals, metrics


def _current_ratio(balance):
    assets = _first(balance, "totalCurrentAssets")
    liabilities = _first(balance, "totalCurrentLiabilities")
    return (assets / liabilities) if assets and liabilities else None


def _debt_equity(balance):
    debt = _first(balance, "totalDebt")
    equity = _first(balance, "totalStockholdersEquity", "totalEquity")
    return (debt / equity) if debt is not None and equity else None


def parse_analyst(price_target_rows, grades_consensus_rows, grades_rows):
    target = (price_target_rows or [None])[0] or {}
    consensus = (grades_consensus_rows or [None])[0] or {}
    changes = [{"date": _first(row, "date"),
                "firm": _first(row, "gradingCompany", "analystCompany", "company"),
                "fromGrade": _first(row, "previousGrade"),
                "toGrade": _first(row, "newGrade"),
                "action": _first(row, "action")}
               for row in (grades_rows or [])[:10]]
    return {
        "ratings": {"strongBuy": _first(consensus, "strongBuy") or 0,
                    "buy": _first(consensus, "buy") or 0,
                    "hold": _first(consensus, "hold") or 0,
                    "sell": _first(consensus, "sell") or 0,
                    "strongSell": _first(consensus, "strongSell") or 0},
        "targets": {"low": _first(target, "targetLow"),
                    "mean": _first(target, "targetConsensus", "targetMean"),
                    "high": _first(target, "targetHigh")},
        "upgradesDowngrades": changes,
    }


# ---------- yfinance ----------

def yf_quote(ticker, ticker_factory=yf.Ticker):
    info = ticker_factory(ticker).info or {}
    price = info.get("regularMarketPrice") or info.get("currentPrice")
    if price is None:
        raise LookupError(f"Unknown or unpriced ticker: {ticker}")
    previous = info.get("regularMarketPreviousClose") or info.get("previousClose")
    return {"name": info.get("longName") or info.get("shortName") or ticker,
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "price": price,
            "dayChangePct": ((price / previous - 1) * 100) if previous else None,
            "marketCap": info.get("marketCap"),
            "peRatio": info.get("trailingPE"),
            "week52Low": info.get("fiftyTwoWeekLow"),
            "week52High": info.get("fiftyTwoWeekHigh")}


def yf_prices(ticker, ticker_factory=yf.Ticker):
    history = ticker_factory(ticker).history(period="1y", interval="1d",
                                             auto_adjust=True)
    if history is None or history.empty:
        raise LookupError(f"No price history for {ticker}")
    return history["Close"]


def _df_value(frame, row_name, column=0):
    try:
        if frame is None or frame.empty or row_name not in frame.index:
            return None
        value = frame.loc[row_name].iloc[column]
        return None if value != value else float(value)   # NaN check
    except (IndexError, KeyError, TypeError):
        return None


def yf_fundamentals(ticker, ticker_factory=yf.Ticker):
    t = ticker_factory(ticker)
    income = t.quarterly_income_stmt
    balance = t.quarterly_balance_sheet
    cashflow = t.quarterly_cashflow
    info = t.info or {}

    columns = list(income.columns)[:8] if income is not None and not income.empty else []
    quarters, revenues, eps_values, margins = [], [], [], []
    for index in range(min(len(columns), 8)):
        revenue = _df_value(income, "Total Revenue", index)
        net_income = _df_value(income, "Net Income", index)
        eps = _df_value(income, "Diluted EPS", index)
        revenues.append(revenue)
        eps_values.append(eps)
        margins.append((net_income / revenue) if revenue and net_income is not None
                       else None)
        if index < 4:
            quarters.append({"date": str(columns[index].date()),
                             "revenue": revenue, "netIncome": net_income,
                             "eps": eps,
                             "netMargin": margins[index]})

    def growth(values):
        if len(values) < 8 or any(v is None for v in values):
            return None
        prior = sum(values[4:8])
        return (sum(values[:4]) / prior - 1) if prior else None

    fcf_values = [_df_value(cashflow, "Free Cash Flow", i) for i in range(4)]
    fcf_ttm = sum(fcf_values) if all(v is not None for v in fcf_values) \
        and len(fcf_values) == 4 else None
    market_cap = info.get("marketCap")
    equity = _df_value(balance, "Stockholders Equity")
    debt = _df_value(balance, "Total Debt")
    assets = _df_value(balance, "Current Assets")
    liabilities = _df_value(balance, "Current Liabilities")
    net_income_ttm = sum(v for v in (_df_value(income, "Net Income", i)
                                     for i in range(4)) if v is not None) or None

    metrics = {
        "revenueGrowth": growth(revenues),
        "epsGrowth": growth(eps_values),
        "netMargin": margins[0] if margins else None,
        "netMarginYearAgo": margins[4] if len(margins) > 4 else None,
        "roe": (net_income_ttm / equity) if net_income_ttm and equity else None,
        "fcfTTM": fcf_ttm,
        "debtToEquity": (debt / equity) if debt is not None and equity else None,
        "currentRatio": (assets / liabilities) if assets and liabilities else None,
        "peTTM": info.get("trailingPE"),
        "peg": info.get("pegRatio") or info.get("trailingPegRatio"),
        "fcfYield": (fcf_ttm / market_cap) if fcf_ttm and market_cap else None,
    }
    fundamentals = {"quarters": quarters, "annual": None,
                    "ratios": {"peTTM": metrics["peTTM"], "peg": metrics["peg"],
                               "priceToBook": info.get("priceToBook"),
                               "evToEBITDA": None,
                               "roe": metrics["roe"],
                               "debtToEquity": metrics["debtToEquity"],
                               "currentRatio": metrics["currentRatio"],
                               "fcfYield": metrics["fcfYield"],
                               "netMargin": metrics["netMargin"]}}
    return fundamentals, metrics


def yf_analyst(ticker, ticker_factory=yf.Ticker):
    t = ticker_factory(ticker)
    targets = getattr(t, "analyst_price_targets", None) or {}
    summary = getattr(t, "recommendations_summary", None)
    ratings = {"strongBuy": 0, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0}
    if summary is not None and not summary.empty:
        latest = summary.iloc[0]
        ratings = {"strongBuy": int(latest.get("strongBuy", 0)),
                   "buy": int(latest.get("buy", 0)),
                   "hold": int(latest.get("hold", 0)),
                   "sell": int(latest.get("sell", 0)),
                   "strongSell": int(latest.get("strongSell", 0))}
    changes = []
    upgrades = getattr(t, "upgrades_downgrades", None)
    if upgrades is not None and not upgrades.empty:
        for timestamp, row in upgrades.head(10).iterrows():
            changes.append({"date": str(timestamp.date()),
                            "firm": row.get("Firm"),
                            "fromGrade": row.get("FromGrade"),
                            "toGrade": row.get("ToGrade"),
                            "action": row.get("Action")})
    return {"ratings": ratings,
            "targets": {"low": targets.get("low"), "mean": targets.get("mean"),
                        "high": targets.get("high")},
            "upgradesDowngrades": changes}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_analysis_sources.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add analysis_sources.py test_analysis_sources.py
git commit -m "feat: FMP client with gated-endpoint fallback memory, yfinance sources"
```

---

### Task 3: `analysis_sources.py` part 2 — Alpha Vantage client, news/technicals/macro, local technicals

**Files:**
- Modify: `analysis_sources.py` (append to Task 2's module)
- Test: `test_analysis_sources.py` (append)

**Interfaces:**
- Consumes: `_http_get_json`, `_first` from Task 2.
- Produces (used by Task 5's `BundleFetcher`):
  - `class QuotaExhausted(Exception)` — AV daily/minute quota hit.
  - `class AVClient(api_key, fetch_json=_http_get_json)` with `.get(function, **params) -> dict` (raises `QuotaExhausted`), `.calls_used: int`.
  - `parse_news(payload: dict) -> dict` → `{"aggregateScore": float|None, "aggregateLabel": str|None, "articles": [≤15 {"title","source","url","publishedAt","sentimentScore","sentimentLabel","topics":[str]}]}`
  - `parse_technicals(rsi_payload, macd_payload, sma50_payload, sma200_payload, close: pandas.Series) -> dict` → the API `technicals` section: `{"prices":[≤250 {"date","close"}], "sma50":[≤250 {"date","value"}], "sma200":[...], "rsi":{"value","state"}, "macd":{"macd","signal","state"}}`
  - `compute_local_technicals(close: pandas.Series) -> dict` → identical shape (pandas RSI-14 Wilder, MACD 12/26/9, SMA 50/200).
  - `parse_macro(treasury_payload, cpi_payload, fedfunds_payload, unemployment_payload) -> dict` → `{"treasury10y": float|None, "fedFunds": float|None, "cpiYoY": float|None, "unemployment": float|None}` (cpiYoY = latest index / index 12 months earlier − 1, as a percentage).
  - `sentiment_label(score: float) -> str` using Alpha Vantage's bands: ≤−0.35 Bearish; ≤−0.15 Somewhat-Bearish; <0.15 Neutral; <0.35 Somewhat-Bullish; else Bullish.

- [ ] **Step 1: Write the failing tests (append to `test_analysis_sources.py`)**

```python
# ---------- Alpha Vantage ----------
import pandas as pd


def test_av_client_counts_calls_and_builds_url():
    seen = []
    def fake_fetch(url):
        seen.append(url)
        return {"feed": []}
    client = src.AVClient("AVSECRET", fetch_json=fake_fetch)
    client.get("NEWS_SENTIMENT", tickers="AAPL")
    assert client.calls_used == 1
    assert "function=NEWS_SENTIMENT" in seen[0]
    assert "tickers=AAPL" in seen[0] and "apikey=AVSECRET" in seen[0]


@pytest.mark.parametrize("payload", [
    {"Note": "Thank you ... 25 requests per day"},
    {"Information": "rate limit is 25 requests per day"},
])
def test_av_quota_message_raises_quota_exhausted(payload):
    client = src.AVClient("K", fetch_json=lambda url: payload)
    with pytest.raises(src.QuotaExhausted):
        client.get("RSI", symbol="AAPL")


def article(score, minutes):
    return {"title": f"t{minutes}", "source": "Reuters",
            "url": "https://example.com/a",
            "time_published": f"20260829T10{minutes:02d}00",
            "overall_sentiment_score": score,
            "overall_sentiment_label": "Neutral",
            "topics": [{"topic": "Earnings"}, {"topic": "Technology"}]}


def test_parse_news_trims_to_15_and_aggregates():
    payload = {"feed": [article(0.4, m) for m in range(20)]}
    news = src.parse_news(payload)
    assert len(news["articles"]) == 15
    assert news["aggregateScore"] == pytest.approx(0.4)
    assert news["aggregateLabel"] == "Bullish"
    first = news["articles"][0]
    assert first["publishedAt"] == "2026-08-29T10:00:00"
    assert first["topics"] == ["Earnings", "Technology"]


def test_parse_news_empty_feed():
    news = src.parse_news({"feed": []})
    assert news["articles"] == []
    assert news["aggregateScore"] is None


@pytest.mark.parametrize("score,label", [
    (-0.5, "Bearish"), (-0.2, "Somewhat-Bearish"), (0.0, "Neutral"),
    (0.2, "Somewhat-Bullish"), (0.5, "Bullish"),
])
def test_sentiment_label_bands(score, label):
    assert src.sentiment_label(score) == label


def close_series(n=300, start=100.0, step=0.5):
    dates = pd.date_range(end="2026-08-28", periods=n, freq="B")
    return pd.Series([start + i * step for i in range(n)], index=dates)


RSI_PAYLOAD = {"Technical Analysis: RSI": {
    "2026-08-28": {"RSI": "71.2"}, "2026-08-27": {"RSI": "65.0"}}}
MACD_PAYLOAD = {"Technical Analysis: MACD": {
    "2026-08-28": {"MACD": "2.10", "MACD_Signal": "1.80", "MACD_Hist": "0.30"}}}
SMA50_PAYLOAD = {"Technical Analysis: SMA": {
    "2026-08-28": {"SMA": "230.5"}, "2026-08-27": {"SMA": "229.9"}}}
SMA200_PAYLOAD = {"Technical Analysis: SMA": {
    "2026-08-28": {"SMA": "205.1"}}}


def test_parse_technicals_from_av_payloads():
    technicals = src.parse_technicals(
        RSI_PAYLOAD, MACD_PAYLOAD, SMA50_PAYLOAD, SMA200_PAYLOAD,
        close_series())
    assert technicals["rsi"] == {"value": 71.2, "state": "overbought"}
    assert technicals["macd"]["state"] == "bullish"
    assert technicals["sma50"][-1] == {"date": "2026-08-28", "value": 230.5}
    assert len(technicals["prices"]) == 250          # trimmed from 300
    assert set(technicals["prices"][0]) == {"date", "close"}


def test_compute_local_technicals_shape_and_states():
    technicals = src.compute_local_technicals(close_series())
    assert len(technicals["prices"]) == 250
    assert len(technicals["sma50"]) <= 250
    # steadily rising series: RSI near 100, MACD above signal, SMA50 > SMA200
    assert technicals["rsi"]["value"] > 70
    assert technicals["rsi"]["state"] == "overbought"
    assert technicals["macd"]["state"] == "bullish"
    assert technicals["sma50"][-1]["value"] > technicals["sma200"][-1]["value"]


@pytest.mark.parametrize("value,state", [
    (75.0, "overbought"), (50.0, "neutral"), (25.0, "oversold")])
def test_rsi_state_bands(value, state):
    assert src._rsi_state(value) == state


def econ(values):   # newest first, monthly
    return {"data": [{"date": d, "value": str(v)} for d, v in values]}


def test_parse_macro():
    cpi_values = [(f"2026-{m:02d}-01", 320.0) for m in range(7, 0, -1)]
    cpi_values += [(f"2025-{m:02d}-01", 310.0) for m in range(12, 6, -1)]
    cpi_values += [("2025-06-01", 308.0)]
    macro = src.parse_macro(
        econ([("2026-08-01", 4.25)]),
        econ(cpi_values),
        econ([("2026-08-01", 3.75)]),
        econ([("2026-08-01", 4.1)]))
    assert macro["treasury10y"] == 4.25
    assert macro["fedFunds"] == 3.75
    assert macro["unemployment"] == 4.1
    # latest 2026-07 = 320 vs 2025-07 = 310 → +3.2258%
    assert macro["cpiYoY"] == pytest.approx((320.0 / 310.0 - 1) * 100)


def test_parse_macro_missing_series_is_none():
    macro = src.parse_macro({}, {}, None, econ([]))
    assert macro == {"treasury10y": None, "fedFunds": None,
                     "cpiYoY": None, "unemployment": None}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_analysis_sources.py -v -k "av or news or sentiment or technicals or rsi or macro"`
Expected: FAIL with `AttributeError: module 'analysis_sources' has no attribute 'AVClient'` (and similar)

- [ ] **Step 3: Write the implementation (append to `analysis_sources.py`)**

```python
# ---------- Alpha Vantage ----------

import pandas as pd

AV_BASE = "https://www.alphavantage.co/query"


class QuotaExhausted(Exception):
    """Alpha Vantage daily or per-minute quota hit."""


class AVClient:
    def __init__(self, api_key, fetch_json=_http_get_json):
        self.api_key = api_key
        self.fetch_json = fetch_json
        self.calls_used = 0

    def get(self, function, **params):
        params.update({"function": function, "apikey": self.api_key})
        url = f"{AV_BASE}?{urllib.parse.urlencode(params)}"
        payload = self.fetch_json(url)
        message = payload.get("Note") or payload.get("Information") or ""
        if "request" in message.lower() or "limit" in message.lower():
            raise QuotaExhausted(f"{function}: {message}")
        if payload.get("Error Message"):
            raise RuntimeError(f"AV {function}: {payload['Error Message']}")
        self.calls_used += 1
        return payload


def sentiment_label(score):
    if score <= -0.35:
        return "Bearish"
    if score <= -0.15:
        return "Somewhat-Bearish"
    if score < 0.15:
        return "Neutral"
    if score < 0.35:
        return "Somewhat-Bullish"
    return "Bullish"


def _iso_time(av_time):
    # "20260829T101500" -> "2026-08-29T10:15:00"
    if not av_time or len(av_time) < 15:
        return av_time
    d, t = av_time[:8], av_time[9:15]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}"


def parse_news(payload):
    feed = (payload or {}).get("feed") or []
    articles = []
    for item in feed[:15]:
        score = item.get("overall_sentiment_score")
        articles.append({
            "title": item.get("title"),
            "source": item.get("source"),
            "url": item.get("url"),
            "publishedAt": _iso_time(item.get("time_published")),
            "sentimentScore": score,
            "sentimentLabel": item.get("overall_sentiment_label")
                or (sentiment_label(score) if score is not None else None),
            "topics": [t.get("topic") for t in item.get("topics") or []],
        })
    scores = [a["sentimentScore"] for a in articles
              if a["sentimentScore"] is not None]
    aggregate = sum(scores) / len(scores) if scores else None
    return {"aggregateScore": aggregate,
            "aggregateLabel": sentiment_label(aggregate)
                if aggregate is not None else None,
            "articles": articles}


def _rsi_state(value):
    if value is None:
        return None
    if value >= 70:
        return "overbought"
    if value <= 30:
        return "oversold"
    return "neutral"


def _macd_state(macd, signal):
    if macd is None or signal is None:
        return None
    return "bullish" if macd > signal else "bearish"


def _trim_series(pairs, limit=250):
    """pairs: [(date_str, float)] oldest-first -> last `limit` dicts."""
    return [{"date": d, "value": v} for d, v in pairs[-limit:]]


def _prices_from_close(close, limit=250):
    tail = close.dropna().iloc[-limit:]
    return [{"date": str(index.date()), "close": round(float(value), 4)}
            for index, value in tail.items()]


def _av_series(payload, series_key, value_key):
    block = (payload or {}).get(series_key) or {}
    pairs = sorted((date, float(values[value_key]))
                   for date, values in block.items() if value_key in values)
    return pairs   # oldest first


def parse_technicals(rsi_payload, macd_payload, sma50_payload,
                     sma200_payload, close):
    rsi_pairs = _av_series(rsi_payload, "Technical Analysis: RSI", "RSI")
    rsi_value = rsi_pairs[-1][1] if rsi_pairs else None
    macd_block = (macd_payload or {}).get("Technical Analysis: MACD") or {}
    macd_value = signal_value = None
    if macd_block:
        latest = macd_block[max(macd_block)]
        macd_value = float(latest["MACD"])
        signal_value = float(latest["MACD_Signal"])
    return {
        "prices": _prices_from_close(close),
        "sma50": _trim_series(_av_series(sma50_payload,
                                         "Technical Analysis: SMA", "SMA")),
        "sma200": _trim_series(_av_series(sma200_payload,
                                          "Technical Analysis: SMA", "SMA")),
        "rsi": {"value": rsi_value, "state": _rsi_state(rsi_value)},
        "macd": {"macd": macd_value, "signal": signal_value,
                 "state": _macd_state(macd_value, signal_value)},
    }


def compute_local_technicals(close):
    close = close.dropna()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / loss)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()

    def series_pairs(series):
        clean = series.dropna()
        return [(str(index.date()), round(float(value), 4))
                for index, value in clean.items()]

    rsi_value = round(float(rsi.iloc[-1]), 2) if len(rsi) else None
    macd_value = round(float(macd.iloc[-1]), 4) if len(macd) else None
    signal_value = round(float(signal.iloc[-1]), 4) if len(signal) else None
    return {
        "prices": _prices_from_close(close),
        "sma50": _trim_series(series_pairs(close.rolling(50).mean())),
        "sma200": _trim_series(series_pairs(close.rolling(200).mean())),
        "rsi": {"value": rsi_value, "state": _rsi_state(rsi_value)},
        "macd": {"macd": macd_value, "signal": signal_value,
                 "state": _macd_state(macd_value, signal_value)},
    }


def _latest_econ(payload):
    data = (payload or {}).get("data") or []
    for row in data:
        try:
            return float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _cpi_yoy(payload):
    data = (payload or {}).get("data") or []
    values = []
    for row in data:
        try:
            values.append((row["date"], float(row["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(values) < 13:
        return None
    latest, year_ago = values[0][1], values[12][1]
    return (latest / year_ago - 1) * 100 if year_ago else None


def parse_macro(treasury_payload, cpi_payload, fedfunds_payload,
                unemployment_payload):
    return {"treasury10y": _latest_econ(treasury_payload),
            "fedFunds": _latest_econ(fedfunds_payload),
            "cpiYoY": _cpi_yoy(cpi_payload),
            "unemployment": _latest_econ(unemployment_payload)}
```

Note: Task 2's module already imports `urllib.parse`; keep the single `import pandas as pd` at the top of the file with the other imports, not mid-file.

- [ ] **Step 4: Run the full sources test file**

Run: `python3 -m pytest test_analysis_sources.py -v`
Expected: all PASS (Task 2's tests must still pass)

- [ ] **Step 5: Commit**

```bash
git add analysis_sources.py test_analysis_sources.py
git commit -m "feat: Alpha Vantage news/technicals/macro parsing and local technicals"
```

---

### Task 4: `firestore_store.py` + credentials hygiene

**Files:**
- Create: `firestore_store.py`, `.gitignore`
- Modify: `requirements.txt`
- Test: `test_analyze_server.py` (store tests live here per spec; create the file now)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (used by Task 5):
  - `class MemoryBackend()` — dict-backed, same methods as `FirestoreBackend`.
  - `class Store(project_id=None, backend=None)` with `.kind: "firestore"|"memory"` and methods:
    - `get_daily(ticker: str, date: str) -> dict|None`
    - `put_daily(ticker: str, date: str, bundle: dict) -> None`
    - `get_macro(date: str) -> dict|None` / `put_macro(date: str, doc: dict) -> None`
    - `upsert_registry(ticker: str, name: str|None, sector: str|None, date: str, summary: dict) -> None` — `summary = {"grade","passRatio","price","targetMean","sentimentScore"}`
    - `list_registry() -> list[dict]` — `[{"ticker","name","latestDate","latestGrade","updatedAt"}]` ordered by `updatedAt` descending
    - `get_registry(ticker: str) -> dict|None` — full registry doc incl. `summaries`
  - `Store(backend=...)` skips Firestore entirely (tests). Without `backend`, it tries Firestore and falls back to `MemoryBackend` on any exception — at init or later per-call (switches permanently, prints a warning).
  - Timestamps are ISO-8601 UTC strings (JSON-serializable, comparable).

- [ ] **Step 1: Write `.gitignore` and update requirements**

`.gitignore` (exactly the spec's list):

```
.env
firebase-service-account.json
venv/
__pycache__/
```

Append to `requirements.txt`:

```
firebase-admin>=6.5
```

Then run: `source venv/bin/activate && pip install -r requirements.txt` (and `pip install pytest` if missing).

- [ ] **Step 2: Write the failing tests (create `test_analyze_server.py`)**

```python
# test_analyze_server.py
import pytest

from firestore_store import MemoryBackend, Store


BUNDLE = {"ticker": "AAPL", "snapshot": {"price": 230.0}, "meta": {}}
SUMMARY = {"grade": "B", "passRatio": 0.7, "price": 230.0,
           "targetMean": 245.5, "sentimentScore": 0.2}


def memory_store():
    return Store(backend=MemoryBackend())


def test_daily_round_trip_and_miss():
    store = memory_store()
    assert store.get_daily("AAPL", "2026-08-29") is None
    store.put_daily("AAPL", "2026-08-29", BUNDLE)
    assert store.get_daily("AAPL", "2026-08-29") == BUNDLE
    assert store.get_daily("AAPL", "2026-08-28") is None


def test_macro_round_trip():
    store = memory_store()
    assert store.get_macro("2026-08-29") is None
    store.put_macro("2026-08-29", {"treasury10y": 4.25})
    assert store.get_macro("2026-08-29") == {"treasury10y": 4.25}


def test_registry_upsert_merges_summaries_and_tracks_latest():
    store = memory_store()
    store.upsert_registry("AAPL", "Apple Inc.", "Technology",
                          "2026-08-28", dict(SUMMARY, grade="C"))
    store.upsert_registry("AAPL", "Apple Inc.", "Technology",
                          "2026-08-29", SUMMARY)
    doc = store.get_registry("AAPL")
    assert set(doc["summaries"]) == {"2026-08-28", "2026-08-29"}
    assert doc["latestDate"] == "2026-08-29"
    assert doc["latestGrade"] == "B"
    assert doc["name"] == "Apple Inc."
    assert doc["updatedAt"]          # ISO string set


def test_registry_upsert_older_date_does_not_regress_latest():
    store = memory_store()
    store.upsert_registry("AAPL", "Apple Inc.", None, "2026-08-29", SUMMARY)
    store.upsert_registry("AAPL", "Apple Inc.", None,
                          "2026-08-27", dict(SUMMARY, grade="D"))
    doc = store.get_registry("AAPL")
    assert doc["latestDate"] == "2026-08-29"
    assert doc["latestGrade"] == "B"


def test_list_registry_ordered_by_updated_at_desc():
    store = memory_store()
    store.upsert_registry("MSFT", "Microsoft", None, "2026-08-29", SUMMARY)
    store.upsert_registry("AAPL", "Apple Inc.", None, "2026-08-29", SUMMARY)
    rows = store.list_registry()
    assert [r["ticker"] for r in rows] == ["AAPL", "MSFT"]  # most recent first
    assert set(rows[0]) == {"ticker", "name", "latestDate",
                            "latestGrade", "updatedAt"}


class ExplodingBackend:
    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise ConnectionError("firestore down")
        return boom


def test_store_switches_to_memory_when_backend_raises(capsys):
    store = Store(backend=ExplodingBackend())
    store.kind = "firestore"   # simulate a live backend that starts failing
    store.put_daily("AAPL", "2026-08-29", BUNDLE)         # must not raise
    assert store.kind == "memory"
    assert store.get_daily("AAPL", "2026-08-29") == BUNDLE
    assert "WARNING" in capsys.readouterr().out
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest test_analyze_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'firestore_store'`

- [ ] **Step 4: Write the implementation**

```python
# firestore_store.py
"""Firestore persistence with an in-memory fallback. All timestamps are
ISO-8601 UTC strings so docs stay JSON-serializable and comparable."""
import datetime


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")


def _registry_update(existing, ticker, name, sector, date, summary):
    doc = existing or {"ticker": ticker, "summaries": {}}
    doc["summaries"][date] = summary
    if name:
        doc["name"] = name
    if sector:
        doc["sector"] = sector
    if date >= doc.get("latestDate", ""):
        doc["latestDate"] = date
        doc["latestGrade"] = summary.get("grade")
    doc["updatedAt"] = _now_iso()
    return doc


class MemoryBackend:
    def __init__(self):
        self.daily = {}
        self.macro = {}
        self.registry = {}

    def get_daily(self, ticker, date):
        return self.daily.get((ticker, date))

    def put_daily(self, ticker, date, bundle):
        self.daily[(ticker, date)] = bundle

    def get_macro(self, date):
        return self.macro.get(date)

    def put_macro(self, date, doc):
        self.macro[date] = doc

    def upsert_registry(self, ticker, name, sector, date, summary):
        self.registry[ticker] = _registry_update(
            self.registry.get(ticker), ticker, name, sector, date, summary)

    def get_registry(self, ticker):
        return self.registry.get(ticker)

    def list_registry(self):
        rows = [{"ticker": d["ticker"], "name": d.get("name"),
                 "latestDate": d.get("latestDate"),
                 "latestGrade": d.get("latestGrade"),
                 "updatedAt": d.get("updatedAt")}
                for d in self.registry.values()]
        return sorted(rows, key=lambda r: r["updatedAt"] or "", reverse=True)


class FirestoreBackend:
    """Requires GOOGLE_APPLICATION_CREDENTIALS in the environment (set by
    analyze_server from .env). Raises on any init problem — Store catches."""

    def __init__(self, project_id):
        import firebase_admin
        from firebase_admin import firestore
        if not firebase_admin._apps:
            firebase_admin.initialize_app(options={"projectId": project_id})
        self.db = firestore.client()

    def _daily_ref(self, ticker, date):
        return (self.db.collection("tickers").document(ticker)
                .collection("daily").document(date))

    def get_daily(self, ticker, date):
        doc = self._daily_ref(ticker, date).get()
        return doc.to_dict() if doc.exists else None

    def put_daily(self, ticker, date, bundle):
        self._daily_ref(ticker, date).set(bundle)

    def get_macro(self, date):
        doc = self.db.collection("macro").document(date).get()
        return doc.to_dict() if doc.exists else None

    def put_macro(self, date, doc):
        self.db.collection("macro").document(date).set(doc)

    def upsert_registry(self, ticker, name, sector, date, summary):
        ref = self.db.collection("tickers").document(ticker)
        snapshot = ref.get()
        existing = snapshot.to_dict() if snapshot.exists else None
        ref.set(_registry_update(existing, ticker, name, sector, date,
                                 summary))

    def get_registry(self, ticker):
        doc = self.db.collection("tickers").document(ticker).get()
        return doc.to_dict() if doc.exists else None

    def list_registry(self):
        rows = []
        for snapshot in self.db.collection("tickers").stream():
            d = snapshot.to_dict() or {}
            rows.append({"ticker": d.get("ticker", snapshot.id),
                         "name": d.get("name"),
                         "latestDate": d.get("latestDate"),
                         "latestGrade": d.get("latestGrade"),
                         "updatedAt": d.get("updatedAt")})
        return sorted(rows, key=lambda r: r["updatedAt"] or "", reverse=True)


class Store:
    def __init__(self, project_id=None, backend=None):
        if backend is not None:
            self.backend = backend
            self.kind = "memory"
            return
        try:
            self.backend = FirestoreBackend(project_id)
            self.kind = "firestore"
        except Exception as error:
            print(f"WARNING: Firestore unavailable ({error}); "
                  "using in-memory store for this process")
            self.backend = MemoryBackend()
            self.kind = "memory"

    def _safe(self, method, *args):
        try:
            return getattr(self.backend, method)(*args)
        except Exception as error:
            if self.kind != "firestore":
                raise
            print(f"WARNING: Firestore error ({error}); "
                  "switching to in-memory store")
            self.backend = MemoryBackend()
            self.kind = "memory"
            return getattr(self.backend, method)(*args)

    def get_daily(self, ticker, date):
        return self._safe("get_daily", ticker, date)

    def put_daily(self, ticker, date, bundle):
        return self._safe("put_daily", ticker, date, bundle)

    def get_macro(self, date):
        return self._safe("get_macro", date)

    def put_macro(self, date, doc):
        return self._safe("put_macro", date, doc)

    def upsert_registry(self, ticker, name, sector, date, summary):
        return self._safe("upsert_registry", ticker, name, sector, date,
                          summary)

    def get_registry(self, ticker):
        return self._safe("get_registry", ticker)

    def list_registry(self):
        return self._safe("list_registry")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest test_analyze_server.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add firestore_store.py test_analyze_server.py .gitignore requirements.txt
git commit -m "feat: Firestore store with registry docs and in-memory fallback"
```

---

### Task 5: `analyze_server.py` — orchestration, API, HTTP server

**Files:**
- Create: `analyze_server.py`
- Test: `test_analyze_server.py` (append to Task 4's file)

**Interfaces:**
- Consumes: `analysis_sources` (Tasks 2–3: `normalize_ticker`, `FMPClient`, `AVClient`, `GatedEndpoint`, `QuotaExhausted`, all parsers, `yf_*`), `value_grade.compute_verdict` (Task 1), `firestore_store.Store` (Task 4).
- Produces:
  - `load_env(path=".env") -> dict` — tiny stdlib parser (comments, blanks, `KEY=value`, strips quotes).
  - `class NotFound(Exception)`
  - `class BundleFetcher(fmp, av, local_technicals=False, src=analysis_sources, grader=value_grade)` with `.fetch(ticker) -> bundle_dict` (raises `LookupError` for unknown tickers; every other section degrades independently and is recorded in `meta.sections`) and `.fetch_macro() -> dict`.
  - `class Analyzer(store, fetcher, today=None)` with `.analyze(ticker, date=None, refresh=False) -> bundle` (raises `NotFound`/`LookupError`), `.analyzed() -> list`, `.history(ticker) -> dict` (raises `NotFound`). `today` is an injectable `() -> "YYYY-MM-DD"` callable for tests.
  - `handle_api(path: str, query: dict[str, list[str]], analyzer) -> (status: int, payload: dict)` — pure routing, no sockets.
  - `main()` — argparse `--port` (default 8000), `--local-technicals`; serves static files + API via `ThreadingHTTPServer`.
- Bundle shape is the spec's API contract: keys `ticker, date, snapshot, verdict, fundamentals, analyst, technicals, news, macro, meta`.

- [ ] **Step 1: Write the failing tests (append to `test_analyze_server.py`)**

```python
import types

import pandas as pd

import analysis_sources
import analyze_server
from analyze_server import Analyzer, BundleFetcher, NotFound, handle_api


# ---------- load_env ----------

def test_load_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nFMP_KEY=abc123\nALPHAVANTAGE_KEY='quoted'\n"
        "GOOGLE_APPLICATION_CREDENTIALS=./firebase-service-account.json\n"
        "BROKEN LINE NO EQUALS\n")
    env = analyze_server.load_env(env_file)
    assert env == {"FMP_KEY": "abc123", "ALPHAVANTAGE_KEY": "quoted",
                   "GOOGLE_APPLICATION_CREDENTIALS":
                       "./firebase-service-account.json"}


def test_load_env_missing_file(tmp_path):
    assert analyze_server.load_env(tmp_path / "nope") == {}


# ---------- Analyzer orchestration (stub store + stub fetcher) ----------

class StubFetcher:
    def __init__(self):
        self.fetched = []
        self.macro_calls = 0

    def fetch(self, ticker):
        if ticker == "ZZZZZZ":
            raise LookupError(f"Unknown or unpriced ticker: {ticker}")
        self.fetched.append(ticker)
        return {"ticker": ticker,
                "snapshot": {"name": "Apple Inc.", "sector": "Technology",
                             "price": 230.0},
                "verdict": {"grade": "B", "passes": 7, "evaluated": 10},
                "fundamentals": {"quarters": []},
                "analyst": {"targets": {"mean": 245.5}},
                "technicals": {"rsi": {"value": 55.0, "state": "neutral"}},
                "news": {"aggregateScore": 0.2},
                "meta": {"fetchedAt": "2026-08-29T12:00:00+00:00",
                         "sections": {"fundamentals": "fmp"},
                         "avCallsUsed": 5, "fmpCallsUsed": 10}}

    def fetch_macro(self):
        self.macro_calls += 1
        return {"treasury10y": 4.25, "fedFunds": 3.75, "cpiYoY": 3.2,
                "unemployment": 4.1, "fetchedAt": "2026-08-29T12:00:00+00:00"}


def make_analyzer():
    store = Store(backend=MemoryBackend())
    fetcher = StubFetcher()
    analyzer = Analyzer(store, fetcher, today=lambda: "2026-08-29")
    return analyzer, store, fetcher


def test_store_miss_fetches_and_writes_daily_and_registry():
    analyzer, store, fetcher = make_analyzer()
    bundle = analyzer.analyze("AAPL")
    assert fetcher.fetched == ["AAPL"]
    assert bundle["date"] == "2026-08-29"
    assert bundle["macro"]["treasury10y"] == 4.25
    assert bundle["meta"]["fromStore"] is False
    assert store.get_daily("AAPL", "2026-08-29") is not None
    registry = store.get_registry("AAPL")
    assert registry["latestGrade"] == "B"
    summary = registry["summaries"]["2026-08-29"]
    assert summary == {"grade": "B", "passRatio": 0.7, "price": 230.0,
                       "targetMean": 245.5, "sentimentScore": 0.2}


def test_store_hit_serves_without_fetching():
    analyzer, store, fetcher = make_analyzer()
    analyzer.analyze("AAPL")
    bundle = analyzer.analyze("AAPL")
    assert fetcher.fetched == ["AAPL"]          # only the first call fetched
    assert bundle["meta"]["fromStore"] is True
    assert bundle["meta"]["store"] == "memory"


def test_refresh_forces_refetch_and_overwrite():
    analyzer, store, fetcher = make_analyzer()
    analyzer.analyze("AAPL")
    bundle = analyzer.analyze("AAPL", refresh=True)
    assert fetcher.fetched == ["AAPL", "AAPL"]
    assert bundle["meta"]["fromStore"] is False


def test_date_request_is_store_only():
    analyzer, store, fetcher = make_analyzer()
    with pytest.raises(NotFound):
        analyzer.analyze("AAPL", date="2026-08-28")
    assert fetcher.fetched == []                 # never fetched externally
    store.put_daily("AAPL", "2026-08-28", dict(BUNDLE, date="2026-08-28"))
    bundle = analyzer.analyze("AAPL", date="2026-08-28")
    assert bundle["date"] == "2026-08-28"
    assert bundle["meta"]["fromStore"] is True
    assert fetcher.fetched == []


def test_macro_fetched_once_per_day_and_shared():
    analyzer, store, fetcher = make_analyzer()
    analyzer.analyze("AAPL")
    analyzer.analyze("MSFT")
    assert fetcher.macro_calls == 1


def test_ticker_is_normalized_before_everything():
    analyzer, store, fetcher = make_analyzer()
    analyzer.analyze(" brk.b ")
    assert fetcher.fetched == ["BRK-B"]
    assert store.get_registry("BRK-B") is not None


def test_history_and_analyzed():
    analyzer, store, fetcher = make_analyzer()
    analyzer.analyze("AAPL")
    history = analyzer.history("aapl")
    assert history["ticker"] == "AAPL"
    assert "2026-08-29" in history["summaries"]
    assert analyzer.analyzed()[0]["ticker"] == "AAPL"
    with pytest.raises(NotFound):
        analyzer.history("NVDA")


# ---------- handle_api routing ----------

def test_handle_api_analyze_ok_and_errors():
    analyzer, _, _ = make_analyzer()
    status, payload = handle_api("/api/analyze", {"ticker": ["AAPL"]}, analyzer)
    assert status == 200 and payload["ticker"] == "AAPL"
    status, payload = handle_api("/api/analyze", {}, analyzer)
    assert status == 400
    status, payload = handle_api(
        "/api/analyze",
        {"ticker": ["AAPL"], "date": ["2026-08-28"], "refresh": ["1"]},
        analyzer)
    assert status == 400 and "exclusive" in payload["error"]
    status, payload = handle_api(
        "/api/analyze", {"ticker": ["AAPL"], "date": ["not-a-date"]}, analyzer)
    assert status == 400
    status, payload = handle_api(
        "/api/analyze", {"ticker": ["ZZZZZZ"]}, analyzer)
    assert status == 404
    status, payload = handle_api(
        "/api/analyze", {"ticker": ["AAPL"], "date": ["2026-01-01"]}, analyzer)
    assert status == 404                          # store-only miss


def test_handle_api_analyzed_history_and_unknown_path():
    analyzer, _, _ = make_analyzer()
    handle_api("/api/analyze", {"ticker": ["AAPL"]}, analyzer)
    status, payload = handle_api("/api/analyzed", {}, analyzer)
    assert status == 200 and payload["tickers"][0]["ticker"] == "AAPL"
    status, payload = handle_api("/api/history", {"ticker": ["AAPL"]}, analyzer)
    assert status == 200 and "2026-08-29" in payload["summaries"]
    status, payload = handle_api("/api/history", {"ticker": ["NVDA"]}, analyzer)
    assert status == 404
    status, payload = handle_api("/api/nope", {}, analyzer)
    assert status == 404


# ---------- BundleFetcher per-section degradation (stub clients + stub src) ----------

class GatedFMP:
    calls_used = 0
    def get(self, endpoint, **params):
        raise analysis_sources.GatedEndpoint(endpoint)


class QuotaAV:
    calls_used = 0
    def get(self, function, **params):
        raise analysis_sources.QuotaExhausted(function)


class CountingAV:
    def __init__(self):
        self.calls_used = 0
        self.functions = []
    def get(self, function, **params):
        self.calls_used += 1
        self.functions.append(function)
        return {"feed": []}


def close_stub():
    dates = pd.date_range(end="2026-08-28", periods=60, freq="B")
    return pd.Series([100.0 + i for i in range(60)], index=dates)


def stub_src(**overrides):
    functions = dict(
        normalize_ticker=analysis_sources.normalize_ticker,
        yf_quote=lambda t: {"name": "Apple Inc.", "sector": "Technology",
                            "industry": "Consumer Electronics", "price": 230.0,
                            "dayChangePct": 1.0, "marketCap": 3.5e12,
                            "peRatio": 28.0, "week52Low": 164.0,
                            "week52High": 260.0},
        yf_prices=lambda t: close_stub(),
        yf_fundamentals=lambda t: ({"quarters": [], "annual": None,
                                    "ratios": {}},
                                   {"roe": 0.30, "fcfTTM": 1.0}),
        yf_analyst=lambda t: {"ratings": {"strongBuy": 1, "buy": 1, "hold": 1,
                                          "sell": 0, "strongSell": 0},
                              "targets": {"low": 1, "mean": 2, "high": 3},
                              "upgradesDowngrades": []},
        parse_profile=analysis_sources.parse_profile,
        build_fundamentals=analysis_sources.build_fundamentals,
        parse_analyst=analysis_sources.parse_analyst,
        parse_news=analysis_sources.parse_news,
        parse_technicals=analysis_sources.parse_technicals,
        parse_macro=analysis_sources.parse_macro,
        compute_local_technicals=analysis_sources.compute_local_technicals,
    )
    functions.update(overrides)
    return types.SimpleNamespace(**functions)


def test_bundle_fetcher_degrades_each_section_independently():
    fetcher = BundleFetcher(GatedFMP(), QuotaAV(), src=stub_src())
    bundle = fetcher.fetch("AAPL")
    sections = bundle["meta"]["sections"]
    assert sections["fundamentals"] == "yfinance"
    assert sections["analyst"] == "yfinance"
    assert sections["news"] == "unavailable"
    assert sections["technicals"] == "local"     # quota → local computation
    assert bundle["news"] is None
    assert bundle["technicals"]["rsi"]["state"] in ("overbought", "neutral",
                                                    "oversold")
    assert bundle["verdict"]["grade"]            # graded from yfinance metrics
    assert bundle["snapshot"]["price"] == 230.0
    assert bundle["analyst"]["targets"]["current"] == 230.0


def test_local_technicals_flag_skips_av_technical_calls():
    av = CountingAV()
    fetcher = BundleFetcher(GatedFMP(), av, local_technicals=True,
                            src=stub_src())
    bundle = fetcher.fetch("AAPL")
    assert bundle["meta"]["sections"]["technicals"] == "local"
    assert av.functions == ["NEWS_SENTIMENT"]    # only news hit AV


def test_unknown_ticker_propagates_lookup_error():
    def raising_quote(t):
        raise LookupError(f"Unknown or unpriced ticker: {t}")
    fetcher = BundleFetcher(GatedFMP(), QuotaAV(),
                            src=stub_src(yf_quote=raising_quote))
    with pytest.raises(LookupError):
        fetcher.fetch("ZZZZZZ")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_analyze_server.py -v`
Expected: Task 4 tests PASS; new tests FAIL with `ModuleNotFoundError: No module named 'analyze_server'`

- [ ] **Step 3: Write the implementation**

```python
# analyze_server.py
"""Local analysis server: static files + /api/analyze|analyzed|history.
Run: python3 analyze_server.py [--port 8000] [--local-technicals]"""
import argparse
import datetime
import json
import os
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import analysis_sources
import firestore_store
import value_grade
from firestore_store import _now_iso


def load_env(path=".env"):
    values = {}
    env_path = Path(path)
    if not env_path.exists():
        return values
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


class NotFound(Exception):
    pass


class BundleFetcher:
    """Fetches one full bundle. Unknown ticker raises LookupError; every
    other section degrades independently, recorded in meta.sections."""

    def __init__(self, fmp, av, local_technicals=False,
                 src=analysis_sources, grader=value_grade):
        self.fmp = fmp
        self.av = av
        self.local_technicals = local_technicals
        self.src = src
        self.grader = grader

    def fetch(self, ticker):
        av_before, fmp_before = self.av.calls_used, self.fmp.calls_used
        sections = {}
        snapshot = self.src.yf_quote(ticker)      # LookupError propagates

        try:
            profile = self.src.parse_profile(
                self.fmp.get("profile", symbol=ticker))
            snapshot.update({k: v for k, v in profile.items() if v})
        except Exception:
            pass                                  # yfinance already filled it

        fundamentals = metrics = None
        try:
            fundamentals, metrics = self.src.build_fundamentals(
                self.fmp.get("income-statement", symbol=ticker,
                             period="quarter", limit=8),
                self.fmp.get("balance-sheet-statement", symbol=ticker,
                             period="quarter", limit=1),
                self.fmp.get("cash-flow-statement", symbol=ticker,
                             period="quarter", limit=4),
                self.fmp.get("income-statement", symbol=ticker,
                             period="annual", limit=1),
                self.fmp.get("ratios-ttm", symbol=ticker),
                self.fmp.get("key-metrics-ttm", symbol=ticker))
            sections["fundamentals"] = "fmp"
        except Exception:
            try:
                fundamentals, metrics = self.src.yf_fundamentals(ticker)
                sections["fundamentals"] = "yfinance"
            except Exception:
                sections["fundamentals"] = "unavailable"
        verdict = self.grader.compute_verdict(metrics) \
            if metrics is not None else None

        analyst = None
        try:
            analyst = self.src.parse_analyst(
                self.fmp.get("price-target-consensus", symbol=ticker),
                self.fmp.get("grades-consensus", symbol=ticker),
                self.fmp.get("grades", symbol=ticker, limit=10))
            sections["analyst"] = "fmp"
        except Exception:
            try:
                analyst = self.src.yf_analyst(ticker)
                sections["analyst"] = "yfinance"
            except Exception:
                sections["analyst"] = "unavailable"
        if analyst:
            analyst["targets"]["current"] = snapshot.get("price")

        technicals = None
        try:
            close = self.src.yf_prices(ticker)
        except Exception:
            close = None
        if close is None:
            sections["technicals"] = "unavailable"
        elif self.local_technicals:
            technicals = self.src.compute_local_technicals(close)
            sections["technicals"] = "local"
        else:
            try:
                technicals = self.src.parse_technicals(
                    self.av.get("RSI", symbol=ticker, interval="daily",
                                time_period=14, series_type="close"),
                    self.av.get("MACD", symbol=ticker, interval="daily",
                                series_type="close"),
                    self.av.get("SMA", symbol=ticker, interval="daily",
                                time_period=50, series_type="close"),
                    self.av.get("SMA", symbol=ticker, interval="daily",
                                time_period=200, series_type="close"),
                    close)
                sections["technicals"] = "av"
            except Exception:
                technicals = self.src.compute_local_technicals(close)
                sections["technicals"] = "local"

        news = None
        try:
            news = self.src.parse_news(
                self.av.get("NEWS_SENTIMENT", tickers=ticker,
                            sort="LATEST", limit=50))
            sections["news"] = "ok"
        except Exception:
            sections["news"] = "unavailable"

        return {"ticker": ticker,
                "snapshot": snapshot,
                "verdict": verdict,
                "fundamentals": fundamentals,
                "analyst": analyst,
                "technicals": technicals,
                "news": news,
                "meta": {"fetchedAt": _now_iso(),
                         "sections": sections,
                         "avCallsUsed": self.av.calls_used - av_before,
                         "fmpCallsUsed": self.fmp.calls_used - fmp_before}}

    def fetch_macro(self):
        macro = self.src.parse_macro(
            self.av.get("TREASURY_YIELD", interval="monthly",
                        maturity="10year"),
            self.av.get("CPI", interval="monthly"),
            self.av.get("FEDERAL_FUNDS_RATE", interval="monthly"),
            self.av.get("UNEMPLOYMENT"))
        macro["fetchedAt"] = _now_iso()
        return macro


def _summary_of(bundle):
    verdict = bundle.get("verdict") or {}
    evaluated = verdict.get("evaluated") or 0
    return {"grade": verdict.get("grade"),
            "passRatio": (verdict.get("passes", 0) / evaluated)
                if evaluated else None,
            "price": (bundle.get("snapshot") or {}).get("price"),
            "targetMean": ((bundle.get("analyst") or {}).get("targets")
                           or {}).get("mean"),
            "sentimentScore": (bundle.get("news") or {}).get("aggregateScore")}


class Analyzer:
    def __init__(self, store, fetcher, today=None):
        self.store = store
        self.fetcher = fetcher
        self._today = today or (lambda: datetime.date.today().isoformat())

    def _stored(self, bundle):
        bundle.setdefault("meta", {})["fromStore"] = True
        bundle["meta"]["store"] = self.store.kind
        return bundle

    def analyze(self, ticker, date=None, refresh=False):
        ticker = analysis_sources.normalize_ticker(ticker)
        if date:
            bundle = self.store.get_daily(ticker, date)
            if bundle is None:
                raise NotFound(f"No stored analysis for {ticker} on {date}")
            return self._stored(bundle)
        today = self._today()
        if not refresh:
            bundle = self.store.get_daily(ticker, today)
            if bundle is not None:
                return self._stored(bundle)
        bundle = self.fetcher.fetch(ticker)       # LookupError propagates
        bundle["date"] = today
        bundle["macro"] = self._macro(today)
        bundle["meta"]["sections"]["macro"] = \
            "ok" if bundle["macro"] else "unavailable"
        bundle["meta"]["fromStore"] = False
        bundle["meta"]["store"] = self.store.kind
        self.store.put_daily(ticker, today, bundle)
        self.store.upsert_registry(
            ticker, bundle["snapshot"].get("name"),
            bundle["snapshot"].get("sector"), today, _summary_of(bundle))
        return bundle

    def _macro(self, today):
        macro = self.store.get_macro(today)
        if macro is None:
            try:
                macro = self.fetcher.fetch_macro()
                self.store.put_macro(today, macro)
            except Exception:
                macro = None
        return macro

    def analyzed(self):
        return self.store.list_registry()

    def history(self, ticker):
        ticker = analysis_sources.normalize_ticker(ticker)
        doc = self.store.get_registry(ticker)
        if doc is None:
            raise NotFound(f"{ticker} has never been analyzed")
        return {"ticker": ticker, "summaries": doc.get("summaries", {})}


def handle_api(path, query, analyzer):
    def param(name):
        values = query.get(name) or []
        return values[0] if values else None

    if path == "/api/analyze":
        ticker = param("ticker")
        date = param("date")
        refresh = param("refresh") == "1"
        if not ticker:
            return 400, {"error": "ticker query parameter is required"}
        if date and refresh:
            return 400, {"error": "date and refresh are mutually exclusive"}
        if date:
            try:
                datetime.date.fromisoformat(date)
            except ValueError:
                return 400, {"error": f"Invalid date: {date} (use YYYY-MM-DD)"}
        try:
            return 200, analyzer.analyze(ticker, date=date, refresh=refresh)
        except (NotFound, LookupError) as error:
            return 404, {"error": str(error)}

    if path == "/api/analyzed":
        return 200, {"tickers": analyzer.analyzed()}

    if path == "/api/history":
        ticker = param("ticker")
        if not ticker:
            return 400, {"error": "ticker query parameter is required"}
        try:
            return 200, analyzer.history(ticker)
        except NotFound as error:
            return 404, {"error": str(error)}

    return 404, {"error": f"Unknown API path: {path}"}


class Handler(SimpleHTTPRequestHandler):
    analyzer = None    # set in main()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if not parsed.path.startswith("/api/"):
            return super().do_GET()
        status, payload = handle_api(
            parsed.path, urllib.parse.parse_qs(parsed.query), self.analyzer)
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(
        description="Stock analyzer server: static files + analysis API.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--local-technicals", action="store_true",
                        help="compute RSI/MACD/SMA locally from yfinance "
                             "prices (1 AV call/ticker instead of 5)")
    args = parser.parse_args()

    env = load_env()
    if env.get("GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS",
                              env["GOOGLE_APPLICATION_CREDENTIALS"])
    fmp = analysis_sources.FMPClient(env.get("FMP_KEY", ""))
    av = analysis_sources.AVClient(env.get("ALPHAVANTAGE_KEY", ""))
    store = firestore_store.Store(project_id=env.get("FIREBASE_PROJECT_ID"))
    fetcher = BundleFetcher(fmp, av, local_technicals=args.local_technicals)
    Handler.analyzer = Analyzer(store, fetcher)

    server = ThreadingHTTPServer(("", args.port), Handler)
    print(f"Serving http://localhost:{args.port}/stock_analyzer.html "
          f"(store: {store.kind}, technicals: "
          f"{'local' if args.local_technicals else 'alpha vantage'})")
    server.serve_forever()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m pytest -v`
Expected: all PASS (all three test files)

- [ ] **Step 5: Smoke-test the live server (manual, uses real quota: ~10 FMP + ≤9 AV calls)**

```bash
python3 analyze_server.py --local-technicals &
curl -s "http://localhost:8000/api/analyze?ticker=AAPL" | python3 -m json.tool | head -40
curl -s "http://localhost:8000/api/analyzed" | python3 -m json.tool
curl -s "http://localhost:8000/api/history?ticker=AAPL" | python3 -m json.tool
kill %1
```

Expected: JSON with grade + sections; second `analyze` call returns instantly with `"fromStore": true`. If Firestore credentials are not yet set up, the log shows the in-memory warning and `"store": "memory"` — acceptable at this stage.

- [ ] **Step 6: Commit**

```bash
git add analyze_server.py test_analyze_server.py
git commit -m "feat: analysis server with store read-through, registry, and API"
```

---

### Task 6: `stock_analyzer.html` — the page

**Files:**
- Create: `stock_analyzer.html`

**Interfaces:**
- Consumes: `GET /api/analyze?ticker=X[&date=YYYY-MM-DD|&refresh=1]`, `GET /api/analyzed`, `GET /api/history?ticker=X` (Task 5 shapes). No other dependencies — vanilla JS, no libraries.
- Produces: the user-facing page. Browser-only code — verified manually (Step 2), no unit tests.

Design rules (from spec): Ledger newspaper aesthetic (Georgia serif + Courier mono, paper/ink palette, double rules, `.stamp`, `.status`), canvas charts, every section renders independently with an inline "unavailable" note, `file://` guard, "server not running" hint, "Data as of" badge + Refresh (hidden for historical views, replaced by rotated HISTORICAL EDITION stamp).

- [ ] **Step 1: Write the page**

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Equity Dossier — Stock Analyzer</title>
<style>
  :root {
    --paper: #f7f3e8; --paper-dark: #efe9d8; --ink: #1c1b16;
    --ink-soft: #55524a; --rule: #8b8674; --accent: #7a1f1f;
    --good: #2e6b30; --bad: #9c2b2b; --neutral: #8a7b3a;
  }
  * { box-sizing: border-box; }
  body { background: var(--paper); color: var(--ink); margin: 0;
         font-family: Georgia, "Times New Roman", serif; }
  .sheet { max-width: 1060px; margin: 0 auto; padding: 24px 28px 60px; }
  .mono { font-family: "Courier New", Courier, monospace; }
  h1 { font-size: 44px; letter-spacing: 2px; text-align: center;
       margin: 8px 0 2px; text-transform: uppercase; }
  .dateline { text-align: center; color: var(--ink-soft); font-style: italic;
              border-top: 1px solid var(--rule); border-bottom: 3px double var(--rule);
              padding: 6px 0; margin-bottom: 18px; }
  h2 { font-size: 15px; letter-spacing: 3px; text-transform: uppercase;
       border-bottom: 3px double var(--rule); padding-bottom: 4px;
       margin: 34px 0 12px; }
  h2 .asof { float: right; font-size: 11px; letter-spacing: 1px;
             color: var(--ink-soft); font-family: Courier, monospace; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { text-align: left; font-size: 11px; letter-spacing: 1px;
       text-transform: uppercase; color: var(--ink-soft);
       border-bottom: 1px solid var(--rule); padding: 4px 8px; }
  td { padding: 5px 8px; border-bottom: 1px dotted var(--rule); }
  td.num, th.num { text-align: right; font-family: Courier, monospace; }
  .status { font-family: Courier, monospace; font-size: 13px;
            color: var(--ink-soft); margin: 10px 0; min-height: 18px; }
  .status.error { color: var(--bad); }
  .controls { display: flex; gap: 10px; justify-content: center;
              margin: 14px 0 6px; }
  input[type=text] { font-family: Courier, monospace; font-size: 18px;
      text-transform: uppercase; width: 180px; padding: 8px 10px;
      border: 1px solid var(--rule); background: #fffdf5; }
  button { font-family: Georgia, serif; font-size: 14px; letter-spacing: 1px;
      text-transform: uppercase; padding: 8px 18px; cursor: pointer;
      background: var(--ink); color: var(--paper); border: none; }
  button.ghost { background: none; color: var(--ink);
      border: 1px solid var(--rule); }
  button:disabled { opacity: .45; cursor: default; }
  .stamp { display: inline-block; border: 3px solid var(--accent);
      color: var(--accent); padding: 4px 14px; font-size: 20px;
      letter-spacing: 3px; text-transform: uppercase; font-weight: bold;
      transform: rotate(-6deg); }
  .badge { font-family: Courier, monospace; font-size: 12px;
      border: 1px solid var(--rule); padding: 3px 8px;
      background: var(--paper-dark); }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
  .snapshot { display: flex; flex-wrap: wrap; gap: 8px 26px;
      align-items: baseline; background: var(--paper-dark);
      border: 1px solid var(--rule); padding: 12px 16px; }
  .snapshot .co { font-size: 24px; font-weight: bold; }
  .snapshot .kv { font-family: Courier, monospace; font-size: 13px; }
  .kv b { font-size: 16px; }
  .up { color: var(--good); } .down { color: var(--bad); }
  .verdict-card { display: flex; gap: 22px; align-items: center;
      border: 1px solid var(--rule); padding: 16px 20px;
      background: #fffdf5; }
  .grade { font-size: 64px; font-weight: bold; border: 4px solid var(--ink);
      padding: 4px 22px; }
  .grade.A, .grade.B { color: var(--good); border-color: var(--good); }
  .grade.C { color: var(--neutral); border-color: var(--neutral); }
  .grade.D, .grade.F { color: var(--bad); border-color: var(--bad); }
  .check-pass::before { content: "✓ "; color: var(--good); }
  .check-fail::before { content: "✗ "; color: var(--bad); }
  .check-neutral::before { content: "– "; color: var(--neutral); }
  canvas { width: 100%; border: 1px solid var(--rule); background: #fffdf5; }
  .sent { font-family: Courier, monospace; font-size: 11px; padding: 2px 6px;
      border: 1px solid var(--rule); white-space: nowrap; }
  .sent.pos { color: var(--good); } .sent.neg { color: var(--bad); }
  .topics { color: var(--ink-soft); font-size: 12px; font-style: italic; }
  .macro-ribbon { display: flex; gap: 30px; flex-wrap: wrap;
      font-family: Courier, monospace; font-size: 14px;
      border: 3px double var(--rule); padding: 10px 16px; }
  .foot { color: var(--ink-soft); font-size: 12px; border-top: 3px double
      var(--rule); margin-top: 40px; padding-top: 10px; }
  .unavailable { color: var(--ink-soft); font-style: italic;
      font-family: Courier, monospace; font-size: 13px; }
  .archive-row { cursor: pointer; }
  .archive-row:hover { background: var(--paper-dark); }
  a.datelink { color: var(--accent); }
  .hidden { display: none; }
</style>
</head>
<body>
<div class="sheet">
  <header>
    <h1>The Equity Dossier</h1>
    <div class="dateline">Value · Analysts · News &amp; Trends · Technicals ·
      Macro — <span class="mono" id="todayline"></span></div>
    <div class="controls">
      <input type="text" id="ticker-input" placeholder="AAPL"
             autocomplete="off" spellcheck="false">
      <button id="analyze-btn">Analyze</button>
    </div>
    <div class="status" id="status"></div>
  </header>

  <section id="archive-section">
    <h2>The Archive
      <button class="ghost" id="archive-toggle"
              style="float:right;font-size:11px;padding:2px 10px">hide</button>
    </h2>
    <div id="archive-body">
      <table>
        <thead><tr><th>Ticker</th><th>Company</th><th>Grade</th>
          <th>Updated</th></tr></thead>
        <tbody id="archive-rows"></tbody>
      </table>
      <div class="unavailable hidden" id="archive-empty">
        No tickers analyzed yet — be the first: enter one above.</div>
    </div>
  </section>

  <main id="report" class="hidden">
    <section id="snapshot-section">
      <div class="snapshot" id="snapshot-bar"></div>
      <div style="display:flex;justify-content:space-between;
                  align-items:center;margin-top:8px">
        <span class="badge" id="asof-badge"></span>
        <span class="stamp hidden" id="historical-stamp"></span>
        <button class="ghost" id="refresh-btn"
          title="Re-fetches today's data. Costs API quota: up to ~10 FMP and 5 Alpha Vantage calls.">
          Refresh ↻</button>
      </div>
    </section>

    <section><h2>The Verdict</h2><div id="verdict-body"></div></section>

    <section><h2>Past Editions</h2>
      <canvas id="history-chart" height="160"></canvas>
      <div id="history-body"></div>
    </section>

    <section><h2>Value Analysis</h2><div id="value-body"></div>
      <canvas id="quarters-chart" height="180" style="margin-top:14px">
      </canvas></section>

    <section><h2>Analyst Desk</h2><div id="analyst-body"></div></section>

    <section><h2>Technicals</h2>
      <canvas id="price-chart" height="220"></canvas>
      <div id="technicals-body"></div></section>

    <section><h2>News &amp; Trends</h2><div id="news-body"></div></section>

    <section><h2>Macro Context
        <span class="asof" id="macro-asof"></span></h2>
      <div class="macro-ribbon" id="macro-body"></div></section>

    <div class="foot" id="footnotes"></div>
  </main>
</div>
<script>
/* JS in the next step */
</script>
</body>
</html>
```

Replace the `<script>` placeholder with this JS (same file):

```html
<script>
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtNum = (v, d = 2) => v == null ? "—" :
  Number(v).toLocaleString("en-US", {maximumFractionDigits: d});
const fmtPct = (v, d = 1) => v == null ? "—" : fmtNum(v, d) + "%";
const fmtFrac = v => v == null ? "—" : fmtNum(v * 100, 1) + "%";
const fmtMoney = v => {
  if (v == null) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e12) return fmtNum(v / 1e12) + "T";
  if (abs >= 1e9) return fmtNum(v / 1e9) + "B";
  if (abs >= 1e6) return fmtNum(v / 1e6) + "M";
  return fmtNum(v);
};
const timeAgo = iso => {
  if (!iso) return "—";
  const hours = (Date.now() - new Date(iso).getTime()) / 36e5;
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} min ago`;
  if (hours < 48) return `${Math.round(hours)} hours ago`;
  return `${Math.round(hours / 24)} days ago`;
};
const setStatus = (text, isError) => {
  const el = $("status");
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
};

let currentTicker = null;

async function api(path) {
  if (location.protocol === "file:") {
    throw new Error("This page must be served over HTTP. Run: " +
      "python3 analyze_server.py, then open http://localhost:8000/stock_analyzer.html");
  }
  let response;
  try { response = await fetch(path); }
  catch { throw new Error("Cannot reach the API — is analyze_server.py running?"); }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

/* ---------- canvas helpers ---------- */
function prepCanvas(canvas) {
  const width = canvas.clientWidth, height = canvas.getAttribute("height");
  canvas.width = width * devicePixelRatio;
  canvas.height = height * devicePixelRatio;
  const ctx = canvas.getContext("2d");
  ctx.scale(devicePixelRatio, devicePixelRatio);
  ctx.clearRect(0, 0, width, height);
  return [ctx, width, Number(height)];
}

// seriesList: [{points: [{x: 0..n, y: value}], color, label, dash?}]
function drawLines(canvas, seriesList, formatY = v => fmtNum(v)) {
  const [ctx, W, H] = prepCanvas(canvas);
  const pad = {l: 54, r: 10, t: 10, b: 20};
  const all = seriesList.flatMap(s => s.points.map(p => p.y))
    .filter(v => v != null);
  if (!all.length) return;
  const maxX = Math.max(...seriesList.map(s => s.points.length - 1), 1);
  let lo = Math.min(...all), hi = Math.max(...all);
  if (lo === hi) { lo -= 1; hi += 1; }
  const X = i => pad.l + (W - pad.l - pad.r) * i / maxX;
  const Y = v => pad.t + (H - pad.t - pad.b) * (1 - (v - lo) / (hi - lo));
  ctx.font = "10px Courier"; ctx.fillStyle = "#55524a";
  ctx.strokeStyle = "#d8d2bd";
  for (let g = 0; g <= 3; g++) {
    const v = lo + (hi - lo) * g / 3, y = Y(v);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y);
    ctx.stroke();
    ctx.fillText(formatY(v), 4, y + 3);
  }
  let lx = pad.l;
  for (const s of seriesList) {
    ctx.strokeStyle = s.color; ctx.setLineDash(s.dash || []);
    ctx.beginPath();
    let started = false;
    s.points.forEach((p, i) => {
      if (p.y == null) return;
      const x = X(p.x ?? i), y = Y(p.y);
      started ? ctx.lineTo(x, y) : ctx.moveTo(x, y); started = true;
    });
    ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = s.color;
    ctx.fillText(s.label, lx, H - 6); lx += ctx.measureText(s.label).width + 16;
  }
}

function drawQuarterBars(canvas, quarters) {
  const [ctx, W, H] = prepCanvas(canvas);
  const n = quarters.length;
  if (!n) return;
  const groupWidth = (W - 60) / n;
  const maxRev = Math.max(...quarters.map(q => q.revenue || 0), 1);
  const maxEps = Math.max(...quarters.map(q => Math.abs(q.eps || 0)), 0.01);
  ctx.font = "10px Courier";
  quarters.slice().reverse().forEach((q, i) => {
    const x0 = 30 + i * groupWidth;
    const revH = (H - 50) * (q.revenue || 0) / maxRev;
    const epsH = (H - 50) * Math.abs(q.eps || 0) / maxEps;
    ctx.fillStyle = "#1c1b16";
    ctx.fillRect(x0, H - 30 - revH, groupWidth * .32, revH);
    ctx.fillStyle = (q.eps || 0) < 0 ? "#9c2b2b" : "#7a1f1f";
    ctx.fillRect(x0 + groupWidth * .38, H - 30 - epsH, groupWidth * .32, epsH);
    ctx.fillStyle = "#55524a";
    ctx.fillText(q.date || "", x0, H - 16);
    ctx.fillText("rev " + fmtMoney(q.revenue), x0, 12);
    ctx.fillText("eps " + fmtNum(q.eps), x0, 24);
  });
  ctx.fillStyle = "#55524a";
  ctx.fillText("■ revenue (black)  ■ diluted EPS (red) — scales independent",
               30, H - 4);
}

/* ---------- archive ---------- */
async function loadArchive() {
  try {
    const {tickers} = await api("/api/analyzed");
    $("archive-empty").classList.toggle("hidden", tickers.length > 0);
    $("archive-rows").innerHTML = tickers.map(t => `
      <tr class="archive-row" data-ticker="${esc(t.ticker)}">
        <td class="mono"><b>${esc(t.ticker)}</b></td><td>${esc(t.name)}</td>
        <td class="mono">${esc(t.latestGrade ?? "—")}</td>
        <td class="mono" title="${esc(t.updatedAt)}">${timeAgo(t.updatedAt)}</td>
      </tr>`).join("");
    document.querySelectorAll(".archive-row").forEach(row =>
      row.addEventListener("click", () => analyze(row.dataset.ticker, {})));
  } catch (error) { setStatus(error.message, true); }
}

/* ---------- main flow ---------- */
async function analyze(ticker, {date, refresh} = {}) {
  ticker = ticker.trim().toUpperCase().replace(".", "-");
  if (!ticker) return;
  setStatus(date ? `Opening ${ticker} edition of ${date}…`
                 : `Analyzing ${ticker}… (first fetch of the day takes ~15s)`);
  $("analyze-btn").disabled = true;
  try {
    const params = new URLSearchParams({ticker});
    if (date) params.set("date", date);
    if (refresh) params.set("refresh", "1");
    const bundle = await api("/api/analyze?" + params);
    currentTicker = ticker;
    history.replaceState(null, "",
      "?" + new URLSearchParams(date ? {ticker, date} : {ticker}));
    render(bundle, !!date);
    setStatus("");
  } catch (error) { setStatus(error.message, true); }
  finally { $("analyze-btn").disabled = false; }
}

function unavailableNote(target, sections, key) {
  target.innerHTML = `<div class="unavailable">Section unavailable ` +
    `(${esc(sections?.[key] ?? "source error / quota exhausted")}).</div>`;
}

function render(bundle, isHistorical) {
  $("report").classList.remove("hidden");
  const sections = bundle.meta?.sections || {};
  renderSnapshot(bundle, isHistorical);
  renderVerdict(bundle.verdict);
  renderHistory(bundle.ticker, isHistorical ? bundle.date : null);
  renderValue(bundle, sections);
  renderAnalyst(bundle.analyst, sections);
  renderTechnicals(bundle.technicals, sections);
  renderNews(bundle.news, sections);
  renderMacro(bundle.macro);
  renderFootnotes(bundle);
  $("report").scrollIntoView({behavior: "smooth"});
}

function renderSnapshot(bundle, isHistorical) {
  const s = bundle.snapshot || {};
  const change = s.dayChangePct;
  $("snapshot-bar").innerHTML = `
    <span class="co">${esc(s.name ?? bundle.ticker)}</span>
    <span class="kv">${esc(s.sector ?? "")}${s.industry ? " · " +
      esc(s.industry) : ""}</span>
    <span class="kv">price <b>$${fmtNum(s.price)}</b>
      <span class="${change >= 0 ? "up" : "down"}">${change == null ? "" :
      (change >= 0 ? "▲" : "▼") + fmtNum(Math.abs(change)) + "%"}</span></span>
    <span class="kv">mkt cap <b>${fmtMoney(s.marketCap)}</b></span>
    <span class="kv">P/E <b>${fmtNum(s.peRatio)}</b></span>
    <span class="kv">52w <b>${fmtNum(s.week52Low)}–${fmtNum(s.week52High)}</b></span>`;
  $("asof-badge").textContent =
    `Data as of ${new Date(bundle.meta?.fetchedAt).toLocaleString()}`;
  $("asof-badge").classList.toggle("hidden", isHistorical);
  $("refresh-btn").classList.toggle("hidden", isHistorical);
  const stamp = $("historical-stamp");
  stamp.classList.toggle("hidden", !isHistorical);
  if (isHistorical) stamp.textContent = `Historical edition — ${bundle.date}`;
}

function renderVerdict(verdict) {
  if (!verdict) return unavailableNote($("verdict-body"), null, null);
  $("verdict-body").innerHTML = `
    <div class="verdict-card">
      <span class="grade ${esc(verdict.grade)}">${esc(verdict.grade)}</span>
      <div><p style="margin:0 0 6px">${esc(verdict.summary)}</p>
      <span class="mono" style="font-size:12px">${verdict.passes} of
        ${verdict.evaluated} evaluated checks pass</span></div>
    </div>`;
}

async function renderHistory(ticker, activeDate) {
  const body = $("history-body");
  try {
    const {summaries} = await api("/api/history?ticker=" + ticker);
    const dates = Object.keys(summaries).sort();
    if (dates.length < 1) { body.innerHTML = ""; return; }
    drawLines($("history-chart"), [
      {label: "pass ratio", color: "#1c1b16",
       points: dates.map((d, i) => ({x: i, y: summaries[d].passRatio}))},
      {label: "news sentiment", color: "#7a1f1f", dash: [4, 3],
       points: dates.map((d, i) => ({x: i, y: summaries[d].sentimentScore}))},
    ], v => fmtNum(v, 2));
    body.innerHTML = `<table><thead><tr><th>Date</th><th>Grade</th>
      <th class="num">Price</th><th class="num">Target</th>
      <th class="num">Sentiment</th></tr></thead><tbody>` +
      dates.slice().reverse().map(d => {
        const s = summaries[d];
        const link = d === activeDate ? `<b>${d}</b>` :
          `<a class="datelink" href="#" data-date="${d}">${d}</a>`;
        return `<tr><td class="mono">${link}</td>
          <td class="mono">${esc(s.grade ?? "—")}</td>
          <td class="num">$${fmtNum(s.price)}</td>
          <td class="num">$${fmtNum(s.targetMean)}</td>
          <td class="num">${fmtNum(s.sentimentScore, 2)}</td></tr>`;
      }).join("") + "</tbody></table>";
    body.querySelectorAll("a.datelink").forEach(a =>
      a.addEventListener("click", e => {
        e.preventDefault(); analyze(ticker, {date: a.dataset.date});
      }));
  } catch { body.innerHTML =
    `<div class="unavailable">No history available.</div>`; }
}

function renderValue(bundle, sections) {
  const body = $("value-body");
  const checks = bundle.verdict?.checks;
  const ratios = bundle.fundamentals?.ratios || {};
  if (!checks) { unavailableNote(body, sections, "fundamentals");
                 prepCanvas($("quarters-chart")); return; }
  const row = c => `<tr><td class="check-${c.result}">${esc(c.label)}</td>
    <td class="num">${fmtNum(c.value, 3)}</td>
    <td class="mono" style="font-size:11px">${esc(c.threshold)}</td></tr>`;
  const half = Math.ceil(checks.length / 2);
  const tableOf = list => `<table><thead><tr><th>Check</th>
    <th class="num">Value</th><th>Threshold</th></tr></thead><tbody>` +
    list.map(row).join("") + "</tbody></table>";
  body.innerHTML = `<div class="grid2">${tableOf(checks.slice(0, half))}
    ${tableOf(checks.slice(half))}</div>
    <p class="mono" style="font-size:12px;color:var(--ink-soft)">
    P/B ${fmtNum(ratios.priceToBook)} · EV/EBITDA ${fmtNum(ratios.evToEBITDA)}
    · net margin ${fmtFrac(ratios.netMargin)} · source:
    ${esc(sections.fundamentals ?? "—")}</p>`;
  drawQuarterBars($("quarters-chart"), bundle.fundamentals?.quarters || []);
}

function renderAnalyst(analyst, sections) {
  const body = $("analyst-body");
  if (!analyst) return unavailableNote(body, sections, "analyst");
  const r = analyst.ratings || {};
  const total = ["strongBuy","buy","hold","sell","strongSell"]
    .reduce((sum, k) => sum + (r[k] || 0), 0) || 1;
  const seg = (k, color, label) => (r[k] || 0) === 0 ? "" :
    `<span style="display:inline-block;background:${color};color:#f7f3e8;
     padding:3px 0;text-align:center;font-size:11px;
     width:${(r[k] / total * 100).toFixed(1)}%">${label} ${r[k]}</span>`;
  const t = analyst.targets || {};
  const gauge = () => {
    if (t.low == null || t.high == null || t.high <= t.low) return "—";
    const pos = v => Math.min(99, Math.max(1,
      (v - t.low) / (t.high - t.low) * 100)).toFixed(1);
    const mark = (v, glyph, cls) => v == null ? "" :
      `<span style="position:absolute;left:${pos(v)}%" class="${cls}"
        title="$${fmtNum(v)}">${glyph}</span>`;
    return `<div class="mono" style="position:relative;height:22px;
      border-bottom:1px solid var(--rule);margin:6px 0">
      ${mark(t.mean, "▼", "")}${mark(t.current, "●", "down")}</div>
      <div class="mono" style="display:flex;justify-content:space-between;
      font-size:12px"><span>low $${fmtNum(t.low)}</span>
      <span>consensus $${fmtNum(t.mean)} · current $${fmtNum(t.current)}</span>
      <span>high $${fmtNum(t.high)}</span></div>`;
  };
  const changes = (analyst.upgradesDowngrades || []).map(u => `
    <tr><td class="mono">${esc(u.date)}</td><td>${esc(u.firm)}</td>
    <td>${esc(u.fromGrade ?? "—")} → <b>${esc(u.toGrade ?? "—")}</b></td>
    <td class="mono">${esc(u.action ?? "")}</td></tr>`).join("");
  body.innerHTML = `
    <div style="font-size:0;border:1px solid var(--rule)">
      ${seg("strongBuy", "#2e6b30", "SB")}${seg("buy", "#5c8a4e", "B")}
      ${seg("hold", "#8a7b3a", "H")}${seg("sell", "#a5552e", "S")}
      ${seg("strongSell", "#9c2b2b", "SS")}</div>
    ${gauge()}
    ${changes ? `<table style="margin-top:10px"><thead><tr><th>Date</th>
      <th>Firm</th><th>Change</th><th>Action</th></tr></thead>
      <tbody>${changes}</tbody></table>` : ""}
    <p class="mono" style="font-size:11px;color:var(--ink-soft)">source:
      ${esc(sections.analyst ?? "—")}</p>`;
}

function renderTechnicals(technicals, sections) {
  const body = $("technicals-body");
  if (!technicals) { unavailableNote(body, sections, "technicals");
                     prepCanvas($("price-chart")); return; }
  const priceDates = (technicals.prices || []).map(p => p.date);
  const align = series => {
    const byDate = Object.fromEntries((series || [])
      .map(p => [p.date, p.value]));
    return priceDates.map((d, i) => ({x: i, y: byDate[d] ?? null}));
  };
  drawLines($("price-chart"), [
    {label: "close", color: "#1c1b16",
     points: (technicals.prices || []).map((p, i) => ({x: i, y: p.close}))},
    {label: "SMA-50", color: "#7a1f1f", points: align(technicals.sma50)},
    {label: "SMA-200", color: "#8a7b3a", dash: [5, 4],
     points: align(technicals.sma200)},
  ], v => "$" + fmtNum(v, 0));
  const rsi = technicals.rsi || {}, macd = technicals.macd || {};
  body.innerHTML = `<p class="mono" style="font-size:14px">
    RSI-14 <b>${fmtNum(rsi.value, 1)}</b> — ${esc(rsi.state ?? "—")} ·
    MACD(12/26/9) <b>${fmtNum(macd.macd, 3)}</b> vs signal
    ${fmtNum(macd.signal, 3)} — ${esc(macd.state ?? "—")} ·
    source: ${esc(sections.technicals ?? "—")}</p>`;
}

function renderNews(news, sections) {
  const body = $("news-body");
  if (!news) return unavailableNote(body, sections, "news");
  const badge = (score, label) => `<span class="sent ${score > 0.15 ? "pos" :
    score < -0.15 ? "neg" : ""}">${esc(label)} ${fmtNum(score, 2)}</span>`;
  const rows = (news.articles || []).map(a => `
    <tr><td><a href="${esc(a.url)}" target="_blank" rel="noopener">
      ${esc(a.title)}</a><div class="topics">${esc((a.topics || [])
      .join(" · "))}</div></td>
    <td class="mono" style="font-size:12px">${esc(a.source)}<br>
      ${esc((a.publishedAt || "").replace("T", " "))}</td>
    <td>${badge(a.sentimentScore, a.sentimentLabel)}</td></tr>`).join("");
  body.innerHTML = `<p>Aggregate sentiment (bearish ↔ bullish):
    ${badge(news.aggregateScore, news.aggregateLabel)}</p>
    <table><thead><tr><th>Story</th><th>Source / Time</th>
    <th>Sentiment</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderMacro(macro) {
  const body = $("macro-body");
  if (!macro) { body.innerHTML =
    `<span class="unavailable">Macro unavailable (quota).</span>`;
    $("macro-asof").textContent = ""; return; }
  body.innerHTML = `
    <span>10Y Treasury <b>${fmtPct(macro.treasury10y)}</b></span>
    <span>Fed funds <b>${fmtPct(macro.fedFunds)}</b></span>
    <span>CPI YoY <b>${fmtPct(macro.cpiYoY)}</b></span>
    <span>Unemployment <b>${fmtPct(macro.unemployment)}</b></span>`;
  $("macro-asof").textContent = macro.fetchedAt ?
    "as of " + new Date(macro.fetchedAt).toLocaleString() : "";
}

function renderFootnotes(bundle) {
  const meta = bundle.meta || {};
  $("footnotes").innerHTML = `Sources: Financial Modeling Prep, Yahoo Finance
    (yfinance), Alpha Vantage. Store: <b>${esc(meta.store ?? "—")}</b>
    (${meta.fromStore ? "served from store" : "freshly fetched"}).
    Bundle fetched ${esc(meta.fetchedAt ?? "—")}; macro fetched
    ${esc(bundle.macro?.fetchedAt ?? "—")}. API calls this fetch:
    ${meta.avCallsUsed ?? "—"} Alpha Vantage / ${meta.fmpCallsUsed ?? "—"}
    FMP. Per-section sources: <span class="mono">${esc(JSON.stringify(
    meta.sections || {}))}</span>. Not investment advice.`;
}

/* ---------- wiring ---------- */
$("analyze-btn").addEventListener("click",
  () => analyze($("ticker-input").value, {}));
$("ticker-input").addEventListener("keydown",
  e => { if (e.key === "Enter") analyze($("ticker-input").value, {}); });
$("refresh-btn").addEventListener("click", () => {
  if (currentTicker && confirm("Refresh re-fetches today's data and spends " +
      "API quota (~10 FMP + up to 5 Alpha Vantage calls). Continue?"))
    analyze(currentTicker, {refresh: true});
});
$("archive-toggle").addEventListener("click", () => {
  const hidden = $("archive-body").classList.toggle("hidden");
  $("archive-toggle").textContent = hidden ? "show" : "hide";
});
$("todayline").textContent = new Date().toDateString();
if (location.protocol === "file:") {
  setStatus("Serve over HTTP: python3 analyze_server.py, then open " +
            "http://localhost:8000/stock_analyzer.html", true);
} else {
  loadArchive();
  const params = new URLSearchParams(location.search);
  if (params.get("ticker"))
    analyze(params.get("ticker"), {date: params.get("date") || undefined});
}
</script>
```

- [ ] **Step 2: Manual verification checklist**

Run `python3 analyze_server.py --local-technicals`, open `http://localhost:8000/stock_analyzer.html`, verify:

1. Archive lists previously analyzed tickers (or the empty note); clicking a row loads that ticker.
2. Analyzing `AAPL` renders all sections; status shows progress; repeat visit is instant with "served from store" in footnotes.
3. "Data as of" badge shows the fetch time; Refresh asks for confirmation (quota hint) and re-fetches.
4. Past Editions: after ≥1 analysis a chart + table appears; clicking a date shows the rotated "HISTORICAL EDITION" stamp, hides Refresh, and the URL carries `?ticker=…&date=…` (reload restores the view).
5. Enter `brk.b` → normalized to BRK-B. Enter `ZZZZZZ` → error status, page not blanked.
6. Kill the server, click Analyze → "is analyze_server.py running?" hint. Open via `file://` → HTTP guard message.
7. Charts render on resize-reload; sections marked unavailable show inline notes (simulate by temporarily removing keys from `.env` and refreshing — then restore `.env`).

- [ ] **Step 3: Commit**

```bash
git add stock_analyzer.html
git commit -m "feat: Equity Dossier page — archive, verdict, history, charts"
```

---

### Task 7: README + final verification

**Files:**
- Modify: `README.md` (append a new section before "## Notes")

**Interfaces:**
- Consumes: everything above (documents it).
- Produces: user-facing setup/run docs.

- [ ] **Step 1: Add the README section**

Insert before the existing `## Notes` heading:

```markdown
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
```

- [ ] **Step 2: Full suite + end-to-end check**

```bash
python3 -m pytest -v
python3 analyze_server.py &
curl -s "http://localhost:8000/api/analyze?ticker=MSFT" -o /dev/null -w "%{http_code}\n"
kill %1
```

Expected: all tests PASS; curl prints 200 (or a clean 404 JSON for an invalid ticker).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: setup and run instructions for the stock analyzer"
```

---

## Spec coverage map (self-review)

| Spec section | Task |
|---|---|
| Value grade methodology (10 checks, bands, neutral) | 1 |
| Data sourcing: FMP primary + gated-endpoint memory + yfinance fallback | 2, 5 |
| News/technicals/macro via AV; trimming ≤15/≤250; `--local-technicals` | 3, 5 |
| Firestore model (daily docs, macro doc, registry), memory fallback | 4 |
| API contract (`/api/analyze` + `date`/`refresh`, `/api/analyzed`, `/api/history`), errors, per-section degradation | 5 |
| `.env` keys server-side only; `.gitignore`; `firebase-admin` dep | 4, 5 |
| Page: 11 sections, Ledger aesthetic, freshness badge, Refresh, HISTORICAL stamp, archive, past editions, independent section rendering, file:// guard | 6 |
| README run/setup instructions | 7 |
