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


# ==================== Task 5: analyze_server.py ====================

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


# ==================== Task 5 fix round: review findings ====================

import threading
import time


def test_stored_bundle_is_not_aliased_to_backing_store():
    """Analyzer._stored() must hand back a copy, not the object the backend
    is holding — MemoryBackend.get_daily returns the exact stored object, so
    mutating it in place (setting fromStore/store on the returned bundle)
    would permanently corrupt the cached document on every subsequent read."""
    analyzer, store, fetcher = make_analyzer()
    analyzer.analyze("AAPL")                      # store-miss: fetch + cache
    first_read = analyzer.analyze("AAPL")          # store-hit #1
    second_read = analyzer.analyze("AAPL")         # store-hit #2

    stored = store.get_daily("AAPL", "2026-08-29")
    assert first_read is not stored
    assert first_read["meta"] is not stored["meta"]
    assert second_read is not first_read

    # Mutating a returned bundle's meta must not corrupt the backing store
    # or a later read's bundle: the store keeps the bundle exactly as it was
    # persisted at fetch time (fromStore False, since it was fresh then).
    first_read["meta"]["fromStore"] = "corrupted"
    third_read = analyzer.analyze("AAPL")
    assert third_read["meta"]["fromStore"] is True
    assert store.get_daily("AAPL", "2026-08-29")["meta"]["fromStore"] is False


class SlowMacroFetcher(StubFetcher):
    """fetch_macro sleeps before returning, giving a second thread a window
    to observe a stale (still-None) store.get_macro() if the check-then-act
    in Analyzer._macro() were not serialized."""

    def fetch_macro(self):
        time.sleep(0.05)
        return super().fetch_macro()


def test_concurrent_macro_fetch_is_serialized():
    store = Store(backend=MemoryBackend())
    fetcher = SlowMacroFetcher()
    analyzer = Analyzer(store, fetcher, today=lambda: "2026-08-29")

    thread = threading.Thread(target=lambda: analyzer.analyze("MSFT"))
    thread.start()
    time.sleep(0.01)      # let the thread reach the macro check/fetch first
    analyzer.analyze("AAPL")
    thread.join(timeout=2)

    assert fetcher.macro_calls == 1
