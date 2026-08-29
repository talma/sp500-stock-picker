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
