"""All external data access: FMP, yfinance, Alpha Vantage. Parsers are pure."""
import json
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
    # Determine MACD state from raw values before rounding to preserve precision
    macd_state = _macd_state(macd.iloc[-1] if len(macd) else None,
                             signal.iloc[-1] if len(signal) else None)
    return {
        "prices": _prices_from_close(close),
        "sma50": _trim_series(series_pairs(close.rolling(50).mean())),
        "sma200": _trim_series(series_pairs(close.rolling(200).mean())),
        "rsi": {"value": rsi_value, "state": _rsi_state(rsi_value)},
        "macd": {"macd": macd_value, "signal": signal_value,
                 "state": macd_state},
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
