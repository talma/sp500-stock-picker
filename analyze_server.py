# analyze_server.py
"""Local analysis server: static files + /api/analyze|analyzed|history|screen.
Run: python3 analyze_server.py [--port 8000] [--local-technicals]"""
import argparse
import collections
import datetime
import json
import os
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import truststore

import analysis_sources
import firestore_store
import value_grade
from firestore_store import _now_iso


# Config keys the server reads. A container deploy ships no .env and injects
# these as real environment variables instead (Fly secrets), so load_env has
# to consider both sources rather than the file alone.
ENV_KEYS = ("ALPHAVANTAGE_KEY", "FMP_KEY", "FIREBASE_PROJECT_ID",
            "GOOGLE_APPLICATION_CREDENTIALS")


def load_env(path=".env"):
    """Reads `path` if it exists, then lets real environment variables win.

    Local runs keep working off the .env file. In a container there is no
    .env and the process environment is the only source; the overlay also
    means a deployed secret always beats a stale value baked into an image."""
    values = {}
    env_path = Path(path)
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    for key in ENV_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


class NotFound(Exception):
    pass


def _log_degradation(section, ticker, error, note=""):
    """Every fallback/failure path logs — a silent `except: pass` makes a
    genuine bug (a bad response shape, a permissions error) indistinguishable
    from ordinary quota exhaustion, with nothing in the server log to tell
    them apart."""
    suffix = f"; {note}" if note else ""
    print(f"WARNING: {section} fetch failed for {ticker} ({error}){suffix}")


def _with_fallback(sections, key, ticker, primary, primary_label,
                   fallback, fallback_label):
    """Try `primary()`; on any exception, try `fallback()`; record which
    source won (or that both failed) in sections[key]. Returns the winning
    call's result, or None if both failed. Both callables take no args —
    callers close over what they need."""
    try:
        result = primary()
        sections[key] = primary_label
        return result
    except Exception as error:
        _log_degradation(key, ticker, error, f"falling back to {fallback_label}")
        try:
            result = fallback()
            sections[key] = fallback_label
            return result
        except Exception as fallback_error:
            _log_degradation(key, ticker, fallback_error,
                             "fallback also failed; marking unavailable")
            sections[key] = "unavailable"
            return None


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
            sections["profile"] = "fmp"
        except Exception as error:
            _log_degradation("profile", ticker, error,
                             "using yfinance-only snapshot")
            sections["profile"] = "yfinance"

        result = _with_fallback(
            sections, "fundamentals", ticker,
            lambda: self.src.build_fundamentals(
                self.fmp.get("income-statement", symbol=ticker,
                             period="quarter", limit=8),
                self.fmp.get("balance-sheet-statement", symbol=ticker,
                             period="quarter", limit=1),
                self.fmp.get("cash-flow-statement", symbol=ticker,
                             period="quarter", limit=4),
                self.fmp.get("income-statement", symbol=ticker,
                             period="annual", limit=1),
                self.fmp.get("ratios-ttm", symbol=ticker),
                self.fmp.get("key-metrics-ttm", symbol=ticker),
                market_cap=snapshot.get("marketCap")),
            "fmp",
            lambda: self.src.yf_fundamentals(ticker),
            "yfinance")
        fundamentals, metrics = result if result is not None else (None, None)
        verdict = self.grader.compute_verdict(metrics) \
            if metrics is not None else None

        analyst = _with_fallback(
            sections, "analyst", ticker,
            lambda: self.src.parse_analyst(
                self.fmp.get("price-target-consensus", symbol=ticker),
                self.fmp.get("grades-consensus", symbol=ticker),
                self.fmp.get("grades", symbol=ticker, limit=10)),
            "fmp",
            lambda: self.src.yf_analyst(ticker),
            "yfinance")
        if analyst:
            analyst["targets"]["current"] = snapshot.get("price")

        technicals = None
        try:
            close = self.src.yf_prices(ticker)
        except Exception as error:
            _log_degradation("technicals", ticker, error, "no price history")
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
            except Exception as error:
                _log_degradation("technicals", ticker, error,
                                 "falling back to local computation")
                technicals = self.src.compute_local_technicals(close)
                sections["technicals"] = "local"

        news = None
        try:
            news = self.src.parse_news(
                self.av.get("NEWS_SENTIMENT", tickers=ticker,
                            sort="LATEST", limit=50))
            sections["news"] = "ok"
        except Exception as error:
            _log_degradation("news", ticker, error)
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
        # UTC, not naive local time: every stored timestamp (_now_iso) is
        # UTC-aware, so bucketing "today" by local calendar date would let
        # the day-key and its own recorded fetchedAt disagree near midnight
        # in any non-UTC deployment, corrupting "latest edition" tracking.
        self._today = today or (
            lambda: datetime.datetime.now(datetime.timezone.utc).date().isoformat())
        self._macro_lock = threading.Lock()
        # Per-(ticker, date) locks, created lazily. Guarded by one outer lock
        # so concurrent first-time lookups for different keys don't race on
        # dict insertion, while different tickers still run in parallel.
        self._analyze_locks = collections.defaultdict(threading.Lock)
        self._analyze_locks_guard = threading.Lock()

    def _lock_for(self, ticker, date):
        with self._analyze_locks_guard:
            return self._analyze_locks[(ticker, date)]

    def _stored(self, bundle):
        # Shallow-copy before mutating: MemoryBackend.get_daily returns the
        # exact object it has stored (no copy), so mutating in place would
        # permanently corrupt the cached document on every read.
        bundle = dict(bundle)
        bundle["meta"] = dict(bundle.get("meta") or {})
        bundle["meta"]["fromStore"] = True
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
        # Serialize the check-then-act per (ticker, date): two concurrent
        # requests for the same ticker (e.g. a double-submit from the UI)
        # could otherwise both miss the store and both fetch, wasting API
        # quota and racing on the write. Different tickers/dates don't
        # contend with each other.
        with self._lock_for(ticker, today):
            if not refresh:
                bundle = self.store.get_daily(ticker, today)
                if bundle is not None:
                    return self._stored(bundle)
            bundle = self.fetcher.fetch(ticker)    # LookupError propagates
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
        # Serialize the check-then-act: under ThreadingHTTPServer, two
        # concurrent first-of-day requests could both observe a cache miss
        # and both fetch, blowing the shared daily AV macro-call budget.
        with self._macro_lock:
            macro = self.store.get_macro(today)
            if macro is None:
                try:
                    macro = self.fetcher.fetch_macro()
                    self.store.put_macro(today, macro)
                except Exception as error:
                    _log_degradation("macro", today, error)
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


DEFAULT_SCREEN_CRITERIA = {
    "exchanges": ["NMS", "NYQ"],       # NasdaqGS + NYSE: the established venues
    "minMarketCap": 2_000_000_000,
    "minAvgVolume": 500_000,
    "minHistoryYears": 1.0,
    "sortField": "intradaymarketcap",
    "sortAsc": False,
    "limit": 100,
}
SCREEN_LIMIT_MAX = 500


class Screener:
    """Runs Yahoo screens and applies the established-listing gate.

    Results are cached in memory for `ttl_seconds` per distinct criteria set,
    because the page re-runs the same screen on every reload and every
    re-scan. Nothing is written to Firestore: a screen is a snapshot of live
    market state, not an analysis worth archiving alongside dated bundles.

    Unlike Analyzer._macro, the fetch deliberately runs *outside* the lock.
    The macro path serializes because Alpha Vantage has a hard 25-call daily
    budget that a duplicate fetch permanently spends; Yahoo's screener needs
    no key and has no such budget, so two concurrent identical screens cost
    only a little redundant time — not worth blocking every other screen for
    the duration of a network call.
    """

    CACHE_MAX = 32              # bounded: criteria come from query params

    def __init__(self, src=analysis_sources, ttl_seconds=900, now=None):
        self.src = src
        self.ttl_seconds = ttl_seconds
        self._now = now or time.time
        self._cache = collections.OrderedDict()
        self._cache_lock = threading.Lock()

    def _cache_key(self, criteria):
        return json.dumps(criteria, sort_keys=True, default=str)

    @staticmethod
    def _detach(payload, from_cache):
        """An independent view of a cached payload. The meta dict and the
        results list are copied, not shared: handing back the stored objects
        would let one caller's mutation corrupt what every later caller
        reads — the same aliasing guard the store makes for daily bundles.
        The row dicts inside the list stay shared, and are only ever read."""
        detached = dict(payload)
        detached["meta"] = dict(payload["meta"], fromCache=from_cache)
        detached["results"] = list(payload["results"])
        return detached

    def _cached(self, key):
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None or self._now() - entry[0] >= self.ttl_seconds:
                return None
            self._cache.move_to_end(key)
            payload = entry[1]
        return self._detach(payload, True)

    def _remember(self, key, payload):
        with self._cache_lock:
            self._cache[key] = (self._now(), payload)
            self._cache.move_to_end(key)
            while len(self._cache) > self.CACHE_MAX:
                self._cache.popitem(last=False)

    def screen(self, criteria):
        key = self._cache_key(criteria)
        cached = self._cached(key)
        if cached is not None:
            return cached
        query = self.src.build_screen_query(criteria)   # ValueError if invalid
        rows, total = self.src.yf_screen_all(
            query, limit=criteria["limit"],
            sort_field=criteria["sortField"], sort_asc=criteria["sortAsc"])
        kept, dropped = self.src.filter_established(
            rows, criteria["minHistoryYears"], self._now() * 1000)
        payload = {"criteria": criteria,
                   "results": kept,
                   "meta": {"fetchedAt": _now_iso(),
                            "totalMatches": total,
                            "fetched": len(rows),
                            "kept": len(kept),
                            "dropped": dropped,
                            "fromCache": False}}
        self._remember(key, payload)
        return self._detach(payload, False)


def _screen_number(raw, name):
    """Coerce one screener query parameter to a finite, non-negative float.

    NaN and infinity are rejected rather than passed through: json.dumps
    renders them as bare NaN/Infinity tokens, which are not legal JSON, and
    the page would receive a body it cannot parse instead of a clear error."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got: {raw!r}")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be a finite number, got: {raw!r}")
    if value < 0:
        raise ValueError(f"{name} must not be negative, got: {raw!r}")
    return value


def parse_screen_criteria(param):
    """Query parameters -> a fully defaulted criteria dict. Raises
    ValueError with a message meant for the user on anything unparseable."""
    criteria = dict(DEFAULT_SCREEN_CRITERIA,
                    exchanges=list(DEFAULT_SCREEN_CRITERIA["exchanges"]))

    raw_exchanges = param("exchanges")
    if raw_exchanges is not None:
        codes = [code.strip().upper()
                 for code in raw_exchanges.split(",") if code.strip()]
        if not codes:
            raise ValueError("exchanges must name at least one exchange code")
        criteria["exchanges"] = codes

    if param("sector"):
        criteria["sector"] = param("sector")

    for name in (*analysis_sources.SCREEN_NUMERIC_FILTERS, "minHistoryYears"):
        raw = param(name)
        if raw not in (None, ""):
            criteria[name] = _screen_number(raw, name)

    raw_limit = param("limit")
    if raw_limit not in (None, ""):
        limit = int(_screen_number(raw_limit, "limit"))
        if not 1 <= limit <= SCREEN_LIMIT_MAX:
            raise ValueError(
                f"limit must be between 1 and {SCREEN_LIMIT_MAX}, "
                f"got: {raw_limit!r}")
        criteria["limit"] = limit

    sort_field = param("sortField")
    if sort_field:
        if sort_field not in analysis_sources.SCREEN_SORT_FIELDS:
            raise ValueError(f"Unsupported sortField: {sort_field}")
        criteria["sortField"] = sort_field
    criteria["sortAsc"] = param("sortAsc") == "1"
    return criteria


def handle_api(path, query, analyzer, screener=None):
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

    if path == "/api/screen":
        if screener is None:
            return 404, {"error": "Screening is not enabled on this server"}
        try:
            criteria = parse_screen_criteria(param)
            return 200, screener.screen(criteria)
        except analysis_sources.ScreenUnavailable as error:
            # Upstream was unreachable, not a bad request: 503 tells the page
            # (and any client) that retrying the same criteria is worthwhile.
            _log_degradation("screen", "yahoo", error)
            return 503, {"error": str(error)}
        except ValueError as error:
            # Both the parameter coercion above and the query builder inside
            # screen() reject bad input this way — either is the caller's
            # fault, so both are a 400 rather than a server error.
            return 400, {"error": str(error)}

    return 404, {"error": f"Unknown API path: {path}"}


class Handler(SimpleHTTPRequestHandler):
    analyzer = None    # set in main()
    screener = None    # set in main()

    # Only these extensions are ever servable as static files. An allowlist
    # keeps "a new file appears in the repo root" default to *not served* —
    # unlike a blocklist (which must be remembered and updated by hand
    # every time a new secret-bearing file, like a second credentials
    # file, is added), this also closes off Python source, directory
    # listings, and anything else with no reason to be browsable.
    _ALLOWED_STATIC_SUFFIXES = {".html", ".htm", ".css", ".js", ".csv",
                                ".md", ".txt"}

    # The site root is the toolkit shell. This is a rewrite to an explicit
    # filename rather than a relaxation of the allowlist above, so
    # "/top50_ticker_data/" stays a 404 instead of becoming a directory
    # listing, and "/" cannot fall through to SimpleHTTPRequestHandler's own
    # index-or-listing behaviour.
    ROOT_DOCUMENT = "/index.html"

    @classmethod
    def _is_servable_static_path(cls, path):
        segments = [s for s in urllib.parse.unquote(path).split("/") if s]
        if not segments or any(segment.startswith(".") for segment in segments):
            return False
        return Path(segments[-1]).suffix.lower() in cls._ALLOWED_STATIC_SUFFIXES

    def _handle_static(self, super_method):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/":
            path = self.ROOT_DOCUMENT
            self.path = parsed._replace(path=path).geturl()
        if not self._is_servable_static_path(path):
            self.send_error(404, "File not found")
            return
        super_method()

    def do_HEAD(self):
        parsed = urllib.parse.urlsplit(self.path)
        if not parsed.path.startswith("/api/"):
            return self._handle_static(super().do_HEAD)
        # No API support for HEAD — treat it like an unknown path.
        self.send_error(404, "File not found")

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if not parsed.path.startswith("/api/"):
            return self._handle_static(super().do_GET)
        try:
            status, payload = handle_api(
                parsed.path, urllib.parse.parse_qs(parsed.query),
                self.analyzer, self.screener)
        except Exception as error:
            print(f"ERROR: unhandled exception in {parsed.path}: {error}")
            status, payload = 500, {"error": str(error)}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    # Patches the process-wide ssl module to use the OS's own certificate
    # store instead of Python's bundled one — the same fix download_sp500.py
    # already needed for outbound HTTPS on this platform. Must run before
    # any HTTPS request (FMP, Alpha Vantage, yfinance, Firestore).
    truststore.inject_into_ssl()

    parser = argparse.ArgumentParser(
        description="Stock analyzer server: static files + analysis API.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--bind", default="127.0.0.1",
                        help="interface to bind (default: 127.0.0.1, "
                             "loopback-only; use 0.0.0.0 to expose on the "
                             "LAN — this spends the operator's API quota "
                             "and writes to the shared Firestore project, "
                             "so only opt in deliberately)")
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
    Handler.screener = Screener()

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"Serving http://{args.bind}:{args.port}/ (toolkit shell) "
          f"(bind: {args.bind}, store: {store.kind}, technicals: "
          f"{'local' if args.local_technicals else 'alpha vantage'})")
    server.serve_forever()


if __name__ == "__main__":
    main()
