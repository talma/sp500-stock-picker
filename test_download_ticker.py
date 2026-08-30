import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import download_ticker


class DownloadTickerTest(unittest.TestCase):
    def test_normalizes_dot_and_dash_symbols(self):
        self.assertEqual(download_ticker.normalize_ticker(" brk.b "), "BRK-B")
        self.assertEqual(download_ticker.normalize_ticker("BRK-B"), "BRK-B")

    def test_rejects_ticker_outside_current_sp500(self):
        with self.assertRaisesRegex(ValueError, "not a current S&P 500 constituent"):
            download_ticker.validate_ticker("ZZZZ", ["AAPL", "MSFT"])

    @patch("download_ticker.yf.download")
    def test_downloads_monthly_ohlcv_and_writes_symbol_file(self, yf_download):
        index = pd.to_datetime(["2025-01-01", "2025-02-01"])
        yf_download.return_value = pd.DataFrame(
            {
                "Open": [10, 11],
                "High": [12, 13],
                "Low": [9, 10],
                "Close": [11, 12],
                "Adj Close": [10.5, 11.5],
                "Volume": [100, 200],
            },
            index=index,
        )

        with tempfile.TemporaryDirectory() as directory:
            output = download_ticker.download_ticker("AAPL", directory)
            saved = pd.read_csv(output)

        self.assertEqual(Path(output).name, "AAPL_monthly_10yr.csv")
        self.assertEqual(
            saved.columns.tolist(),
            ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"],
        )
        self.assertEqual(yf_download.call_args.kwargs["interval"], "1mo")

    @patch("download_ticker.yf.download", return_value=pd.DataFrame())
    def test_refuses_to_write_empty_download(self, _yf_download):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "No data returned"):
                download_ticker.download_ticker("AAPL", directory)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
