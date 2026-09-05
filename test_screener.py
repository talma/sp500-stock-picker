# test_screener.py
"""Screener data layer, criteria parsing, and /api/screen routing. No
network: every test drives a fake screen function."""
import json

import pytest

import analysis_sources
from analyze_server import (DEFAULT_SCREEN_CRITERIA, SCREEN_LIMIT_MAX,
                            Screener, handle_api, parse_screen_criteria)


DAY_MS = 24 * 60 * 60 * 1000
NOW_MS = 1_800_000_000_000          # fixed "now" for deterministic ages
YEAR_MS = int(365.25 * DAY_MS)


def quote(symbol, **overrides):
    """A raw Yahoo screener row, defaulting to a decade-old large cap."""
    row = {"symbol": symbol, "longName": f"{symbol} Inc.",
           "fullExchangeName": "NasdaqGS", "quoteType": "EQUITY",
           "regularMarketPrice": 100.0, "regularMarketChangePercent": 1.5,
           "marketCap": 50_000_000_000, "trailingPE": 20.0, "forwardPE": 18.0,
           "priceToBook": 5.0, "dividendYield": 1.25,
           "averageDailyVolume3Month": 3_000_000,
           "fiftyTwoWeekChangePercent": 12.0,
           "averageAnalystRating": "1.9 - Buy",
           "firstTradeDateMilliseconds": NOW_MS - 10 * YEAR_MS}
    row.update(overrides)
    return row


def fake_screen(pages):
    """Returns a yf.screen stand-in that serves `pages` in order and records
    the (offset, size, sortField, sortAsc) of every call."""
    calls = []

    def screen_fn(query, offset=None, size=None, sortField=None, sortAsc=None):
        calls.append({"offset": offset, "size": size,
                      "sortField": sortField, "sortAsc": sortAsc})
        return pages[len(calls) - 1] if len(calls) <= len(pages) else \
            {"total": 0, "quotes": []}

    screen_fn.calls = calls
    return screen_fn


# ---------- build_screen_query ----------

def test_build_screen_query_maps_every_public_filter():
    query = analysis_sources.build_screen_query({
        "exchanges": ["NMS", "NYQ"], "sector": "Technology",
        "minMarketCap": 2e9, "minAvgVolume": 5e5, "maxPeRatio": 25,
        "minRoe": 15, "minDividendYield": 1.5, "maxBeta": 1.2})
    rendered = json.dumps(query.to_dict())
    for field in ("region", "exchange", "sector", "intradaymarketcap",
                  "avgdailyvol3m", "peratio.lasttwelvemonths",
                  "returnonequity.lasttwelvemonths", "dividendyield", "beta"):
        assert field in rendered
    assert "NMS" in rendered and "NYQ" in rendered


def test_build_screen_query_omits_absent_optional_filters():
    query = analysis_sources.build_screen_query({"minMarketCap": 1e9})
    rendered = json.dumps(query.to_dict())
    assert "intradaymarketcap" in rendered
    assert "peratio" not in rendered and "beta" not in rendered


def test_build_screen_query_survives_every_filter_cleared():
    # "and" over a single operand is rejected by EquityQuery, so a criteria
    # set with nothing but the implicit region must degrade to that one term
    # rather than raising.
    query = analysis_sources.build_screen_query({})
    assert query.to_dict() == {"operator": "EQ", "operands": ["region", "us"]}


def test_build_screen_query_rejects_unknown_exchange_and_sector():
    with pytest.raises(ValueError, match="Unsupported exchange"):
        analysis_sources.build_screen_query({"exchanges": ["NMS", "MOON"]})
    # Sector values are validated by the query class itself.
    with pytest.raises(ValueError):
        analysis_sources.build_screen_query({"sector": "Cryptozoology"})


def test_screen_exchanges_exclude_otc_venues():
    # OTC/pink-sheet venues must stay out of an "established listings"
    # screener even though Yahoo accepts them as filter values.
    assert not {"PNK", "OEM", "OQB", "OQX"} & set(
        analysis_sources.SCREEN_EXCHANGES)


# ---------- yf_screen_all pagination ----------

def test_yf_screen_all_paginates_and_reports_total():
    pages = [{"total": 600, "quotes": [quote(f"A{i}") for i in range(250)]},
             {"total": 600, "quotes": [quote(f"B{i}") for i in range(250)]}]
    screen_fn = fake_screen(pages)
    rows, total = analysis_sources.yf_screen_all(
        "q", limit=500, screen_fn=screen_fn)
    assert len(rows) == 500 and total == 600
    assert [c["offset"] for c in screen_fn.calls] == [0, 250]
    assert [c["size"] for c in screen_fn.calls] == [250, 250]


def test_yf_screen_all_caps_page_size_at_yahoos_limit():
    screen_fn = fake_screen([{"total": 900,
                              "quotes": [quote(f"A{i}") for i in range(250)]}])
    analysis_sources.yf_screen_all("q", limit=400, screen_fn=screen_fn)
    assert screen_fn.calls[0]["size"] == analysis_sources.SCREEN_PAGE_MAX


def test_yf_screen_all_last_page_requests_only_the_remainder():
    pages = [{"total": 900, "quotes": [quote(f"A{i}") for i in range(250)]},
             {"total": 900, "quotes": [quote(f"B{i}") for i in range(50)]}]
    screen_fn = fake_screen(pages)
    rows, _ = analysis_sources.yf_screen_all("q", limit=300,
                                             screen_fn=screen_fn)
    assert len(rows) == 300
    assert screen_fn.calls[1]["size"] == 50


def test_yf_screen_all_stops_on_short_page_even_if_total_overstates():
    # Yahoo reporting more matches than it will hand back must terminate the
    # loop, not spin on offset forever.
    screen_fn = fake_screen([{"total": 10_000,
                              "quotes": [quote("AAA"), quote("BBB")]}])
    rows, total = analysis_sources.yf_screen_all("q", limit=500,
                                                 screen_fn=screen_fn)
    assert len(rows) == 2 and total == 10_000
    assert len(screen_fn.calls) == 1


def test_yf_screen_all_stops_on_empty_first_page():
    screen_fn = fake_screen([{"total": 0, "quotes": []}])
    rows, total = analysis_sources.yf_screen_all("q", limit=250,
                                                 screen_fn=screen_fn)
    assert rows == [] and total == 0
    assert len(screen_fn.calls) == 1


def test_yf_screen_all_tolerates_missing_keys_and_none_payload():
    screen_fn = fake_screen([None])
    rows, total = analysis_sources.yf_screen_all("q", limit=250,
                                                 screen_fn=screen_fn)
    assert rows == [] and total == 0


def test_yf_screen_all_passes_sort_through():
    screen_fn = fake_screen([{"total": 1, "quotes": [quote("AAA")]}])
    analysis_sources.yf_screen_all("q", limit=10, sort_field="dividendyield",
                                   sort_asc=True, screen_fn=screen_fn)
    assert screen_fn.calls[0]["sortField"] == "dividendyield"
    assert screen_fn.calls[0]["sortAsc"] is True


# ---------- transient upstream failures ----------

def flaky_screen(outcomes):
    """A yf.screen stand-in that raises or returns per `outcomes`, in order."""
    calls = []

    def screen_fn(query, offset=None, size=None, sortField=None, sortAsc=None):
        calls.append(offset)
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    screen_fn.calls = calls
    return screen_fn


def recorder():
    waits = []
    return waits, waits.append


def test_yf_screen_all_retries_a_timeout_and_succeeds():
    # The real failure this guards against: Yahoo's screener timing out on the
    # first request but answering fine on the next one.
    page = {"total": 1, "quotes": [quote("AAA")]}
    screen_fn = flaky_screen([TimeoutError("curl: (28) timed out"), page])
    waits, sleep = recorder()
    rows, total = analysis_sources.yf_screen_all(
        "q", limit=10, screen_fn=screen_fn, sleep=sleep, clock=lambda: 0.0)
    assert [row["symbol"] for row in rows] == ["AAA"]
    assert total == 1
    assert len(screen_fn.calls) == 2
    assert waits == [analysis_sources.SCREEN_RETRY_WAIT]


def test_yf_screen_all_backs_off_progressively_then_gives_up():
    screen_fn = flaky_screen([TimeoutError("curl: (28) timed out")])
    waits, sleep = recorder()
    with pytest.raises(analysis_sources.ScreenUnavailable) as caught:
        analysis_sources.yf_screen_all("q", limit=10, screen_fn=screen_fn,
                                       sleep=sleep, clock=lambda: 0.0)
    assert len(screen_fn.calls) == analysis_sources.SCREEN_ATTEMPTS
    # One wait fewer than attempts: nothing is slept after the last failure.
    assert waits == [analysis_sources.SCREEN_RETRY_WAIT * n
                     for n in range(1, analysis_sources.SCREEN_ATTEMPTS)]
    message = str(caught.value)
    assert "TimeoutError" in message      # names the underlying cause
    assert str(analysis_sources.SCREEN_ATTEMPTS) in message
    assert caught.value.__cause__ is not None


def test_screen_unavailable_is_not_a_value_error():
    # handle_api routes ValueError to 400 and ScreenUnavailable to 503, so the
    # two must not share a base class.
    assert not issubclass(analysis_sources.ScreenUnavailable, ValueError)


def test_yf_screen_all_does_not_retry_programming_errors():
    # A malformed query or call is a bug, not a flake: retrying it three times
    # only delays the report by the length of the backoff.
    for error in (ValueError("Invalid EQ value"), TypeError("bad argument")):
        screen_fn = flaky_screen([error])
        waits, sleep = recorder()
        with pytest.raises(type(error)):
            analysis_sources.yf_screen_all("q", limit=10, screen_fn=screen_fn,
                                           sleep=sleep, clock=lambda: 0.0)
        assert len(screen_fn.calls) == 1
        assert waits == []


def test_yf_screen_all_stops_retrying_once_the_deadline_passes():
    screen_fn = flaky_screen([TimeoutError("slow")])
    waits, sleep = recorder()
    clock = iter([0.0] + [analysis_sources.SCREEN_DEADLINE + 1] * 20)
    with pytest.raises(analysis_sources.ScreenUnavailable):
        analysis_sources.yf_screen_all("q", limit=10, screen_fn=screen_fn,
                                       sleep=sleep, clock=lambda: next(clock))
    assert len(screen_fn.calls) == 1      # no retry after the deadline
    assert waits == []


def test_yf_screen_all_returns_partial_rows_when_the_deadline_passes():
    # Mid-pagination timeout budget exhaustion returns what arrived rather
    # than failing: the caller reports fetched-vs-total, so truncation is
    # already visible without an error.
    pages = [{"total": 900, "quotes": [quote(f"A{i}") for i in range(250)]},
             {"total": 900, "quotes": [quote(f"B{i}") for i in range(250)]}]
    screen_fn = fake_screen(pages)
    # First tick sets the deadline; the next is the post-page deadline check,
    # which is where the budget runs out.
    ticks = iter([0.0, analysis_sources.SCREEN_DEADLINE + 1] + [1e9] * 20)
    rows, total = analysis_sources.yf_screen_all(
        "q", limit=500, screen_fn=screen_fn, sleep=lambda _: None,
        clock=lambda: next(ticks))
    assert len(rows) == 250 and total == 900
    assert len(screen_fn.calls) == 1


# ---------- parse_screen_row ----------

def test_parse_screen_row_maps_fields_and_derives_history_years():
    parsed = analysis_sources.parse_screen_row(quote("AAPL"), NOW_MS)
    assert parsed["ticker"] == "AAPL"
    assert parsed["name"] == "AAPL Inc."
    assert parsed["exchange"] == "NasdaqGS"
    assert parsed["price"] == 100.0
    assert parsed["marketCap"] == 50_000_000_000
    assert parsed["peRatio"] == 20.0
    assert parsed["dividendYield"] == 1.25
    assert parsed["analystRating"] == "1.9 - Buy"
    assert parsed["historyYears"] == pytest.approx(10, abs=0.01)


def test_parse_screen_row_falls_back_through_name_fields():
    row = quote("XYZ")
    del row["longName"]
    row["shortName"] = "Xyz Co"
    assert analysis_sources.parse_screen_row(row, NOW_MS)["name"] == "Xyz Co"
    row.pop("shortName")
    # With every name field gone the ticker stands in, so the column is
    # never blank.
    assert analysis_sources.parse_screen_row(row, NOW_MS)["name"] == "XYZ"


def test_parse_screen_row_history_years_is_none_without_listing_date():
    row = quote("NEW", firstTradeDateMilliseconds=None)
    assert analysis_sources.parse_screen_row(row, NOW_MS)["historyYears"] is None


# ---------- filter_established ----------

def test_filter_established_keeps_only_seasoned_common_equity():
    rows = [
        quote("OLD", firstTradeDateMilliseconds=NOW_MS - 10 * YEAR_MS),
        quote("YEAR", firstTradeDateMilliseconds=NOW_MS - 2 * YEAR_MS),
        quote("IPO", firstTradeDateMilliseconds=NOW_MS - 30 * DAY_MS),
        quote("NODATE", firstTradeDateMilliseconds=None),
        quote("FUND", quoteType="ETF"),
    ]
    kept, dropped = analysis_sources.filter_established(rows, 1.0, NOW_MS)
    assert [row["ticker"] for row in kept] == ["OLD", "YEAR"]
    assert dropped == {"nonEquity": 1, "unknownListing": 1, "tooYoung": 1}


def test_filter_established_boundary_is_inclusive():
    # Exactly at the threshold counts as established: the gate is "at least
    # N years", so an on-the-nose anniversary must not be dropped.
    rows = [quote("EXACT", firstTradeDateMilliseconds=NOW_MS - YEAR_MS),
            quote("SHORT",
                  firstTradeDateMilliseconds=NOW_MS - YEAR_MS + DAY_MS)]
    kept, dropped = analysis_sources.filter_established(rows, 1.0, NOW_MS)
    assert [row["ticker"] for row in kept] == ["EXACT"]
    assert dropped["tooYoung"] == 1


def test_filter_established_tolerates_missing_quote_type():
    row = quote("AAA")
    del row["quoteType"]
    kept, dropped = analysis_sources.filter_established([row], 1.0, NOW_MS)
    assert [r["ticker"] for r in kept] == ["AAA"]
    assert dropped["nonEquity"] == 0


def test_filter_established_zero_year_gate_still_drops_non_equity():
    rows = [quote("FUND", quoteType="ETF"),
            quote("NEW", firstTradeDateMilliseconds=NOW_MS - DAY_MS)]
    kept, dropped = analysis_sources.filter_established(rows, 0.0, NOW_MS)
    assert [row["ticker"] for row in kept] == ["NEW"]
    assert dropped["nonEquity"] == 1


# ---------- Screener ----------

class StubSource:
    """Stands in for analysis_sources: records the query criteria it was
    handed and serves canned rows."""

    def __init__(self, rows, total=None):
        self.rows = rows
        self.total = total if total is not None else len(rows)
        self.queries = []
        self.screens = 0

    def build_screen_query(self, criteria):
        self.queries.append(criteria)
        return "QUERY"

    def yf_screen_all(self, query, limit, sort_field, sort_asc):
        self.screens += 1
        self.last_call = {"limit": limit, "sortField": sort_field,
                          "sortAsc": sort_asc}
        return self.rows[:limit], self.total

    def filter_established(self, rows, min_history_years, now_ms):
        return analysis_sources.filter_established(
            rows, min_history_years, now_ms)


def criteria(**overrides):
    return dict(DEFAULT_SCREEN_CRITERIA,
                exchanges=list(DEFAULT_SCREEN_CRITERIA["exchanges"]),
                **overrides)


def test_screener_returns_kept_rows_with_drop_accounting():
    src = StubSource([quote("OLD"), quote("IPO",
                      firstTradeDateMilliseconds=NOW_MS - DAY_MS)], total=900)
    screener = Screener(src=src, now=lambda: NOW_MS / 1000)
    payload = screener.screen(criteria())
    assert [row["ticker"] for row in payload["results"]] == ["OLD"]
    assert payload["meta"]["totalMatches"] == 900
    assert payload["meta"]["fetched"] == 2
    assert payload["meta"]["kept"] == 1
    assert payload["meta"]["dropped"]["tooYoung"] == 1
    assert payload["meta"]["fromCache"] is False
    assert src.last_call == {"limit": 100,
                             "sortField": "intradaymarketcap",
                             "sortAsc": False}


def test_screener_caches_identical_criteria_within_ttl():
    src = StubSource([quote("AAA")])
    clock = [NOW_MS / 1000]
    screener = Screener(src=src, ttl_seconds=900, now=lambda: clock[0])
    first = screener.screen(criteria())
    second = screener.screen(criteria())
    assert src.screens == 1
    assert first["meta"]["fromCache"] is False
    assert second["meta"]["fromCache"] is True
    assert [r["ticker"] for r in second["results"]] == ["AAA"]


def test_screener_refetches_after_ttl_expires():
    src = StubSource([quote("AAA")])
    clock = [NOW_MS / 1000]
    screener = Screener(src=src, ttl_seconds=900, now=lambda: clock[0])
    screener.screen(criteria())
    clock[0] += 901
    payload = screener.screen(criteria())
    assert src.screens == 2
    assert payload["meta"]["fromCache"] is False


def test_screener_treats_different_criteria_as_different_cache_entries():
    src = StubSource([quote("AAA")])
    screener = Screener(src=src, now=lambda: NOW_MS / 1000)
    screener.screen(criteria())
    screener.screen(criteria(minMarketCap=5e9))
    screener.screen(criteria(sortAsc=True))
    assert src.screens == 3


def test_screener_cached_payload_is_not_aliased_to_the_cache():
    src = StubSource([quote("AAA")])
    screener = Screener(src=src, now=lambda: NOW_MS / 1000)
    first = screener.screen(criteria())
    first["meta"]["totalMatches"] = 999_999      # mutate the caller's copy
    second = screener.screen(criteria())
    assert second["meta"]["totalMatches"] == 1


def test_screener_cache_is_bounded():
    src = StubSource([quote("AAA")])
    screener = Screener(src=src, now=lambda: NOW_MS / 1000)
    for cap in range(Screener.CACHE_MAX + 10):
        screener.screen(criteria(minMarketCap=float(cap)))
    assert len(screener._cache) == Screener.CACHE_MAX


def test_screener_passes_history_gate_through_to_the_filter():
    src = StubSource([quote("MID",
                            firstTradeDateMilliseconds=NOW_MS - 2 * YEAR_MS)])
    screener = Screener(src=src, now=lambda: NOW_MS / 1000)
    assert screener.screen(criteria(minHistoryYears=1.0))["meta"]["kept"] == 1
    assert screener.screen(criteria(minHistoryYears=5.0))["meta"]["kept"] == 0


# ---------- parse_screen_criteria ----------

def as_param(mapping):
    return lambda name: mapping.get(name)


def test_parse_screen_criteria_defaults():
    parsed = parse_screen_criteria(as_param({}))
    assert parsed["exchanges"] == ["NMS", "NYQ"]
    assert parsed["minMarketCap"] == 2_000_000_000
    assert parsed["minAvgVolume"] == 500_000
    assert parsed["minHistoryYears"] == 1.0
    assert parsed["limit"] == 100
    assert parsed["sortField"] == "intradaymarketcap"
    assert parsed["sortAsc"] is False
    assert "sector" not in parsed


def test_parse_screen_criteria_does_not_mutate_the_defaults():
    parse_screen_criteria(as_param({"exchanges": "ASE"}))
    assert DEFAULT_SCREEN_CRITERIA["exchanges"] == ["NMS", "NYQ"]


def test_parse_screen_criteria_reads_every_filter():
    parsed = parse_screen_criteria(as_param({
        "exchanges": "nms, ase ", "sector": "Technology",
        "minMarketCap": "1e10", "minAvgVolume": "250000",
        "minHistoryYears": "3", "maxPeRatio": "25", "minRoe": "15",
        "minDividendYield": "1.5", "maxBeta": "1.2", "limit": "250",
        "sortField": "dividendyield", "sortAsc": "1"}))
    assert parsed["exchanges"] == ["NMS", "ASE"]     # trimmed and upper-cased
    assert parsed["sector"] == "Technology"
    assert parsed["minMarketCap"] == 1e10
    assert parsed["minHistoryYears"] == 3.0
    assert parsed["maxPeRatio"] == 25.0
    assert parsed["maxBeta"] == 1.2
    assert parsed["limit"] == 250
    assert parsed["sortField"] == "dividendyield"
    assert parsed["sortAsc"] is True


def test_parse_screen_criteria_blank_optional_filter_keeps_default():
    parsed = parse_screen_criteria(as_param({"minMarketCap": "",
                                             "maxPeRatio": ""}))
    assert parsed["minMarketCap"] == 2_000_000_000
    assert "maxPeRatio" not in parsed


@pytest.mark.parametrize("params, message", [
    ({"minMarketCap": "abc"}, "must be a number"),
    ({"minMarketCap": "nan"}, "finite"),
    ({"minMarketCap": "inf"}, "finite"),
    ({"minMarketCap": "-inf"}, "finite"),
    ({"minHistoryYears": "-1"}, "must not be negative"),
    ({"limit": "0"}, "between 1"),
    ({"limit": str(SCREEN_LIMIT_MAX + 1)}, "between 1"),
    ({"sortField": "marketCap"}, "Unsupported sortField"),
    ({"exchanges": " , "}, "at least one exchange"),
])
def test_parse_screen_criteria_rejects_bad_input(params, message):
    with pytest.raises(ValueError, match=message):
        parse_screen_criteria(as_param(params))


def test_parse_screen_criteria_rejects_non_finite_because_json_cannot_hold_it():
    # NaN/inf reach the page as bare NaN/Infinity tokens, which json.loads in
    # the browser rejects — the request must fail with a readable message
    # instead of an unparseable body.
    with pytest.raises(ValueError):
        parse_screen_criteria(as_param({"maxBeta": "NaN"}))
    criteria_ok = parse_screen_criteria(as_param({"maxBeta": "1.5"}))
    assert json.dumps(criteria_ok)          # round-trips as valid JSON


# ---------- /api/screen routing ----------

def screening_handler(rows=None, total=None):
    src = StubSource(rows if rows is not None else [quote("AAA")], total=total)
    return Screener(src=src, now=lambda: NOW_MS / 1000), src


def test_handle_api_screen_ok():
    screener, src = screening_handler([quote("AAA"), quote("BBB")], total=42)
    status, payload = handle_api("/api/screen", {}, None, screener)
    assert status == 200
    assert [row["ticker"] for row in payload["results"]] == ["AAA", "BBB"]
    assert payload["meta"]["totalMatches"] == 42
    assert src.queries[0]["exchanges"] == ["NMS", "NYQ"]


def test_handle_api_screen_forwards_query_parameters():
    screener, src = screening_handler()
    status, _ = handle_api("/api/screen",
                           {"sector": ["Technology"], "maxPeRatio": ["18"],
                            "exchanges": ["NYQ"], "limit": ["50"]},
                           None, screener)
    assert status == 200
    assert src.queries[0]["sector"] == "Technology"
    assert src.queries[0]["maxPeRatio"] == 18.0
    assert src.queries[0]["exchanges"] == ["NYQ"]
    assert src.last_call["limit"] == 50


def test_handle_api_screen_bad_parameter_is_400_not_500():
    screener, _ = screening_handler()
    status, payload = handle_api("/api/screen", {"limit": ["9999"]},
                                 None, screener)
    assert status == 400 and "limit" in payload["error"]


def test_handle_api_screen_rejected_query_is_400():
    # A ValueError raised inside build_screen_query (unknown exchange, bad
    # sector) is the caller's fault too, so it must not become a 500.
    class RejectingSource(StubSource):
        def build_screen_query(self, criteria):
            raise ValueError("Unsupported exchange(s): MOON")

    screener = Screener(src=RejectingSource([]), now=lambda: NOW_MS / 1000)
    status, payload = handle_api("/api/screen", {"exchanges": ["MOON"]},
                                 None, screener)
    assert status == 400 and "MOON" in payload["error"]


def test_handle_api_screen_without_a_screener_is_404():
    status, payload = handle_api("/api/screen", {}, None, None)
    assert status == 404 and "not enabled" in payload["error"]


def test_handle_api_screen_upstream_failure_is_not_swallowed():
    # A genuine, unexpected error must reach do_GET's 500 handler rather than
    # be reported as a 400 or an empty result set.
    class ExplodingSource(StubSource):
        def yf_screen_all(self, *args, **kwargs):
            raise ConnectionError("yahoo unreachable")

    screener = Screener(src=ExplodingSource([]), now=lambda: NOW_MS / 1000)
    with pytest.raises(ConnectionError):
        handle_api("/api/screen", {}, None, screener)


def test_handle_api_screen_unavailable_is_503_with_an_actionable_message(capsys):
    # The bug this covers: a Yahoo timeout surfaced as a 500 carrying a raw
    # "curl: (28) Operation timed out" string, which tells the user nothing
    # about what to do next.
    class TimingOutSource(StubSource):
        def yf_screen_all(self, *args, **kwargs):
            raise analysis_sources.ScreenUnavailable(
                "Yahoo's screener did not respond after 3 attempt(s) "
                "(TimeoutError). This is usually transient — try again, or "
                "lower the row limit.")

    screener = Screener(src=TimingOutSource([]), now=lambda: NOW_MS / 1000)
    status, payload = handle_api("/api/screen", {}, None, screener)
    assert status == 503
    assert "try again" in payload["error"]
    assert "curl:" not in payload["error"]
    assert "WARNING" in capsys.readouterr().out      # logged, not silent


def test_handle_api_screen_failure_is_not_cached():
    # A transient failure must not poison the cache: the next identical
    # request has to actually retry rather than replay the error.
    class FlakySource(StubSource):
        def __init__(self, rows):
            super().__init__(rows)
            self.attempts = 0

        def yf_screen_all(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise analysis_sources.ScreenUnavailable("timed out")
            return super().yf_screen_all(*args, **kwargs)

    src = FlakySource([quote("AAA")])
    screener = Screener(src=src, now=lambda: NOW_MS / 1000)
    status, _ = handle_api("/api/screen", {}, None, screener)
    assert status == 503
    status, payload = handle_api("/api/screen", {}, None, screener)
    assert status == 200
    assert [row["ticker"] for row in payload["results"]] == ["AAA"]
    assert payload["meta"]["fromCache"] is False


def test_handle_api_existing_routes_still_work_without_a_screener():
    # handle_api's screener parameter is optional; the analyze/analyzed/
    # history routes must be unaffected by its absence.
    status, payload = handle_api("/api/nope", {}, None)
    assert status == 404 and "Unknown API path" in payload["error"]
