import tempfile
import unittest
from pathlib import Path

import pandas as pd

import download_ranked_tickers


class RankedTickerDownloadTest(unittest.TestCase):
    def write_rankings(self, directory):
        path = Path(directory) / "rankings.csv"
        pd.DataFrame(
            {
                "criterion": [
                    "market_cap",
                    "market_cap",
                    "average_daily_volume",
                    "one_year_return",
                ],
                "rank": [1, 2, 1, 1],
                "ticker": ["MSFT", "AAPL", "AAPL", "NVDA"],
                "value": [3, 2, 10, 0.5],
            }
        ).to_csv(path, index=False)
        return path

    def test_reads_unique_tickers_across_all_rankings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_rankings(directory)

            tickers = download_ranked_tickers.read_ranked_tickers(path)

        self.assertEqual(tickers, ["MSFT", "AAPL", "NVDA"])

    def test_filters_tickers_by_criterion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_rankings(directory)

            tickers = download_ranked_tickers.read_ranked_tickers(
                path, "market_cap"
            )

        self.assertEqual(tickers, ["MSFT", "AAPL"])

    def test_rejects_csv_without_required_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            pd.DataFrame({"symbol": ["AAPL"]}).to_csv(path, index=False)

            with self.assertRaisesRegex(ValueError, "criterion.*ticker"):
                download_ranked_tickers.read_ranked_tickers(path)

    def test_creates_output_directory_and_continues_after_failure(self):
        calls = []
        messages = []

        def fake_download(ticker, output_directory):
            calls.append(ticker)
            if ticker == "AAPL":
                raise RuntimeError("unavailable")
            return Path(output_directory) / f"{ticker}.csv"

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "downloads"
            failures = download_ranked_tickers.download_ranked_tickers(
                ["MSFT", "AAPL", "NVDA"],
                output,
                downloader=fake_download,
                progress_callback=messages.append,
            )

            self.assertTrue(output.is_dir())

        self.assertEqual(calls, ["MSFT", "AAPL", "NVDA"])
        self.assertEqual(failures, [("AAPL", "unavailable")])
        self.assertEqual(messages[0], "[1/3] Downloading MSFT...")
        self.assertEqual(messages[-1], "[3/3] Saved NVDA.csv")


if __name__ == "__main__":
    unittest.main()
