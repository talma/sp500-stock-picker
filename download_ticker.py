import argparse
import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from download_sp500 import fetch_sp500_tables


def normalize_ticker(ticker):
    return ticker.strip().upper().replace(".", "-")


def validate_ticker(ticker, constituents):
    ticker = normalize_ticker(ticker)
    normalized_constituents = {normalize_ticker(value) for value in constituents}
    if ticker not in normalized_constituents:
        raise ValueError(f"{ticker} is not a current S&P 500 constituent")
    return ticker


def current_sp500_tickers():
    table = fetch_sp500_tables()[0]
    return table["Symbol"].map(normalize_ticker).tolist()


def download_ticker(ticker, output_directory="."):
    ticker = normalize_ticker(ticker)
    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=10 * 365)
    data = yf.download(
        ticker,
        start=start_date,
        end=end_date,
        interval="1mo",
        auto_adjust=False,
        progress=False,
    )
    if data.empty:
        raise RuntimeError(f"No data returned for {ticker}")

    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    columns = [
        column
        for column in ("Open", "High", "Low", "Close", "Adj Close", "Volume")
        if column in data.columns
    ]
    output = Path(output_directory) / f"{ticker}_monthly_10yr.csv"
    data[columns].rename_axis("Date").to_csv(output)
    return output


def main():
    parser = argparse.ArgumentParser(
        description="Download 10 years of monthly OHLCV data for a current S&P 500 ticker."
    )
    parser.add_argument("ticker", help="Ticker symbol, for example AAPL or BRK.B")
    args = parser.parse_args()

    try:
        ticker = validate_ticker(args.ticker, current_sp500_tickers())
        output = download_ticker(ticker)
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(f"Success! Data saved to '{output}'.")


if __name__ == "__main__":
    main()
