"""Pure value-grade scoring over trailing-year fundamentals. No I/O."""

GRADE_BANDS = [(0.9, "A"), (0.7, "B"), (0.5, "C"), (0.3, "D")]


def _compare(value, result_if):
    if value is None:
        return "neutral", None
    return ("pass" if result_if(value) else "fail"), value


def _net_margin(metrics):
    margin = metrics.get("netMargin")
    year_ago = metrics.get("netMarginYearAgo")
    if margin is None or year_ago is None:
        return "neutral", margin
    return ("pass" if margin > 0 and margin >= year_ago else "fail"), margin


CHECKS = [
    ("revenue_growth", "Revenue growth (TTM YoY)", "> 0",
     lambda m: _compare(m.get("revenueGrowth"), lambda v: v > 0)),
    ("net_margin", "Net margin", "positive and >= year-ago", _net_margin),
    ("eps_growth", "EPS growth (TTM YoY)", "> 0",
     lambda m: _compare(m.get("epsGrowth"), lambda v: v > 0)),
    ("roe", "Return on equity", "> 10%",
     lambda m: _compare(m.get("roe"), lambda v: v > 0.10)),
    ("fcf", "Free cash flow (TTM)", "> 0",
     lambda m: _compare(m.get("fcfTTM"), lambda v: v > 0)),
    ("debt_equity", "Debt / equity", "< 1.5",
     lambda m: _compare(m.get("debtToEquity"), lambda v: v < 1.5)),
    ("current_ratio", "Current ratio", "> 1",
     lambda m: _compare(m.get("currentRatio"), lambda v: v > 1)),
    ("pe", "P/E (trailing)", "0 < P/E < 30",
     lambda m: _compare(m.get("peTTM"), lambda v: 0 < v < 30)),
    ("peg", "PEG", "0 < PEG < 2",
     lambda m: _compare(m.get("peg"), lambda v: 0 < v < 2)),
    ("fcf_yield", "FCF yield", "> 3%",
     lambda m: _compare(m.get("fcfYield"), lambda v: v > 0.03)),
]


def compute_verdict(metrics):
    checks = []
    for check_id, label, threshold, evaluate in CHECKS:
        result, value = evaluate(metrics or {})
        checks.append({"id": check_id, "label": label, "value": value,
                       "threshold": threshold, "result": result})

    evaluated = [c for c in checks if c["result"] != "neutral"]
    passes = [c for c in checks if c["result"] == "pass"]
    fails = [c for c in checks if c["result"] == "fail"]

    if not evaluated:
        return {"grade": "N/A", "passes": 0, "evaluated": 0,
                "summary": "Insufficient data to grade this ticker.",
                "checks": checks}

    ratio = len(passes) / len(evaluated)
    grade = next((g for floor, g in GRADE_BANDS if ratio >= floor), "F")

    strongest = ", ".join(c["label"] for c in passes[:2]) or "none"
    weakest = ", ".join(c["label"] for c in fails[:2])
    summary = (f"Grade {grade}: {len(passes)} of {len(evaluated)} evaluated "
               f"checks pass. Strongest: {strongest}."
               + (f" Weakest: {weakest}." if weakest else " No failed checks."))
    return {"grade": grade, "passes": len(passes),
            "evaluated": len(evaluated), "summary": summary, "checks": checks}
