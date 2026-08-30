import re
import subprocess
import unittest
from pathlib import Path


HTML_PATH = Path(__file__).with_name("sp500_simulator.html")


class SimulatorHtmlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML_PATH.read_text()

    def test_loads_downloaded_csv_files(self):
        self.assertIn("sp500_monthly_prices_10yr.csv", self.html)
        self.assertIn("sp500_top50_rankings.csv", self.html)
        self.assertIn("Promise.all", self.html)
        self.assertIn("fetch(", self.html)

    def test_top_stock_control_supports_one_to_fifty_and_defaults_to_ten(self):
        control = re.search(r'<input[^>]+id="topY"[^>]*>', self.html).group(0)
        self.assertIn('min="1"', control)
        self.assertIn('max="50"', control)
        self.assertIn('value="10"', control)
        self.assertIn('<span id="topYVal">10</span>', self.html)

    def test_has_loading_and_actionable_error_states(self):
        self.assertIn('id="dataStatus"', self.html)
        self.assertIn("python3 -m http.server 8000", self.html)
        self.assertIn("window.location.protocol === 'file:'", self.html)

    def test_uses_selected_ranking_mode_for_stock_universe(self):
        self.assertIn("RANKING_BY_MODE", self.html)
        self.assertIn("rankings[criterion]", self.html)
        self.assertIn(".slice(0, topY)", self.html)

    def test_builds_equal_weight_benchmark_from_constituent_returns(self):
        self.assertIn("buildEqualWeightBenchmark", self.html)
        self.assertIn("validReturns.reduce", self.html)
        self.assertIn("validReturns.length", self.html)

    def test_legacy_embedded_datasets_are_removed(self):
        self.assertNotIn("const REAL_INDEX", self.html)
        self.assertNotIn("const REAL_STOCKS", self.html)
        self.assertNotIn("MAR 2013", self.html)

    def test_backtest_window_is_configured_from_loaded_data(self):
        self.assertIn("backtestMonths.max", self.html)
        self.assertIn("DATA.months.length", self.html)

    def test_optimizer_controls_and_results_are_available(self):
        self.assertIn('id="runOptimizer"', self.html)
        self.assertIn('id="optimizerProgress"', self.html)
        for objective in ("maxGain", "minVolatility", "minTrades"):
            self.assertIn(f'id="optimizer-{objective}"', self.html)
            self.assertIn(f'data-objective="{objective}"', self.html)
        self.assertNotIn('id="optimizer-maxCagr"', self.html)
        self.assertIn('data-field="benchmarkLead"', self.html)
        self.assertIn("No searched rule outperformed monthly investing.", self.html)

    def test_fee_tax_and_period_holdings_ui(self):
        for element_id in (
            "dcaFees",
            "lsFees",
            "lsNetInvested",
            "rbFees",
            "rbTaxes",
            "rbHoldings",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("bold = held at period end", self.html)

    def test_optimizer_is_an_accessible_collapsed_disclosure(self):
        toggle = re.search(
            r'<button[^>]+id="optimizerToggle"[^>]*>', self.html
        ).group(0)
        self.assertIn('aria-expanded="false"', toggle)
        self.assertIn('aria-controls="optimizerBody"', toggle)
        body = re.search(
            r'<div[^>]+id="optimizerBody"[^>]*>', self.html
        ).group(0)
        self.assertIn("hidden", body)
        self.assertIn("Show optimizer", self.html)
        self.assertIn("Hide optimizer", self.html)

    def test_optimizer_behaviors(self):
        script = re.findall(
            r"<script(?:\\s[^>]*)?>(.*?)</script>", self.html, re.S
        )[-1]
        production = script.split(
            "['monthly','backtestMonths'", 1
        )[0]
        harness = r"""
const assert = require('assert');

const benchmark = simulateBenchmark(3, 100, 1, 2);
assert.equal(benchmark.dcaFees, 6);
assert.equal(benchmark.lumpFee, 3);
assert.equal(benchmark.lumpNetInvested, 297);

const profitableSale = settleSale(10, 15, 100, 5, 0.25);
assert.equal(profitableSale.tax, 11.25);
assert.equal(profitableSale.cashReceived, 133.75);
const losingSale = settleSale(10, 8, 100, 5, 0.25);
assert.equal(losingSale.tax, 0);
assert.equal(losingSale.cashReceived, 75);

const history = createHoldingHistory();
recordHolding(history, 'MSFT');
recordHolding(history, 'AAPL');
recordHolding(history, 'MSFT');
assert.deepEqual(history, ['MSFT', 'AAPL']);

const candidates = [
  {topY: 5, dipPct: 8, gainPct: 20, rebuyMonths: 2},
  {topY: 10, dipPct: 10, gainPct: 30, rebuyMonths: 4},
  {topY: 15, dipPct: 12, gainPct: 40, rebuyMonths: 6}
];
const results = [
  {finalValue: 1400, invested: 1000, cagr: 8, trades: 20, volatility: 0.12},
  {finalValue: 1600, invested: 1000, cagr: 12, trades: 25, volatility: 0.08},
  {finalValue: 1500, invested: 1000, cagr: 10, trades: 20, volatility: 0.08}
];
let index = 0;
const winners = evaluateOptimizerCandidates(candidates, () => results[index++], 1450);
assert.equal(winners.maxGain.parameters.topY, 10);
assert.equal(winners.minVolatility.parameters.topY, 10);
assert.equal(winners.minTrades.parameters.topY, 15);
assert.equal(winners.minVolatility.beatsMonthlyBy, 150);

index = 0;
const noQualifier = evaluateOptimizerCandidates(candidates, () => results[index++], 2000);
assert.equal(noQualifier.minVolatility, null);
assert.equal(noQualifier.maxGain.parameters.topY, 10);
assert.equal(noQualifier.minTrades.parameters.topY, 15);

const volatility = contributionAdjustedVolatility([100, 210, 305], 100);
assert(Math.abs(volatility - 0.2144443857) < 1e-9);

const refined = buildRefinementCandidates({
  topY: 10, dipPct: 10, gainPct: 20, rebuyMonths: 3
});
assert(refined.some(item => item.topY === 9));
assert(refined.some(item => item.topY === 11));
assert(refined.every(item => item.topY >= 1 && item.topY <= 50));
assert(refined.every(item => item.dipPct >= 3 && item.dipPct <= 25));
assert(refined.every(item => item.gainPct >= 5 && item.gainPct <= 60));
assert(refined.every(item => item.rebuyMonths >= 0 && item.rebuyMonths <= 12));

const objectiveSeeds = collectObjectiveSeeds(winners);
assert.equal(objectiveSeeds.length, 2);
console.log('optimizer behavior OK');
"""
        result = subprocess.run(
            ["node", "-e", production + harness],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("optimizer behavior OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
