"""All external data access: FMP, yfinance, Alpha Vantage. Parsers are pure."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd
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


_FIELD_CANDIDATES = {
    "revenue": ("revenue", "totalRevenue"),
    "eps": ("eps", "epsdiluted", "epsDiluted"),
}


def _first_field(row, field):
    """Resolve `field` from a raw FMP quarter row via the same candidate-key
    list everywhere it's read, so naming drift is handled consistently
    instead of only in whichever function happened to be written first."""
    return _first(row, *_FIELD_CANDIDATES.get(field, (field,)))


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


EQUITY_QUOTE_TYPE = "EQUITY"


def is_non_equity(quote_type):
    """True only for a positively non-equity Yahoo quoteType ("ETF",
    "MUTUALFUND", "INDEX"). A missing quoteType is not treated as non-equity,
    matching how the FMP parsers tolerate missing keys.

    Both the screener's established-equity gate and the analyzer's decision to
    skip the fundamental grade key off this one predicate, so "what counts as
    a gradeable company" is defined in exactly one place."""
    return quote_type not in (None, EQUITY_QUOTE_TYPE)


def parse_profile(rows):
    row = rows[0] if rows else {}
    return {"name": _first(row, "companyName", "name"),
            "sector": _first(row, "sector"),
            "industry": _first(row, "industry")}


def _quarter_row(row):
    revenue = _first_field(row, "revenue")
    net_income = _first(row, "netIncome")
    return {"date": _first(row, "date"),
            "revenue": revenue,
            "netIncome": net_income,
            "eps": _first_field(row, "eps"),
            "netMargin": (net_income / revenue) if revenue and net_income is not None else None}


def _growth_ratio(values):
    """TTM growth from up to 8 values, newest-first: (sum of first 4) /
    (sum of next 4) - 1. None if fewer than 8, any is missing, or the
    denominator is zero."""
    if len(values) < 8 or any(v is None for v in values):
        return None
    recent, prior = sum(values[:4]), sum(values[4:8])
    return (recent / prior - 1) if prior else None


def _ttm_growth(quarters, field):
    return _growth_ratio([_first_field(q, field) for q in quarters[:8]])


def build_fundamentals(income_q, balance_q, cashflow_q, income_a,
                       ratios_ttm, key_metrics_ttm, market_cap=None):
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
    # `or` would treat a legitimate 0 (e.g. a debt-free company) as missing
    # and silently overwrite it with the balance-sheet fallback.
    current_ratio_raw = _first(ratios, "currentRatioTTM")
    current_ratio = current_ratio_raw if current_ratio_raw is not None \
        else _current_ratio(balance)
    debt_equity_raw = _first(ratios, "debtEquityRatioTTM", "debtToEquityTTM")
    debt_equity = debt_equity_raw if debt_equity_raw is not None \
        else _debt_equity(balance)

    year_ago = income_q[4] if len(income_q) > 4 else None
    equity = _first(balance, "totalStockholdersEquity", "totalEquity")

    # The three fallbacks below only engage when FMP's ratios-ttm/
    # key-metrics-ttm endpoints are gated (a documented free-tier risk) —
    # the same reports already fetched for `quarters`/`balance` carry
    # enough data to derive these locally instead of silently grading
    # them "neutral" while the identical yfinance-fallback path would
    # have produced a real value.
    net_margin = _first(ratios, "netProfitMarginTTM", "netIncomePerRevenueTTM")
    if net_margin is None and quarters:
        net_margin = quarters[0]["netMargin"]

    roe = _first(key_metrics, "roeTTM", "returnOnEquityTTM")
    if roe is None and equity is not None and equity > 0 and len(income_q) >= 4:
        ttm_net_income = [_first(q, "netIncome") for q in income_q[:4]]
        if all(v is not None for v in ttm_net_income):
            roe = sum(ttm_net_income) / equity

    fcf_yield = _first(key_metrics, "freeCashFlowYieldTTM")
    if fcf_yield is None and fcf_ttm is not None and market_cap:
        fcf_yield = fcf_ttm / market_cap

    metrics = {
        "revenueGrowth": _ttm_growth(income_q, "revenue"),
        "epsGrowth": _ttm_growth(income_q, "eps"),
        "netMargin": net_margin,
        "netMarginYearAgo": _quarter_row(year_ago)["netMargin"] if year_ago else None,
        "roe": roe,
        "fcfTTM": fcf_ttm,
        "debtToEquity": debt_equity,
        "currentRatio": current_ratio,
        "peTTM": pe,
        "peg": _first(ratios, "pegRatioTTM", "priceEarningsToGrowthRatioTTM"),
        "fcfYield": fcf_yield,
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
    # `liabilities` must stay a truthiness check (guards divide-by-zero);
    # `assets` must not, since a legitimate 0 shouldn't count as missing.
    if assets is None or not liabilities:
        return None
    return assets / liabilities


def _debt_equity(balance):
    debt = _first(balance, "totalDebt")
    equity = _first(balance, "totalStockholdersEquity", "totalEquity")
    # Negative equity must not produce a ratio: debt/equity with equity < 0
    # flips sign and can land under the "< 1.5" pass threshold, making a
    # company in genuine financial distress (negative equity) look healthy.
    if debt is None or equity is None or equity <= 0:
        return None
    return debt / equity


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
            "quoteType": info.get("quoteType"),
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

    fcf_values = [_df_value(cashflow, "Free Cash Flow", i) for i in range(4)]
    fcf_ttm = sum(fcf_values) if all(v is not None for v in fcf_values) \
        and len(fcf_values) == 4 else None
    market_cap = info.get("marketCap")
    equity = _df_value(balance, "Stockholders Equity")
    debt = _df_value(balance, "Total Debt")
    assets = _df_value(balance, "Current Assets")
    liabilities = _df_value(balance, "Current Liabilities")
    net_income_values = [_df_value(income, "Net Income", i) for i in range(4)]
    net_income_ttm = (sum(net_income_values)
                      if all(v is not None for v in net_income_values) else None)

    metrics = {
        "revenueGrowth": _growth_ratio(revenues),
        "epsGrowth": _growth_ratio(eps_values),
        "netMargin": margins[0] if margins else None,
        "netMarginYearAgo": margins[4] if len(margins) > 4 else None,
        # Negative equity must not produce a ratio here either — see
        # _debt_equity's comment for why the sign matters, not just the
        # divide-by-zero guard.
        "roe": (net_income_ttm / equity)
            if net_income_ttm is not None and equity is not None and equity > 0
            else None,
        "fcfTTM": fcf_ttm,
        "debtToEquity": (debt / equity)
            if debt is not None and equity is not None and equity > 0 else None,
        "currentRatio": (assets / liabilities)
            if assets is not None and liabilities else None,
        "peTTM": info.get("trailingPE"),
        "peg": info.get("pegRatio") or info.get("trailingPegRatio"),
        "fcfYield": (fcf_ttm / market_cap)
            if fcf_ttm is not None and market_cap else None,
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


# ---------- Yahoo screener ----------

SCREEN_PAGE_MAX = 250          # Yahoo rejects size > 250 outright

# US exchange codes offered by the screener page. Deliberately excludes the
# OTC/pink-sheet venues Yahoo also accepts (PNK, OEM, ...): a screen for
# *established* companies should not surface unlisted shells, and Yahoo's
# fundamentals for them are too sparse to filter on.
SCREEN_EXCHANGES = {"NMS": "NasdaqGS", "NGM": "NasdaqGM", "NCM": "NasdaqCM",
                    "NYQ": "NYSE", "ASE": "NYSE American"}

# Public filter name -> (Yahoo screener field, comparator). Yahoo exposes no
# listing-age or IPO-date field among its filterable fields, so the
# "established" gate cannot be pushed server-side; filter_established
# applies it locally against firstTradeDateMilliseconds, which every
# response row carries.
#
# Units follow Yahoo's own, which are not uniform: dividendyield and
# returnonequity are percentages (15 means 15%), marketcap and volume are
# absolute counts.
SCREEN_NUMERIC_FILTERS = {
    "minMarketCap": ("intradaymarketcap", "gt"),
    "minAvgVolume": ("avgdailyvol3m", "gt"),
    "minDividendYield": ("dividendyield", "gt"),
    "minRoe": ("returnonequity.lasttwelvemonths", "gt"),
    "maxPeRatio": ("peratio.lasttwelvemonths", "lt"),
    "maxBeta": ("beta", "lt"),
}

SCREEN_SORT_FIELDS = ("intradaymarketcap", "avgdailyvol3m", "percentchange",
                      "peratio.lasttwelvemonths", "dividendyield",
                      "fiftytwowkpercentchange")

_MS_PER_YEAR = 365.25 * 24 * 60 * 60 * 1000

# Yahoo's screener endpoint times out often enough in normal use that a single
# failed request must not become the user's answer. yfinance hardcodes a 30s
# request timeout inside YfData.post and exposes no way to shorten it, so the
# retries are few and the whole paginated screen is bounded by a deadline —
# a browser waiting on this cannot be left hanging for minutes.
SCREEN_ATTEMPTS = 3
SCREEN_RETRY_WAIT = 1.5        # seconds, scaled by the attempt number
SCREEN_DEADLINE = 75           # seconds for the entire paginated screen


class ScreenUnavailable(Exception):
    """Yahoo's screener could not be reached. Deliberately distinct from the
    ValueError a bad query raises: the caller's criteria were fine, the
    upstream was not, and the two deserve different HTTP statuses."""


def _screen_page(screen_fn, query, offset, size, sort_field, sort_asc,
                 attempts, sleep, clock, deadline):
    """Fetch one page, retrying transient upstream failures.

    ValueError and TypeError are re-raised without retrying: those mean the
    query or this very call is malformed, and retrying a bug three times only
    delays the report by a minute and a half."""
    last_error = None
    attempt = 0
    while attempt < attempts:
        attempt += 1
        try:
            return screen_fn(query, offset=offset, size=size,
                             sortField=sort_field, sortAsc=sort_asc) or {}
        except (ValueError, TypeError):
            raise
        except Exception as error:
            last_error = error
            if attempt >= attempts or clock() >= deadline:
                break
            sleep(SCREEN_RETRY_WAIT * attempt)
    raise ScreenUnavailable(
        f"Yahoo's screener did not respond after {attempt} attempt(s) "
        f"({type(last_error).__name__}). This is usually transient — try "
        f"again, or lower the row limit.") from last_error


def build_screen_query(criteria, query_cls=yf.EquityQuery):
    """Pure: a criteria dict -> an EquityQuery. The query class validates
    field names and enum values offline, so a typo'd sector or exchange
    raises here instead of silently returning an empty result set."""
    unknown = [code for code in criteria.get("exchanges") or ()
               if code not in SCREEN_EXCHANGES]
    if unknown:
        raise ValueError(f"Unsupported exchange(s): {', '.join(unknown)}")

    terms = [query_cls("eq", ["region", "us"])]
    if criteria.get("exchanges"):
        terms.append(query_cls("is-in", ["exchange", *criteria["exchanges"]]))
    if criteria.get("sector"):
        terms.append(query_cls("eq", ["sector", criteria["sector"]]))
    for name, (field, comparator) in SCREEN_NUMERIC_FILTERS.items():
        if criteria.get(name) is not None:
            terms.append(query_cls(comparator, [field, criteria[name]]))
    # "and" over a single operand is rejected by the query class, which a
    # caller who cleared every optional filter would otherwise hit.
    return terms[0] if len(terms) == 1 else query_cls("and", terms)


def yf_screen_all(query, limit, sort_field="intradaymarketcap",
                  sort_asc=False, screen_fn=yf.screen,
                  attempts=SCREEN_ATTEMPTS, sleep=time.sleep,
                  clock=time.monotonic):
    """Page through Yahoo's screener until `limit` rows or the matches run
    out. Returns (raw_rows, total_matches) — total is Yahoo's own count of
    everything matching server-side, which is usually larger than what was
    fetched. Raises ScreenUnavailable if a page cannot be fetched at all."""
    deadline = clock() + SCREEN_DEADLINE
    rows, total, offset = [], 0, 0
    while len(rows) < limit:
        wanted = min(SCREEN_PAGE_MAX, limit - len(rows))
        page = _screen_page(screen_fn, query, offset, wanted, sort_field,
                            sort_asc, attempts, sleep, clock, deadline)
        total = page.get("total") or total
        quotes = page.get("quotes") or []
        rows.extend(quotes)
        offset += len(quotes)
        # Stop on an empty or short page as well as on reaching `total`:
        # trusting `total` alone would spin forever whenever Yahoo reports
        # more matches than it will actually hand back.
        if not quotes or len(quotes) < wanted or offset >= total:
            break
        # Out of time mid-pagination: return the rows that did arrive rather
        # than failing outright. The caller reports fetched-vs-total, so a
        # truncated screen already shows up as such instead of as an error.
        if clock() >= deadline:
            break
    return rows[:limit], total


def parse_screen_row(row, now_ms):
    """Pure: one raw Yahoo screener quote -> the fields the screener page
    renders. historyYears comes from firstTradeDateMilliseconds, the only
    listing-age signal in the response and the basis of the established
    gate; it is None when Yahoo omits the date."""
    first_trade = row.get("firstTradeDateMilliseconds")
    return {"ticker": row.get("symbol"),
            "name": row.get("longName") or row.get("shortName")
                    or row.get("displayName") or row.get("symbol"),
            "exchange": row.get("fullExchangeName"),
            "quoteType": row.get("quoteType"),
            "price": row.get("regularMarketPrice"),
            "dayChangePct": row.get("regularMarketChangePercent"),
            "marketCap": row.get("marketCap"),
            "peRatio": row.get("trailingPE"),
            "forwardPeRatio": row.get("forwardPE"),
            "priceToBook": row.get("priceToBook"),
            "dividendYield": row.get("dividendYield"),
            "avgVolume3m": row.get("averageDailyVolume3Month"),
            "week52ChangePct": row.get("fiftyTwoWeekChangePercent"),
            "analystRating": row.get("averageAnalystRating"),
            "historyYears": ((now_ms - first_trade) / _MS_PER_YEAR)
                            if first_trade else None}


def filter_established(rows, min_history_years, now_ms):
    """Pure: parse rows and keep the common equities with at least
    `min_history_years` of price history. Returns (kept, dropped_counts) so
    the page can say *why* a screen returned fewer names than Yahoo matched
    rather than leaving the gap unexplained.

    A row with no listing date is dropped, not kept: the gate exists to
    require a proven track record, and an unknown listing date is not
    proof of one."""
    kept = []
    dropped = {"nonEquity": 0, "unknownListing": 0, "tooYoung": 0}
    for row in rows:
        parsed = parse_screen_row(row, now_ms)
        # A missing quoteType is tolerated (kept); only a positively
        # non-equity quoteType — an ETF or fund that slipped past the filters
        # — is dropped as such. See is_non_equity.
        if is_non_equity(parsed["quoteType"]):
            dropped["nonEquity"] += 1
        elif parsed["historyYears"] is None:
            dropped["unknownListing"] += 1
        elif parsed["historyYears"] < min_history_years:
            dropped["tooYoung"] += 1
        else:
            kept.append(parsed)
    return kept, dropped


# ---------- Alpha Vantage ----------

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
    macd_value = signal_value = macd_date = None
    if macd_block:
        macd_date = max(macd_block)
        latest = macd_block[macd_date]
        macd_value = float(latest["MACD"])
        signal_value = float(latest["MACD_Signal"])
    sma50_pairs = _av_series(sma50_payload, "Technical Analysis: SMA", "SMA")
    sma200_pairs = _av_series(sma200_payload, "Technical Analysis: SMA", "SMA")
    prices = _prices_from_close(close)

    # Alpha Vantage's free tier commonly lags yfinance's freshest close by
    # a day or more, so the SMA/RSI/MACD overlay can silently stop short
    # of "today" with no error surfaced anywhere. Compute and expose the
    # actual latest indicator date so the frontend can show it instead of
    # implying the indicators are as fresh as the price chart.
    indicator_dates = [d for d, _ in (rsi_pairs[-1:] + sma50_pairs[-1:]
                                      + sma200_pairs[-1:])]
    if macd_date:
        indicator_dates.append(macd_date)
    indicators_as_of = max(indicator_dates) if indicator_dates else None
    prices_as_of = prices[-1]["date"] if prices else None

    return {
        "prices": prices,
        "sma50": _trim_series(sma50_pairs),
        "sma200": _trim_series(sma200_pairs),
        "rsi": {"value": rsi_value, "state": _rsi_state(rsi_value)},
        "macd": {"macd": macd_value, "signal": signal_value,
                 "state": _macd_state(macd_value, signal_value)},
        "indicatorsAsOf": indicators_as_of,
        "stale": bool(indicators_as_of and prices_as_of
                     and indicators_as_of < prices_as_of),
    }


def _last_or_none(series):
    """Last value of `series`, or None if empty or NaN (e.g. a flat/
    illiquid price series produces a 0/0 RSI). A bare NaN would otherwise
    reach json.dumps (default allow_nan=True) and serialize as the
    non-standard `NaN` token, breaking every JSON parser downstream."""
    if not len(series):
        return None
    value = float(series.iloc[-1])
    return None if value != value else value


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

    rsi_raw = _last_or_none(rsi)
    macd_raw = _last_or_none(macd)
    signal_raw = _last_or_none(signal)
    rsi_value = round(rsi_raw, 2) if rsi_raw is not None else None
    macd_value = round(macd_raw, 4) if macd_raw is not None else None
    signal_value = round(signal_raw, 4) if signal_raw is not None else None
    return {
        "prices": _prices_from_close(close),
        "sma50": _trim_series(series_pairs(close.rolling(50).mean())),
        "sma200": _trim_series(series_pairs(close.rolling(200).mean())),
        "rsi": {"value": rsi_value, "state": _rsi_state(rsi_value)},
        "macd": {"macd": macd_value, "signal": signal_value,
                 # State from the raw (unrounded) values — rounding both
                 # to 4dp can make a genuinely-bullish macd > signal
                 # collapse to an apparent tie (see test fixture).
                 "state": _macd_state(macd_raw, signal_raw)},
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
    if not values:
        return None
    latest_date, latest_value = values[0]
    # Match "12 months prior" by calendar date, not list position: any
    # skipped/unparsable row (e.g. a government placeholder like ".")
    # would otherwise shift every later index, silently comparing against
    # the wrong month.
    try:
        target_prefix = f"{int(latest_date[:4]) - 1}-{latest_date[5:7]}"
    except (ValueError, IndexError):
        return None
    for date, value in values[1:]:
        if date.startswith(target_prefix):
            return (latest_value / value - 1) * 100 if value else None
    return None


def parse_macro(treasury_payload, cpi_payload, fedfunds_payload,
                unemployment_payload):
    return {"treasury10y": _latest_econ(treasury_payload),
            "fedFunds": _latest_econ(fedfunds_payload),
            "cpiYoY": _cpi_yoy(cpi_payload),
            "unemployment": _latest_econ(unemployment_payload)}
