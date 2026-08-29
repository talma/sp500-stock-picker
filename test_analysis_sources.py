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
