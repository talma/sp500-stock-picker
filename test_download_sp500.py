import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import download_sp500


class FetchSp500TickersTest(unittest.TestCase):
    @patch("download_sp500.pd.read_html")
    def test_fetches_wikipedia_with_browser_user_agent(self, read_html):
        read_html.return_value = []

        download_sp500.fetch_sp500_tables()

        read_html.assert_called_once_with(
            download_sp500.SP500_URL,
            storage_options={"User-Agent": download_sp500.USER_AGENT},
        )


class RankingTest(unittest.TestCase):
    def test_ranks_each_metric_descending_and_drops_missing_values(self):
        metrics = pd.DataFrame(
            {
                "market_cap": {"AAA": 10, "BBB": 30, "CCC": None},
                "average_daily_volume": {"AAA": 300, "BBB": 100, "CCC": 200},
                "one_year_return": {"AAA": 0.1, "BBB": None, "CCC": 0.4},
            }
        )

        rankings = download_sp500.build_rankings(metrics, limit=2)

        self.assertEqual(rankings["market_cap"]["ticker"].tolist(), ["BBB", "AAA"])
        self.assertEqual(
            rankings["average_daily_volume"]["ticker"].tolist(), ["AAA", "CCC"]
        )
        self.assertEqual(rankings["one_year_return"]["ticker"].tolist(), ["CCC", "AAA"])
        self.assertEqual(rankings["market_cap"]["rank"].tolist(), [1, 2])

    def test_calculates_volume_and_one_year_return_from_daily_data(self):
        dates = pd.to_datetime(["2025-01-02", "2025-12-31"])
        columns = pd.MultiIndex.from_product(
            [["Close", "Volume"], ["AAA", "BBB"]]
        )
        daily = pd.DataFrame(
            [[10.0, 20.0, 100.0, 400.0], [15.0, 10.0, 300.0, 200.0]],
            index=dates,
            columns=columns,
        )

        metrics = download_sp500.calculate_historical_metrics(daily, ["AAA", "BBB"])

        self.assertEqual(metrics.loc["AAA", "average_daily_volume"], 200.0)
        self.assertAlmostEqual(metrics.loc["AAA", "one_year_return"], 0.5)
        self.assertAlmostEqual(metrics.loc["BBB", "one_year_return"], -0.5)

    def test_market_cap_lookup_reports_periodic_progress(self):
        messages = []

        class FakeTicker:
            def __init__(self, ticker):
                self.fast_info = {"marketCap": 1}

        download_sp500.fetch_market_caps(
            [f"T{i}" for i in range(5)],
            ticker_factory=FakeTicker,
            max_workers=2,
            progress_every=2,
            progress_callback=messages.append,
        )

        self.assertEqual(
            messages,
            [
                "Market caps: 2/5 processed",
                "Market caps: 4/5 processed",
                "Market caps: 5/5 processed",
            ],
        )

    def test_fetches_market_caps_and_keeps_failures_missing(self):
        class FakeTicker:
            def __init__(self, ticker):
                if ticker == "BAD":
                    raise RuntimeError("unavailable")
                self.fast_info = {"marketCap": {"AAA": 10, "BBB": 20}[ticker]}

        result = download_sp500.fetch_market_caps(
            ["AAA", "BAD", "BBB"], ticker_factory=FakeTicker
        )

        self.assertEqual(result.to_dict(), {"AAA": 10.0, "BBB": 20.0})

    def test_combines_rankings_with_criterion_column(self):
        rankings = {
            "market_cap": pd.DataFrame(
                {"rank": [1], "ticker": ["AAA"], "value": [10.0]}
            ),
            "one_year_return": pd.DataFrame(
                {"rank": [1], "ticker": ["BBB"], "value": [0.5]}
            ),
        }

        result = download_sp500.combine_rankings(rankings)

        self.assertEqual(
            result.columns.tolist(), ["criterion", "rank", "ticker", "value"]
        )
        self.assertEqual(result["criterion"].tolist(), ["market_cap", "one_year_return"])

    def test_writes_rankings_and_three_monthly_subsets(self):
        prices = pd.DataFrame({"AAA": [1], "BBB": [2]})
        rankings = {
            criterion: pd.DataFrame(
                {"rank": [1], "ticker": [ticker], "value": [1.0]}
            )
            for criterion, ticker in (
                ("market_cap", "AAA"),
                ("average_daily_volume", "BBB"),
                ("one_year_return", "AAA"),
            )
        }

        with tempfile.TemporaryDirectory() as directory:
            download_sp500.write_ranking_outputs(prices, rankings, directory)
            names = {path.name for path in Path(directory).iterdir()}

        self.assertEqual(
            names,
            {
                "sp500_top50_rankings.csv",
                "sp500_top50_market_cap_monthly.csv",
                "sp500_top50_volume_monthly.csv",
                "sp500_top50_performance_monthly.csv",
            },
        )

    def test_subsets_monthly_prices_in_ranking_order(self):
        prices = pd.DataFrame({"AAA": [1], "BBB": [2], "CCC": [3]})
        ranking = pd.DataFrame({"ticker": ["CCC", "AAA", "MISSING"]})

        result = download_sp500.subset_monthly_prices(prices, ranking)

        self.assertEqual(result.columns.tolist(), ["CCC", "AAA"])


if __name__ == "__main__":
    unittest.main()
