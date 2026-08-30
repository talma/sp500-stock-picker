# analyze_server.py
"""Local analysis server: static files + /api/analyze|analyzed|history.
Run: python3 analyze_server.py [--port 8000] [--local-technicals]"""
import argparse
import collections
import datetime
import json
import os
import threading
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import truststore

import analysis_sources
import firestore_store
import value_grade
from firestore_store import _now_iso


def load_env(path=".env"):
    values = {}
    env_path = Path(path)
    if not env_path.exists():
        return values
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
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


def handle_api(path, query, analyzer):
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

    return 404, {"error": f"Unknown API path: {path}"}


class Handler(SimpleHTTPRequestHandler):
    analyzer = None    # set in main()

    # Only these extensions are ever servable as static files. An allowlist
    # keeps "a new file appears in the repo root" default to *not served* —
    # unlike a blocklist (which must be remembered and updated by hand
    # every time a new secret-bearing file, like a second credentials
    # file, is added), this also closes off Python source, directory
    # listings, and anything else with no reason to be browsable.
    _ALLOWED_STATIC_SUFFIXES = {".html", ".htm", ".css", ".js", ".csv",
                                ".md", ".txt"}

    @classmethod
    def _is_servable_static_path(cls, path):
        segments = [s for s in urllib.parse.unquote(path).split("/") if s]
        if not segments or any(segment.startswith(".") for segment in segments):
            return False
        return Path(segments[-1]).suffix.lower() in cls._ALLOWED_STATIC_SUFFIXES

    def _handle_static(self, super_method):
        parsed = urllib.parse.urlsplit(self.path)
        if not self._is_servable_static_path(parsed.path):
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
                self.analyzer)
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

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"Serving http://{args.bind}:{args.port}/stock_analyzer.html "
          f"(bind: {args.bind}, store: {store.kind}, technicals: "
          f"{'local' if args.local_technicals else 'alpha vantage'})")
    server.serve_forever()


if __name__ == "__main__":
    main()
