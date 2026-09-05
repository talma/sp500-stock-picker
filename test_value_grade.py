# test_value_grade.py
import pytest

import value_grade


def good_metrics(**overrides):
    metrics = {
        "revenueGrowth": 0.12,
        "netMargin": 0.25,
        "netMarginYearAgo": 0.22,
        "epsGrowth": 0.10,
        "roe": 0.30,
        "fcfTTM": 90e9,
        "debtToEquity": 1.2,
        "currentRatio": 1.4,
        "peTTM": 22.0,
        "peg": 1.5,
        "fcfYield": 0.045,
    }
    metrics.update(overrides)
    return metrics


# (metric override, failing value, check id) for every check
FAILING = [
    ("revenueGrowth", -0.05, "revenue_growth"),
    ("netMargin", -0.01, "net_margin"),
    ("epsGrowth", -0.02, "eps_growth"),
    ("roe", 0.05, "roe"),
    ("fcfTTM", -1e9, "fcf"),
    ("debtToEquity", 2.0, "debt_equity"),
    ("currentRatio", 0.8, "current_ratio"),
    ("peTTM", 45.0, "pe"),
    ("peg", 3.0, "peg"),
    ("fcfYield", 0.01, "fcf_yield"),
]


def check_by_id(verdict, check_id):
    return next(c for c in verdict["checks"] if c["id"] == check_id)


def test_all_pass_is_grade_a():
    verdict = value_grade.compute_verdict(good_metrics())
    assert verdict["grade"] == "A"
    assert verdict["passes"] == 10
    assert verdict["evaluated"] == 10
    assert all(c["result"] == "pass" for c in verdict["checks"])


@pytest.mark.parametrize("key,bad,check_id", FAILING)
def test_each_check_fails_on_bad_value(key, bad, check_id):
    verdict = value_grade.compute_verdict(good_metrics(**{key: bad}))
    assert check_by_id(verdict, check_id)["result"] == "fail"


def test_missing_value_is_neutral_and_excluded():
    verdict = value_grade.compute_verdict(good_metrics(peg=None))
    assert check_by_id(verdict, "peg")["result"] == "neutral"
    assert verdict["evaluated"] == 9
    assert verdict["passes"] == 9
    assert verdict["grade"] == "A"  # 9/9


def test_net_margin_needs_year_ago_value():
    verdict = value_grade.compute_verdict(good_metrics(netMarginYearAgo=None))
    assert check_by_id(verdict, "net_margin")["result"] == "neutral"


def test_positive_margin_below_year_ago_fails():
    verdict = value_grade.compute_verdict(
        good_metrics(netMargin=0.10, netMarginYearAgo=0.20)
    )
    assert check_by_id(verdict, "net_margin")["result"] == "fail"


def test_negative_pe_fails_not_passes():
    verdict = value_grade.compute_verdict(good_metrics(peTTM=-12.0))
    assert check_by_id(verdict, "pe")["result"] == "fail"


def fail_n(n):
    """good_metrics with the first n checks failing."""
    overrides = {key: bad for key, bad, _ in FAILING[:n]}
    return value_grade.compute_verdict(good_metrics(**overrides))


@pytest.mark.parametrize("fails,grade", [
    (0, "A"), (1, "A"),          # 10/10, 9/10 = 0.9
    (3, "B"),                    # 7/10
    (5, "C"),                    # 5/10
    (7, "D"),                    # 3/10
    (8, "F"),                    # 2/10
])
def test_grade_boundaries(fails, grade):
    assert fail_n(fails)["grade"] == grade


def test_all_neutral_is_not_applicable():
    verdict = value_grade.compute_verdict({})
    assert verdict["grade"] == "N/A"
    assert verdict["evaluated"] == 0
    assert "insufficient" in verdict["summary"].lower()


def only_checks(*keys):
    """Metrics with data for `keys` only — every other check goes neutral."""
    good = good_metrics()
    metrics = {k: good[k] for k in keys}
    if "netMargin" in keys:
        metrics["netMarginYearAgo"] = good["netMarginYearAgo"]
    return value_grade.compute_verdict(metrics)


def test_one_evaluated_check_is_not_graded():
    """The QQQ bug: an ETF left only the trailing-P/E check with data, so the
    grade was passes/1 — either 0.0 (F) or 1.0 (A) — and flipped F -> A when
    that one P/E was revised 30.63 -> 29.30 across the "< 30" threshold."""
    failing = value_grade.compute_verdict({"peTTM": 30.634703})
    passing = value_grade.compute_verdict({"peTTM": 29.303808})
    assert failing["evaluated"] == passing["evaluated"] == 1
    assert failing["grade"] == passing["grade"] == "N/A"


def test_ungraded_verdict_still_reports_its_checks_and_counts():
    verdict = value_grade.compute_verdict({"peTTM": 22.0, "roe": 0.30})
    assert verdict["passes"] == 2 and verdict["evaluated"] == 2
    assert len(verdict["checks"]) == len(value_grade.CHECKS)
    assert check_by_id(verdict, "pe")["result"] == "pass"


@pytest.mark.parametrize("count,graded", [
    (value_grade.MIN_EVALUATED - 1, False),
    (value_grade.MIN_EVALUATED, True),
])
def test_grade_floor_is_the_boundary(count, graded):
    keys = [key for key, _, _ in FAILING][:count]
    verdict = only_checks(*keys)
    assert verdict["evaluated"] == count
    assert (verdict["grade"] != "N/A") is graded


def test_thin_coverage_summary_names_the_shortfall():
    verdict = value_grade.compute_verdict({"peTTM": 22.0})
    assert str(value_grade.MIN_EVALUATED) in verdict["summary"]
    assert "1 of 10" in verdict["summary"]


def test_summary_names_grade_and_weakest_check():
    verdict = value_grade.compute_verdict(good_metrics(peg=3.0))
    assert verdict["grade"] in "ABCDF"
    assert verdict["grade"] in verdict["summary"]
    assert "PEG" in verdict["summary"]
