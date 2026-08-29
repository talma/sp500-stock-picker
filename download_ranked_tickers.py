import argparse
from pathlib import Path

import pandas as pd

from download_ticker import download_ticker


CRITERIA = ("market_cap", "average_daily_volume", "one_year_return")


def read_ranked_tickers(input_file, criterion=None):
    rankings = pd.read_csv(input_file)
    required = {"criterion", "ticker"}
    missing = required - set(rankings.columns)
    if missing:
        raise ValueError(
            "Ranking CSV must contain criterion and ticker columns "
            f"(missing: {', '.join(sorted(missing))})"
        )
    if criterion:
        rankings = rankings[rankings["criterion"] == criterion]
    return rankings["ticker"].dropna().astype(str).str.strip().drop_duplicates().tolist()


def download_ranked_tickers(
    tickers,
    output_directory,
    downloader=download_ticker,
    progress_callback=print,
):
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    failures = []
    total = len(tickers)
    for number, ticker in enumerate(tickers, 1):
        progress_callback(f"[{number}/{total}] Downloading {ticker}...")
        try:
            output = downloader(ticker, output_directory)
            progress_callback(f"[{number}/{total}] Saved {Path(output).name}")
        except Exception as error:
            failures.append((ticker, str(error)))
            progress_callback(f"[{number}/{total}] Failed {ticker}: {error}")
    return failures


def main():
    parser = argparse.ArgumentParser(
        description="Download monthly data for tickers in an S&P 500 ranking CSV."
    )
    parser.add_argument(
        "--input",
        default="sp500_top50_rankings.csv",
        help="ranking CSV (default: %(default)s)",
    )
    parser.add_argument(
        "--output-dir",
        default="top50_ticker_data",
        help="download directory (default: %(default)s)",
    )
    parser.add_argument(
        "--criterion",
        choices=CRITERIA,
        help="download only one ranking; omit to download their union",
    )
    args = parser.parse_args()

    try:
        tickers = read_ranked_tickers(args.input, args.criterion)
    except (FileNotFoundError, pd.errors.EmptyDataError, ValueError) as error:
        parser.error(str(error))
    if not tickers:
        parser.error("No tickers matched the requested ranking")

    print(f"Downloading {len(tickers)} unique tickers to '{args.output_dir}'...")
    failures = download_ranked_tickers(tickers, args.output_dir)
    succeeded = len(tickers) - len(failures)
    print(f"\nCompleted: {succeeded}/{len(tickers)} downloaded.")
    if failures:
        print("Failed tickers:")
        for ticker, error in failures:
            print(f"  {ticker}: {error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
