"""Firestore persistence with an in-memory fallback. All timestamps are
ISO-8601 UTC strings so docs stay JSON-serializable and comparable."""
import copy
import datetime
import threading


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="microseconds")


def _registry_update(existing, ticker, name, sector, date, summary):
    doc = existing or {"ticker": ticker, "summaries": {}}
    doc["summaries"][date] = summary
    if name:
        doc["name"] = name
    if sector:
        doc["sector"] = sector
    if date >= doc.get("latestDate", ""):
        doc["latestDate"] = date
        doc["latestGrade"] = summary.get("grade")
    doc["updatedAt"] = _now_iso()
    return doc


class MemoryBackend:
    def __init__(self):
        self.daily = {}
        self.macro = {}
        self.registry = {}

    def get_daily(self, ticker, date):
        # Deep copy: callers may hold this bundle across concurrent requests,
        # and the stored object must never be aliased to a caller-mutable one.
        bundle = self.daily.get((ticker, date))
        return copy.deepcopy(bundle) if bundle is not None else None

    def put_daily(self, ticker, date, bundle):
        self.daily[(ticker, date)] = bundle

    def get_macro(self, date):
        doc = self.macro.get(date)
        return copy.deepcopy(doc) if doc is not None else None

    def put_macro(self, date, doc):
        self.macro[date] = doc

    def upsert_registry(self, ticker, name, sector, date, summary):
        self.registry[ticker] = _registry_update(
            self.registry.get(ticker), ticker, name, sector, date, summary)

    def get_registry(self, ticker):
        # Deep copy: a caller iterating/serializing the returned "summaries"
        # dict must never race a concurrent upsert_registry mutating the
        # live stored dict (RuntimeError: dictionary changed size during
        # iteration) or otherwise observe a partially-updated document.
        doc = self.registry.get(ticker)
        return copy.deepcopy(doc) if doc is not None else None

    def list_registry(self):
        rows = [{"ticker": d["ticker"], "name": d.get("name"),
                 "latestDate": d.get("latestDate"),
                 "latestGrade": d.get("latestGrade"),
                 "updatedAt": d.get("updatedAt")}
                for d in self.registry.values()]
        return sorted(rows, key=lambda r: r["updatedAt"] or "", reverse=True)


class FirestoreBackend:
    """Requires GOOGLE_APPLICATION_CREDENTIALS in the environment (set by
    analyze_server from .env). Raises on any init problem — Store catches."""

    def __init__(self, project_id):
        import firebase_admin
        from firebase_admin import firestore
        if not firebase_admin._apps:
            firebase_admin.initialize_app(options={"projectId": project_id})
        self.db = firestore.client()

    def _daily_ref(self, ticker, date):
        return (self.db.collection("tickers").document(ticker)
                .collection("daily").document(date))

    def get_daily(self, ticker, date):
        doc = self._daily_ref(ticker, date).get()
        return doc.to_dict() if doc.exists else None

    def put_daily(self, ticker, date, bundle):
        self._daily_ref(ticker, date).set(bundle)

    def get_macro(self, date):
        doc = self.db.collection("macro").document(date).get()
        return doc.to_dict() if doc.exists else None

    def put_macro(self, date, doc):
        self.db.collection("macro").document(date).set(doc)

    def upsert_registry(self, ticker, name, sector, date, summary):
        ref = self.db.collection("tickers").document(ticker)
        snapshot = ref.get()
        existing = snapshot.to_dict() if snapshot.exists else None
        ref.set(_registry_update(existing, ticker, name, sector, date,
                                 summary))

    def get_registry(self, ticker):
        doc = self.db.collection("tickers").document(ticker).get()
        return doc.to_dict() if doc.exists else None

    def list_registry(self):
        rows = []
        for snapshot in self.db.collection("tickers").stream():
            d = snapshot.to_dict() or {}
            rows.append({"ticker": d.get("ticker", snapshot.id),
                         "name": d.get("name"),
                         "latestDate": d.get("latestDate"),
                         "latestGrade": d.get("latestGrade"),
                         "updatedAt": d.get("updatedAt")})
        return sorted(rows, key=lambda r: r["updatedAt"] or "", reverse=True)


class Store:
    def __init__(self, project_id=None, backend=None):
        self._swap_lock = threading.Lock()
        if backend is not None:
            self.backend = backend
            self.kind = "memory"
            return
        try:
            self.backend = FirestoreBackend(project_id)
            self.kind = "firestore"
        except Exception as error:
            print(f"WARNING: Firestore unavailable ({error}); "
                  "using in-memory store for this process")
            self.backend = MemoryBackend()
            self.kind = "memory"

    def _safe(self, method, *args):
        try:
            return getattr(self.backend, method)(*args)
        except Exception as error:
            # The swap itself must be atomic: without this lock, two threads
            # racing the same Firestore outage can each install their own
            # fresh MemoryBackend, and the second one silently discards the
            # first's writes (lost update). Guarding the check-then-swap
            # ensures only one thread ever performs it; every other thread
            # that lands here — whether it caused the swap or arrives after
            # it — retries its own call against whatever backend is current,
            # instead of incorrectly re-raising its own original exception.
            with self._swap_lock:
                if self.kind == "firestore":
                    print(f"WARNING: Firestore error ({error}); "
                          "switching to in-memory store")
                    self.backend = MemoryBackend()
                    self.kind = "memory"
                backend = self.backend
            return getattr(backend, method)(*args)

    def get_daily(self, ticker, date):
        return self._safe("get_daily", ticker, date)

    def put_daily(self, ticker, date, bundle):
        return self._safe("put_daily", ticker, date, bundle)

    def get_macro(self, date):
        return self._safe("get_macro", date)

    def put_macro(self, date, doc):
        return self._safe("put_macro", date, doc)

    def upsert_registry(self, ticker, name, sector, date, summary):
        return self._safe("upsert_registry", ticker, name, sector, date,
                          summary)

    def get_registry(self, ticker):
        return self._safe("get_registry", ticker)

    def list_registry(self):
        return self._safe("list_registry")
