# test_analysis_sources.py
import urllib.error
from datetime import datetime, timedelta

import pandas as pd
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


def test_build_fundamentals_keeps_legitimate_zero_ratios():
    """A debt-free company's real 0 must not be overwritten by the
    balance-sheet fallback (the old `_first(...) or fallback` treated 0
    as falsy/missing)."""
    ratios = [{"currentRatioTTM": 0.0, "debtEquityRatioTTM": 0.0}]
    balance = [{"totalCurrentAssets": 999, "totalCurrentLiabilities": 1,
                "totalDebt": 999, "totalStockholdersEquity": 1}]
    _, metrics = src.build_fundamentals(
        INCOME_Q, balance, CASHFLOW_Q, INCOME_A, ratios, [])
    assert metrics["currentRatio"] == 0.0
    assert metrics["debtToEquity"] == 0.0


def test_build_fundamentals_negative_equity_does_not_pass_debt_check():
    """Negative equity must not produce a ratio at all — debt/negative
    equity flips sign and can land under the '< 1.5' pass threshold,
    making a distressed company (negative equity) look healthy."""
    balance = [{"totalDebt": 500, "totalStockholdersEquity": -50}]
    _, metrics = src.build_fundamentals(
        INCOME_Q, balance, CASHFLOW_Q, INCOME_A, [], [])
    assert metrics["debtToEquity"] is None


def test_ttm_growth_resolves_camelcase_eps_and_alternate_revenue_key():
    """_ttm_growth must use the same candidate-key list _quarter_row uses,
    or a response using only the camelCase/alternate key silently loses
    growth data even though every quarter's value is present and displayed."""
    quarters = [{"date": f"q{i}", "totalRevenue": 100 + i, "epsDiluted": 1.0}
                for i in range(8)]
    fundamentals, metrics = src.build_fundamentals(
        quarters, [], [], [], [], [])
    assert fundamentals["quarters"][0]["revenue"] == 100
    assert fundamentals["quarters"][0]["eps"] == 1.0
    assert metrics["revenueGrowth"] is not None
    assert metrics["epsGrowth"] is not None


def test_build_fundamentals_falls_back_locally_when_fmp_ratios_gated():
    """When FMP's ratios-ttm/key-metrics-ttm endpoints are gated (empty),
    netMargin/roe/fcfYield must not silently go neutral — the same
    quarters/balance data already fetched can derive them locally, just
    like the yfinance fallback path already does."""
    balance = [{"totalStockholdersEquity": 200}]
    _, metrics = src.build_fundamentals(
        INCOME_Q, balance, CASHFLOW_Q, INCOME_A, [], [],
        market_cap=1000)
    assert metrics["netMargin"] == pytest.approx(25 / 100)   # most recent quarter
    assert metrics["roe"] == pytest.approx((25 + 24 + 30 + 21) / 200)
    assert metrics["fcfYield"] == pytest.approx(115 / 1000)  # fcfTTM / market_cap


def test_build_fundamentals_local_fallback_skipped_when_fmp_present():
    """The fallback must never override a real (including gated-then-later-
    available) FMP value — this is additive, not a replacement."""
    ratios = [{"netProfitMarginTTM": 0.5}]
    key_metrics = [{"roeTTM": 0.9, "freeCashFlowYieldTTM": 0.05}]
    _, metrics = src.build_fundamentals(
        INCOME_Q, BALANCE_Q, CASHFLOW_Q, INCOME_A, ratios, key_metrics,
        market_cap=1000)
    assert metrics["netMargin"] == 0.5
    assert metrics["roe"] == 0.9
    assert metrics["fcfYield"] == 0.05


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


# ---------- yfinance prices ----------

def test_yf_prices_returns_close_series():
    """yf_prices returns the Close column as a Series with DatetimeIndex."""
    class PriceTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, period="1y", interval="1d", auto_adjust=True):
            dates = pd.date_range(end="2026-08-28", periods=252, freq="D")
            data = {"Close": range(100, 100 + len(dates))}
            return pd.DataFrame(data, index=dates)

    prices = src.yf_prices("AAPL", ticker_factory=PriceTicker)
    assert isinstance(prices, pd.Series)
    assert isinstance(prices.index, pd.DatetimeIndex)
    assert len(prices) == 252
    assert prices.iloc[0] == 100
    assert prices.iloc[-1] == 351


def test_yf_prices_empty_history_raises_lookup_error():
    """yf_prices raises LookupError when history is empty."""
    class EmptyHistoryTicker:
        def __init__(self, symbol):
            pass

        def history(self, period="1y", interval="1d", auto_adjust=True):
            return pd.DataFrame()

    with pytest.raises(LookupError):
        src.yf_prices("UNKNOWN", ticker_factory=EmptyHistoryTicker)


def test_yf_prices_none_history_raises_lookup_error():
    """yf_prices raises LookupError when history is None."""
    class NoneHistoryTicker:
        def __init__(self, symbol):
            pass

        def history(self, period="1y", interval="1d", auto_adjust=True):
            return None

    with pytest.raises(LookupError):
        src.yf_prices("UNKNOWN", ticker_factory=NoneHistoryTicker)


# ---------- yfinance fundamentals ----------

def test_yf_fundamentals_with_8_quarters_and_full_data():
    """yf_fundamentals returns correct fundamentals and metrics from 8 quarters of data."""
    class FundamentalsTicker:
        def __init__(self, symbol):
            self.info = {"marketCap": 3e12, "trailingPE": 25.0, "pegRatio": 1.5,
                         "priceToBook": 35.0}

        @property
        def quarterly_income_stmt(self):
            dates = pd.date_range(end="2026-06-28", periods=8, freq="QS")
            # 8 quarters: recent 4 sum to 410, prior 4 sum to 360
            revenues = [100, 100, 110, 90, 95, 90, 95, 80]  # newest first
            net_incomes = [25, 24, 30, 21, 22, 20, 24, 18]
            eps_values = [1.60, 1.55, 1.90, 1.35, 1.40, 1.30, 1.55, 1.15]
            return pd.DataFrame(
                {"Total Revenue": revenues,
                 "Net Income": net_incomes,
                 "Diluted EPS": eps_values},
                index=dates
            ).T

        @property
        def quarterly_balance_sheet(self):
            dates = pd.date_range(end="2026-06-28", periods=4, freq="QS")
            return pd.DataFrame(
                {"Current Assets": [140, 135, 130, 125],
                 "Current Liabilities": [100, 98, 95, 90],
                 "Total Debt": [110, 108, 105, 100],
                 "Stockholders Equity": [80, 82, 85, 88]},
                index=dates
            ).T

        @property
        def quarterly_cashflow(self):
            dates = pd.date_range(end="2026-06-28", periods=4, freq="QS")
            return pd.DataFrame(
                {"Free Cash Flow": [30, 28, 35, 22]},
                index=dates
            ).T

    fundamentals, metrics = src.yf_fundamentals("AAPL", ticker_factory=FundamentalsTicker)

    # Check fundamentals structure
    assert "quarters" in fundamentals
    assert "annual" in fundamentals
    assert "ratios" in fundamentals
    assert len(fundamentals["quarters"]) == 4
    assert fundamentals["annual"] is None  # yfinance doesn't fetch annual data

    # Check metrics
    # Recent 4: 100+100+110+90 = 400; Prior 4: 95+90+95+80 = 360
    assert metrics["revenueGrowth"] == pytest.approx((400 / 360) - 1)
    # Recent 4: 1.60+1.55+1.90+1.35 = 6.40; Prior 4: 1.40+1.30+1.55+1.15 = 5.40
    assert metrics["epsGrowth"] == pytest.approx((6.40 / 5.40) - 1)
    assert metrics["fcfTTM"] == 115  # 30+28+35+22
    assert metrics["peTTM"] == 25.0
    assert metrics["peg"] == 1.5


def test_yf_fundamentals_fewer_than_8_quarters():
    """yf_fundamentals returns None for growth when fewer than 8 quarters available."""
    class PartialTicker:
        def __init__(self, symbol):
            self.info = {}

        @property
        def quarterly_income_stmt(self):
            # Only 4 quarters
            dates = pd.date_range(end="2026-06-28", periods=4, freq="QS")
            revenues = [100, 100, 110, 90]
            net_incomes = [25, 24, 30, 21]
            eps_values = [1.60, 1.55, 1.90, 1.35]
            return pd.DataFrame({
                "Total Revenue": revenues,
                "Net Income": net_incomes,
                "Diluted EPS": eps_values
            }, index=dates).T

        @property
        def quarterly_balance_sheet(self):
            return pd.DataFrame()

        @property
        def quarterly_cashflow(self):
            return pd.DataFrame()

    fundamentals, metrics = src.yf_fundamentals("AAPL", ticker_factory=PartialTicker)

    # Growth should be None with only 4 quarters
    assert metrics["revenueGrowth"] is None
    assert metrics["epsGrowth"] is None
    assert metrics["fcfTTM"] is None
    assert len(fundamentals["quarters"]) == 4


def test_yf_fundamentals_handles_nan_values():
    """yf_fundamentals handles NaN values gracefully, returning None instead of NaN."""
    class NaNTicker:
        def __init__(self, symbol):
            self.info = {}

        @property
        def quarterly_income_stmt(self):
            # Include NaN values in eps to break epsGrowth calculation
            dates = pd.date_range(end="2026-06-28", periods=8, freq="QS")
            revenues = [100, 100, 110, 90, 95, 90, 95, 80]
            net_incomes = [25, float('nan'), 30, 21, 22, 20, 24, 18]  # NaN in quarter 1
            eps_values = [1.60, 1.55, float('nan'), 1.35, 1.40, 1.30, 1.55, 1.15]  # NaN in eps
            return pd.DataFrame({
                "Total Revenue": revenues,
                "Net Income": net_incomes,
                "Diluted EPS": eps_values
            }, index=dates).T

        @property
        def quarterly_balance_sheet(self):
            return pd.DataFrame()

        @property
        def quarterly_cashflow(self):
            return pd.DataFrame()

    fundamentals, metrics = src.yf_fundamentals("AAPL", ticker_factory=NaNTicker)

    # Should not raise; netMargin for quarter with NaN should be None
    assert fundamentals["quarters"][1]["netMargin"] is None
    # epsGrowth should be None due to NaN in EPS
    assert metrics["epsGrowth"] is None


# ---------- yfinance analyst ----------

def test_yf_analyst_with_populated_data():
    """yf_analyst returns ratings, targets, and upgrades when data is populated."""
    class AnalystTicker:
        def __init__(self, symbol):
            pass

        @property
        def analyst_price_targets(self):
            return {"low": 180.0, "mean": 245.5, "high": 310.0}

        @property
        def recommendations_summary(self):
            data = {
                "strongBuy": [12],
                "buy": [20],
                "hold": [8],
                "sell": [2],
                "strongSell": [1]
            }
            return pd.DataFrame(data)

        @property
        def upgrades_downgrades(self):
            dates = pd.date_range(end="2026-08-20", periods=5, freq="D")
            data = {
                "Firm": ["Morgan Stanley", "Goldman Sachs", "JPMorgan", "BofA", "Citigroup"],
                "FromGrade": ["Equal-Weight", "Neutral", "Neutral", "Neutral", "Neutral"],
                "ToGrade": ["Overweight", "Buy", "Buy", "Overweight", "Buy"],
                "Action": ["upgrade", "upgrade", "upgrade", "upgrade", "upgrade"]
            }
            df = pd.DataFrame(data, index=dates)
            return df

    analyst = src.yf_analyst("AAPL", ticker_factory=AnalystTicker)

    assert analyst["ratings"]["strongBuy"] == 12
    assert analyst["ratings"]["buy"] == 20
    assert analyst["ratings"]["hold"] == 8
    assert analyst["targets"]["low"] == 180.0
    assert analyst["targets"]["mean"] == 245.5
    assert analyst["targets"]["high"] == 310.0
    assert len(analyst["upgradesDowngrades"]) == 5
    assert analyst["upgradesDowngrades"][0]["firm"] == "Morgan Stanley"
    assert analyst["upgradesDowngrades"][0]["fromGrade"] == "Equal-Weight"


def test_yf_analyst_with_empty_or_none_data():
    """yf_analyst returns proper shape with empty/None data, no exceptions."""
    class EmptyAnalystTicker:
        def __init__(self, symbol):
            pass

        @property
        def analyst_price_targets(self):
            return None

        @property
        def recommendations_summary(self):
            return None

        @property
        def upgrades_downgrades(self):
            return None

    analyst = src.yf_analyst("UNKNOWN", ticker_factory=EmptyAnalystTicker)

    # Should return proper shape with zeros and empty lists
    assert analyst["ratings"]["strongBuy"] == 0
    assert analyst["ratings"]["buy"] == 0
    assert analyst["ratings"]["hold"] == 0
    assert analyst["ratings"]["sell"] == 0
    assert analyst["ratings"]["strongSell"] == 0
    assert analyst["targets"]["low"] is None
    assert analyst["targets"]["mean"] is None
    assert analyst["targets"]["high"] is None
    assert analyst["upgradesDowngrades"] == []


def test_yf_analyst_upgrades_downgrades_trimmed_to_10():
    """yf_analyst trims upgrades_downgrades to at most 10 entries."""
    class ManyUpgradesTicker:
        def __init__(self, symbol):
            pass

        @property
        def analyst_price_targets(self):
            return {}

        @property
        def recommendations_summary(self):
            return None

        @property
        def upgrades_downgrades(self):
            # 15 upgrades
            dates = pd.date_range(end="2026-08-20", periods=15, freq="D")
            data = {
                "Firm": [f"Firm{i}" for i in range(15)],
                "FromGrade": ["Neutral"] * 15,
                "ToGrade": ["Buy"] * 15,
                "Action": ["upgrade"] * 15
            }
            return pd.DataFrame(data, index=dates)

    analyst = src.yf_analyst("AAPL", ticker_factory=ManyUpgradesTicker)

    # Should be trimmed to 10
    assert len(analyst["upgradesDowngrades"]) == 10


# ---------- Alpha Vantage ----------

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
    assert technicals["indicatorsAsOf"] == "2026-08-28"
    assert technicals["stale"] is False


def test_parse_technicals_flags_staleness_when_av_lags_price_series():
    """The fixtures above were hand-picked to exactly match the price
    series' latest date, which can never catch Alpha Vantage's free-tier
    data lagging yfinance's freshest close. This test uses deliberately
    mismatched dates to prove the lag is surfaced, not silently dropped."""
    stale_rsi = {"Technical Analysis: RSI": {"2026-08-26": {"RSI": "55.0"}}}
    stale_macd = {"Technical Analysis: MACD": {
        "2026-08-26": {"MACD": "1.0", "MACD_Signal": "0.5"}}}
    stale_sma50 = {"Technical Analysis: SMA": {"2026-08-26": {"SMA": "220.0"}}}
    stale_sma200 = {"Technical Analysis: SMA": {"2026-08-26": {"SMA": "200.0"}}}

    technicals = src.parse_technicals(
        stale_rsi, stale_macd, stale_sma50, stale_sma200, close_series())

    assert technicals["prices"][-1]["date"] == "2026-08-28"
    assert technicals["indicatorsAsOf"] == "2026-08-26"
    assert technicals["stale"] is True


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


def test_compute_local_technicals_flat_price_is_none_not_nan():
    """A perfectly flat price series (e.g. a halted/illiquid stock) makes
    RSI's gain and loss both 0, so gain/loss is 0/0 = NaN. That must
    surface as None, not a literal NaN float that json.dumps would emit
    as an invalid, non-standard JSON token."""
    import math

    import pandas as pd

    dates = pd.date_range(end="2026-08-28", periods=60, freq="B")
    flat = pd.Series([100.0] * 60, index=dates)
    technicals = src.compute_local_technicals(flat)
    assert technicals["rsi"]["value"] is None
    assert technicals["rsi"]["state"] is None
    # Never a raw float NaN anywhere in the payload (json.dumps would
    # otherwise emit the bare, invalid `NaN` token).
    assert not any(isinstance(v, float) and math.isnan(v)
                  for v in (technicals["rsi"]["value"],
                           technicals["macd"]["macd"],
                           technicals["macd"]["signal"]))


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


def test_cpi_yoy_tolerates_a_missing_month_by_matching_calendar_date():
    """If a calendar month is entirely absent from the feed (a real gap,
    not just a malformed value), every later index shifts by one — the
    fixed list-position lookup (`values[12]`) would then land on the
    WRONG month. Matching by calendar date must still find the true
    12-months-back value regardless of where it sits in the list."""
    values = [
        ("2026-07-01", 320.0), ("2026-06-01", 321.0), ("2026-05-01", 322.0),
        # 2026-04 missing entirely — a real gap, not a parse failure
        ("2026-03-01", 324.0), ("2026-02-01", 325.0), ("2026-01-01", 326.0),
        ("2025-12-01", 300.0), ("2025-11-01", 301.0), ("2025-10-01", 302.0),
        ("2025-09-01", 303.0), ("2025-08-01", 304.0),
        ("2025-07-01", 310.0),   # true 12-months-back value
        ("2025-06-01", 305.0),   # sits at old code's values[12] — wrong
    ]
    macro = src.parse_macro({}, econ(values), {}, {})
    assert macro["cpiYoY"] == pytest.approx((320.0 / 310.0 - 1) * 100)
