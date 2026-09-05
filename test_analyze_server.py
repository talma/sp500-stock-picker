# test_analyze_server.py
import json

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


def test_get_daily_returns_copy_not_aliased_to_store():
    store = memory_store()
    store.put_daily("AAPL", "2026-08-29", {"meta": {"fromStore": False}})
    first = store.get_daily("AAPL", "2026-08-29")
    first["meta"]["fromStore"] = True          # mutate the caller's copy
    second = store.get_daily("AAPL", "2026-08-29")
    assert second["meta"]["fromStore"] is False    # store unaffected


def test_get_registry_returns_copy_safe_during_concurrent_mutation():
    store = memory_store()
    store.upsert_registry("AAPL", "Apple Inc.", None, "2026-08-29", SUMMARY)

    doc = store.get_registry("AAPL")
    doc["summaries"]["2026-08-29"]["grade"] = "Z"    # mutate caller's copy
    fresh = store.get_registry("AAPL")
    assert fresh["summaries"]["2026-08-29"]["grade"] == "B"

    # Iterating a returned copy while a separate upsert inserts a new date
    # must not raise "dictionary changed size during iteration" — the two
    # are now independent objects, not aliases of the same live dict.
    doc2 = store.get_registry("AAPL")
    for _ in doc2["summaries"]:
        store.upsert_registry("AAPL", "Apple Inc.", None,
                              "2026-08-28", dict(SUMMARY, grade="C"))
    assert "2026-08-28" in store.get_registry("AAPL")["summaries"]


def test_get_macro_returns_independent_copy():
    store = memory_store()
    store.put_macro("2026-08-29", {"treasury10y": 4.25})
    first = store.get_macro("2026-08-29")
    first["treasury10y"] = 999.0
    assert store.get_macro("2026-08-29")["treasury10y"] == 4.25


class FlakyBackend:
    """Always raises, with a small delay so concurrent callers are
    reliably in-flight at the same time — widens the race window this
    test needs to exercise the swap-lock fix."""

    def __getattr__(self, name):
        def boom(*args, **kwargs):
            import time
            time.sleep(0.01)
            raise ConnectionError("firestore down")
        return boom


def test_safe_swap_is_atomic_under_concurrent_failures():
    import threading

    store = Store(backend=FlakyBackend())
    store.kind = "firestore"   # simulate a live backend that starts failing
    backends_seen = {}

    def worker(ticker):
        store.put_daily(ticker, "2026-08-29", {"ticker": ticker})
        backends_seen[ticker] = store.backend

    t1 = threading.Thread(target=worker, args=("AAPL",))
    t2 = threading.Thread(target=worker, args=("MSFT",))
    t1.start()
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert store.kind == "memory"
    # Both writes must land on the SAME backend instance — an unlocked
    # swap would let each thread install its own MemoryBackend, silently
    # discarding whichever write happened first (lost update).
    assert backends_seen["AAPL"] is backends_seen["MSFT"]
    assert store.get_daily("AAPL", "2026-08-29") is not None
    assert store.get_daily("MSFT", "2026-08-29") is not None


# ==================== Task 5: analyze_server.py ====================

import types

import pandas as pd

import analysis_sources
import analyze_server
from analyze_server import Analyzer, BundleFetcher, NotFound, handle_api


# ---------- load_env ----------

@pytest.fixture
def no_config_env(monkeypatch):
    """load_env overlays the process environment on top of the file, so these
    tests must start from a known-empty environment rather than inheriting
    whatever the developer or CI runner happens to export."""
    for key in analyze_server.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_load_env(tmp_path, no_config_env):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nFMP_KEY=abc123\nALPHAVANTAGE_KEY='quoted'\n"
        "GOOGLE_APPLICATION_CREDENTIALS=./firebase-service-account.json\n"
        "BROKEN LINE NO EQUALS\n")
    env = analyze_server.load_env(env_file)
    assert env == {"FMP_KEY": "abc123", "ALPHAVANTAGE_KEY": "quoted",
                   "GOOGLE_APPLICATION_CREDENTIALS":
                       "./firebase-service-account.json"}


def test_load_env_missing_file(tmp_path, no_config_env):
    assert analyze_server.load_env(tmp_path / "nope") == {}


def test_load_env_reads_process_environment_without_a_file(
        tmp_path, monkeypatch, no_config_env):
    """The container deploy ships no .env: Fly secrets arrive as real
    environment variables, and they must still reach the clients."""
    monkeypatch.setenv("FMP_KEY", "from-secret")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "proj-1")

    env = analyze_server.load_env(tmp_path / "nope")

    assert env == {"FMP_KEY": "from-secret", "FIREBASE_PROJECT_ID": "proj-1"}


def test_load_env_process_environment_beats_the_file(
        tmp_path, monkeypatch, no_config_env):
    """A rotated deploy secret must win over a stale value baked into an
    image, so the overlay is applied after the file is parsed."""
    env_file = tmp_path / ".env"
    env_file.write_text("FMP_KEY=stale\nALPHAVANTAGE_KEY=kept\n")
    monkeypatch.setenv("FMP_KEY", "fresh")

    env = analyze_server.load_env(env_file)

    assert env == {"FMP_KEY": "fresh", "ALPHAVANTAGE_KEY": "kept"}


def test_load_env_ignores_blank_environment_variables(
        tmp_path, monkeypatch, no_config_env):
    """An empty variable is how an unset Fly secret shows up; treating it as
    a value would blank out a working .env entry on a local run."""
    env_file = tmp_path / ".env"
    env_file.write_text("FMP_KEY=real\n")
    monkeypatch.setenv("FMP_KEY", "")

    assert analyze_server.load_env(env_file) == {"FMP_KEY": "real"}


def test_load_env_overlays_only_known_keys(tmp_path, monkeypatch,
                                           no_config_env):
    """The overlay is an allowlist: the whole process environment must not
    leak into the config dict."""
    monkeypatch.setenv("PATH", "/nope")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")

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
                                   {"roe": 0.30, "fcfTTM": 1.0,
                                    "revenueGrowth": 0.12, "currentRatio": 1.4,
                                    "peTTM": 22.0}),
        yf_analyst=lambda t: {"ratings": {"strongBuy": 1, "buy": 1, "hold": 1,
                                          "sell": 0, "strongSell": 0},
                              "targets": {"low": 1, "mean": 2, "high": 3},
                              "upgradesDowngrades": []},
        parse_profile=analysis_sources.parse_profile,
        is_non_equity=analysis_sources.is_non_equity,
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
    assert sections["profile"] == "yfinance"     # was previously unrecorded
    assert sections["fundamentals"] == "yfinance"
    assert sections["analyst"] == "yfinance"
    assert sections["news"] == "unavailable"
    assert sections["technicals"] == "local"     # quota → local computation
    assert bundle["news"] is None
    assert bundle["technicals"]["rsi"]["state"] in ("overbought", "neutral",
                                                    "oversold")
    # Enough yfinance metrics to clear the grade floor, so this really is a
    # graded verdict and not the "N/A" that too-thin coverage returns.
    assert bundle["verdict"]["grade"] in "ABCDF"
    assert bundle["snapshot"]["price"] == 230.0
    assert bundle["analyst"]["targets"]["current"] == 230.0


def test_bundle_fetcher_logs_every_fallback_not_silent(capsys):
    """Previously every degradation swallowed its exception with no trace
    (`except Exception: pass`), making a real bug indistinguishable from
    ordinary quota exhaustion. Every fallback must now log."""
    fetcher = BundleFetcher(GatedFMP(), QuotaAV(), src=stub_src())
    fetcher.fetch("AAPL")
    output = capsys.readouterr().out
    for section in ("profile", "fundamentals", "analyst", "technicals",
                    "news"):
        assert f"WARNING: {section}" in output, \
            f"expected a logged WARNING for {section}, got: {output!r}"


def test_bundle_fetcher_threads_market_cap_into_build_fundamentals():
    """build_fundamentals' local fallback (netMargin/roe/fcfYield when
    FMP's ratio endpoints are gated) needs market cap, which only the
    already-fetched yfinance snapshot has — confirm it's actually wired
    through, not left at the function's default of None."""
    captured = {}

    def spy_build_fundamentals(*args, **kwargs):
        captured["market_cap"] = kwargs.get("market_cap")
        return {"quarters": [], "annual": None, "ratios": {}}, {}

    class FMPThatReachesBuildFundamentals:
        calls_used = 0
        def get(self, endpoint, **params):
            return []   # empty payload, not gated — reaches build_fundamentals

    fetcher = BundleFetcher(
        FMPThatReachesBuildFundamentals(), QuotaAV(),
        src=stub_src(build_fundamentals=spy_build_fundamentals))
    fetcher.fetch("AAPL")
    assert captured["market_cap"] == 3.5e12   # from stub_src's yf_quote


class RecordingFMP:
    """Serves a profile row and an empty payload for everything else (empty is
    a valid response, not a gating error), recording each endpoint asked for."""
    def __init__(self):
        self.calls_used = 0
        self.endpoints = []

    def get(self, endpoint, **params):
        self.calls_used += 1
        self.endpoints.append(endpoint)
        if endpoint == "profile":
            return [{"companyName": "Invesco QQQ Trust, Series 1",
                     "sector": "Financial Services",
                     "industry": "Asset Management"}]
        return []


FUNDAMENTAL_ENDPOINTS = {"income-statement", "balance-sheet-statement",
                         "cash-flow-statement", "ratios-ttm",
                         "key-metrics-ttm"}


def fund_src(quote_type="ETF"):
    return stub_src(yf_quote=lambda t: {
        "name": "Invesco QQQ Trust, Series 1", "quoteType": quote_type,
        "sector": None, "industry": None, "price": 718.96,
        "dayChangePct": 0.35, "marketCap": None, "peRatio": 29.303808,
        "week52Low": 402.39, "week52High": 720.11})


def test_fund_is_not_graded_and_its_fundamentals_are_not_fetched():
    """QQQ swung F -> A in six days because nine of ten checks were neutral
    for an ETF, leaving the grade riding on the single trailing-P/E check. A
    fund reports no company fundamentals — don't fetch or grade them."""
    fmp = RecordingFMP()
    bundle = BundleFetcher(fmp, QuotaAV(), src=fund_src()).fetch("QQQ")
    assert bundle["verdict"] is None
    assert bundle["fundamentals"] is None
    assert "fund" in bundle["meta"]["sections"]["fundamentals"]
    assert FUNDAMENTAL_ENDPOINTS.isdisjoint(fmp.endpoints)
    # The sections that do apply to a fund are unaffected.
    assert bundle["technicals"] is not None
    assert bundle["snapshot"]["price"] == 718.96


@pytest.mark.parametrize("quote_type", ["ETF", "MUTUALFUND", "INDEX"])
def test_every_non_equity_quote_type_skips_the_grade(quote_type):
    bundle = BundleFetcher(RecordingFMP(), QuotaAV(),
                           src=fund_src(quote_type)).fetch("QQQ")
    assert bundle["verdict"] is None


@pytest.mark.parametrize("quote_type", ["EQUITY", None])
def test_equity_or_unknown_quote_type_is_still_graded(quote_type):
    """A missing quoteType must not silently disable grading: only a
    positively non-equity type counts as a fund."""
    fmp = RecordingFMP()
    bundle = BundleFetcher(fmp, QuotaAV(),
                           src=fund_src(quote_type)).fetch("QQQ")
    assert bundle["verdict"] is not None
    assert FUNDAMENTAL_ENDPOINTS.issubset(fmp.endpoints)


def test_fund_does_not_take_a_sector_from_the_fmp_profile():
    """FMP files QQQ under "Financial Services", which describes the trust and
    not what it holds — the profile overlay must not relabel a fund. Its name
    still comes through."""
    bundle = BundleFetcher(RecordingFMP(), QuotaAV(),
                           src=fund_src()).fetch("QQQ")
    assert bundle["meta"]["sections"]["profile"] == "fmp"
    assert bundle["snapshot"]["name"] == "Invesco QQQ Trust, Series 1"
    assert bundle["snapshot"]["sector"] is None
    assert bundle["snapshot"]["industry"] is None


def test_ungraded_verdict_records_no_pass_ratio():
    """The history chart plots passRatio; an "N/A" verdict's passes/evaluated
    is the thin-denominator artifact the grade floor exists to suppress, so it
    must not be recorded as a data point."""
    summary = analyze_server._summary_of(
        {"verdict": {"grade": "N/A", "passes": 1, "evaluated": 1}})
    assert summary["grade"] == "N/A"
    assert summary["passRatio"] is None


def test_missing_verdict_records_no_grade_or_pass_ratio():
    summary = analyze_server._summary_of({"verdict": None})
    assert summary["grade"] is None and summary["passRatio"] is None


def test_analyzer_today_uses_utc_not_local_calendar_date():
    import datetime as dt

    analyzer = Analyzer(Store(backend=MemoryBackend()), StubFetcher())
    assert analyzer._today() == \
        dt.datetime.now(dt.timezone.utc).date().isoformat()


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


# ==================== Final review wave: Findings 3 & 4 ====================

class SlowFetcher(StubFetcher):
    """fetch() sleeps before returning, giving a second thread a window to
    observe a stale (still-empty) store.get_daily() if analyze()'s own
    check-then-act were not serialized per (ticker, date) — e.g. a
    double-submit from stock_analyzer.html's Enter-key handler firing two
    concurrent requests for the same ticker."""

    def fetch(self, ticker):
        time.sleep(0.05)
        return super().fetch(ticker)


def test_concurrent_analyze_same_ticker_is_serialized():
    store = Store(backend=MemoryBackend())
    fetcher = SlowFetcher()
    analyzer = Analyzer(store, fetcher, today=lambda: "2026-08-29")

    thread = threading.Thread(target=lambda: analyzer.analyze("AAPL"))
    thread.start()
    time.sleep(0.01)      # let the thread reach the fetch first
    analyzer.analyze("AAPL")
    thread.join(timeout=2)

    assert fetcher.fetched.count("AAPL") == 1


class SlowFetcherForTicker(StubFetcher):
    """fetch() sleeps only for one designated ticker, so a second ticker's
    analyze() call can be timed without its own fetch muddying the
    measurement of whether it waited on the first ticker's lock."""

    def __init__(self, slow_ticker, delay=0.05):
        super().__init__()
        self.slow_ticker = slow_ticker
        self.delay = delay

    def fetch(self, ticker):
        if ticker == self.slow_ticker:
            time.sleep(self.delay)
        return super().fetch(ticker)


def test_concurrent_analyze_different_tickers_are_not_serialized():
    """Per-(ticker, date) granularity: a slow fetch for one ticker must not
    block a concurrent analyze() of a different ticker (a single global
    lock would be unnecessarily conservative here)."""
    store = Store(backend=MemoryBackend())
    fetcher = SlowFetcherForTicker("AAPL", delay=0.05)
    analyzer = Analyzer(store, fetcher, today=lambda: "2026-08-29")

    thread = threading.Thread(target=lambda: analyzer.analyze("AAPL"))
    thread.start()
    time.sleep(0.01)      # let AAPL's fetch begin (and start sleeping)
    start = time.monotonic()
    analyzer.analyze("MSFT")               # must not wait for AAPL's lock
    elapsed = time.monotonic() - start
    thread.join(timeout=2)

    assert fetcher.fetched.count("MSFT") == 1
    assert elapsed < 0.03    # near-instant; would be ~0.04s+ if serialized


class RaisingAnalyzer:
    """Stand-in whose analyze() raises an exception unrelated to NotFound/
    LookupError, simulating e.g. a network/parse error surfacing from
    BundleFetcher.fetch with no enclosing try/except."""

    def analyze(self, ticker, date=None, refresh=False):
        raise RuntimeError("boom: upstream data source exploded")


def test_handle_api_unexpected_exception_is_not_caught_by_handle_api():
    # handle_api() itself only catches NotFound/LookupError around
    # /api/analyze — an unrelated exception must propagate out of it
    # (Handler.do_GET is responsible for turning it into a 500).
    with pytest.raises(RuntimeError):
        handle_api("/api/analyze", {"ticker": ["AAPL"]}, RaisingAnalyzer())


def test_do_get_returns_500_json_on_unexpected_exception(capsys):
    import io

    class FakeRequest:
        def makefile(self, *args, **kwargs):
            return io.BytesIO(b"")

    handler = analyze_server.Handler.__new__(analyze_server.Handler)
    handler.analyzer = RaisingAnalyzer()
    handler.path = "/api/analyze?ticker=AAPL"
    handler.client_address = ("127.0.0.1", 0)
    handler.request = FakeRequest()
    handler.rfile = io.BytesIO(b"")
    handler.wfile = io.BytesIO()
    status_holder = {}

    def fake_send_response(status, message=None):
        status_holder["status"] = status
    handler.send_response = fake_send_response
    handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda: None

    handler.do_GET()

    assert status_holder["status"] == 500
    body = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert "error" in body
    assert "boom" in body["error"]
    assert "ERROR" in capsys.readouterr().out   # logged, not swallowed


# ==================== Final-review fix wave: allowlist + HEAD guard ====================

@pytest.mark.parametrize("path,expected", [
    ("/stock_analyzer.html", True),
    ("/sp500_simulator.html", True),
    ("/README.md", True),
    ("/sp500_top50_rankings.csv", True),
    ("/top50_ticker_data/AAPL_monthly_10yr.csv", True),   # nested path
    ("/.env", False),
    ("/firebase-service-account.json", False),   # blocked by extension, not name
    ("/.git/config", False),
    ("/analyze_server.py", False),               # source code: not allowlisted
    ("/requirements.txt", True),
    ("/", False),                                 # no directory listing
    ("/top50_ticker_data/", False),
    ("/%2eenv", False),                           # percent-encoded dot bypass
])
def test_is_servable_static_path(path, expected):
    assert analyze_server.Handler._is_servable_static_path(path) is expected


def test_do_head_blocks_env_the_same_as_do_get():
    """Regression test: a prior fix guarded do_GET but not do_HEAD, so
    `HEAD /.env` still returned 200 with real file size/timestamp —
    leaking the credential file's existence even though its contents
    were never returned."""
    import io

    handler = analyze_server.Handler.__new__(analyze_server.Handler)
    handler.path = "/.env"
    handler.client_address = ("127.0.0.1", 0)
    handler.wfile = io.BytesIO()
    error_holder = {}

    def fake_send_error(code, message=None):
        error_holder["code"] = code
    handler.send_error = fake_send_error

    handler.do_HEAD()

    assert error_holder["code"] == 404


# ==================== Site root serves the toolkit shell ====================

def _static_handler(path):
    """A Handler wired up just enough to drive _handle_static directly."""
    import io

    handler = analyze_server.Handler.__new__(analyze_server.Handler)
    handler.path = path
    handler.client_address = ("127.0.0.1", 0)
    handler.wfile = io.BytesIO()
    calls = {"served": 0, "error": None}
    handler.send_error = lambda code, message=None: calls.__setitem__(
        "error", code)
    return handler, calls, lambda: calls.__setitem__("served",
                                                     calls["served"] + 1)


def test_handle_static_rewrites_root_to_the_shell():
    """The deployed site root has to render index.html. The allowlist rejects
    "/" by design, so the rewrite must happen before that check."""
    handler, calls, super_method = _static_handler("/")

    handler._handle_static(super_method)

    assert calls["error"] is None
    assert calls["served"] == 1
    assert handler.path == "/index.html"


def test_handle_static_root_rewrite_keeps_the_query_string():
    handler, calls, super_method = _static_handler("/?ticker=AAPL")

    handler._handle_static(super_method)

    assert calls["served"] == 1
    assert handler.path == "/index.html?ticker=AAPL"


@pytest.mark.parametrize("path", ["/top50_ticker_data/", "/docs/", "/.env"])
def test_handle_static_root_rewrite_does_not_open_up_directories(path):
    """Only the bare root is rewritten: any other directory path must stay a
    404 rather than becoming a listing."""
    handler, calls, super_method = _static_handler(path)

    handler._handle_static(super_method)

    assert calls["error"] == 404
    assert calls["served"] == 0
    assert handler.path == path


def test_do_get_serves_the_shell_at_the_root(monkeypatch):
    """End-to-end through do_GET: "/" must not be treated as an API path and
    must not 404."""
    handler, calls, _ = _static_handler("/")
    monkeypatch.setattr(analyze_server.SimpleHTTPRequestHandler, "do_GET",
                        lambda self: calls.__setitem__("served", 1))

    handler.do_GET()

    assert calls["error"] is None
    assert calls["served"] == 1
    assert handler.path == "/index.html"
