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
