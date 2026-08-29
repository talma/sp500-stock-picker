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
