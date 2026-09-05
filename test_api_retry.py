"""Behaviour of the pages' api() helper on a scale-to-zero deploy.

The retry only means anything against a real fetch, so this extracts the
production JavaScript out of the pages and drives it under node — the same
approach test_simulator.py uses for the optimizer. Re-implementing the logic
in Python would test the copy instead of what ships.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest


PAGES = ["screener.html", "stock_analyzer.html"]
_END = "  return payload;\n}"


def _api_source(name, delay_ms):
    """The api() helper and the two functions it delegates to.

    The 2000ms production delay is swapped for a negligible one so the suite
    stays fast; test_api_retry_delay_is_a_sane_production_value covers the
    shipped constant itself.
    """
    html = Path(__file__).with_name(name).read_text()
    start = html.index("const API_RETRY_DELAY_MS")
    end = html.index(_END, start) + len(_END)
    source = html[start:end]
    return source.replace("const API_RETRY_DELAY_MS = 2000;",
                          f"const API_RETRY_DELAY_MS = {delay_ms};")


def _run(name, hostname, fetch_js, delay_ms=1):
    """Calls api("/api/screen") with a stubbed fetch, and reports back the
    number of fetch attempts plus either the payload or the error message."""
    script = f"""
globalThis.location = {{protocol: "https:",
                        hostname: {json.dumps(hostname)}}};
globalThis.calls = 0;
globalThis.fetch = {fetch_js};
{_api_source(name, delay_ms)}
api("/api/screen?x=1").then(
  payload => console.log(JSON.stringify(
      {{calls: globalThis.calls, payload}})),
  error => console.log(JSON.stringify(
      {{calls: globalThis.calls, message: error.message}})));
"""
    result = subprocess.run(["node", "-e", script], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


_OK = """async () => { globalThis.calls++;
  return {ok: true, status: 200, json: async () => ({results: ["NVDA"]})}; }"""
_FLAKY = """async () => { globalThis.calls++;
  if (globalThis.calls === 1) throw new TypeError("network");
  return {ok: true, status: 200, json: async () => ({results: ["NVDA"]})}; }"""
_DEAD = """async () => { globalThis.calls++;
  throw new TypeError("network"); }"""
_HTTP_500 = """async () => { globalThis.calls++;
  return {ok: false, status: 500, json: async () => ({error: "boom"})}; }"""


@pytest.mark.parametrize("page", PAGES)
def test_a_healthy_request_is_not_retried(page):
    outcome = _run(page, "sp500-toolkit.fly.dev", _OK)
    assert outcome["calls"] == 1
    assert outcome["payload"]["results"] == ["NVDA"]


@pytest.mark.parametrize("page", PAGES)
def test_a_dropped_connection_is_retried_and_recovers(page):
    """The symptom this exists for: a request landing during a cold start can
    have its connection cut, which rejects the fetch even though the machine
    is coming up fine. The second attempt meets a warm server."""
    outcome = _run(page, "sp500-toolkit.fly.dev", _FLAKY)
    assert outcome["calls"] == 2
    assert outcome["payload"]["results"] == ["NVDA"]
    assert "message" not in outcome


@pytest.mark.parametrize("page", PAGES)
def test_a_hosted_origin_does_not_blame_analyze_server_py(page):
    """On a deployed origin the server is not the reader's to start, so the
    old wording sent them chasing the wrong problem."""
    outcome = _run(page, "sp500-toolkit.fly.dev", _DEAD)
    assert outcome["calls"] == 2
    assert "waking up" in outcome["message"]
    assert "analyze_server.py" not in outcome["message"]


@pytest.mark.parametrize("page", PAGES)
def test_localhost_keeps_the_actionable_local_advice(page):
    outcome = _run(page, "localhost", _DEAD)
    assert outcome["calls"] == 2
    assert "analyze_server.py" in outcome["message"]


@pytest.mark.parametrize("page", PAGES)
def test_an_http_error_is_neither_retried_nor_masked(page):
    """Retrying is only ever right for a rejected fetch. A 500 is a real
    answer from a reachable server: sending it twice would double any side
    effect, and hiding its message behind "cannot reach" would lose the
    diagnosis."""
    outcome = _run(page, "sp500-toolkit.fly.dev", _HTTP_500)
    assert outcome["calls"] == 1
    assert outcome["message"] == "boom"


@pytest.mark.parametrize("page", PAGES)
def test_api_retry_delay_is_a_sane_production_value(page):
    """Guards the constant the other tests substitute away."""
    html = Path(__file__).with_name(page).read_text()
    delay = int(re.search(r"const API_RETRY_DELAY_MS = (\d+);", html).group(1))
    assert 500 <= delay <= 5000


@pytest.mark.parametrize("page", PAGES)
def test_file_protocol_still_short_circuits_before_fetching(page):
    """The file:// guard predates the retry and must keep winning: retrying a
    URL the browser will never allow just delays a fixed, explainable error.

    Scoped to the body of api(), since fetchRetryingOnce is *declared* above
    the guard and only *called* below it."""
    html = Path(__file__).with_name(page).read_text()
    body = html[html.index("async function api(path) {"):]
    assert (body.index('location.protocol === "file:"')
            < body.index("await fetchRetryingOnce(path)"))
