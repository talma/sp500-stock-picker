import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import truststore
import yfinance as yf


SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
USER_AGENT = "Mozilla/5.0 (compatible; sp500-data-downloader/1.0)"


def fetch_sp500_tables():
    truststore.inject_into_ssl()
    return pd.read_html(
        SP500_URL,
        storage_options={"User-Agent": USER_AGENT},
    )


def calculate_historical_metrics(daily_data, tickers):
    metrics = pd.DataFrame(index=tickers)
    for ticker in tickers:
        try:
            close = daily_data["Close"][ticker].dropna()
            volume = daily_data["Volume"][ticker].dropna()
        except (KeyError, TypeError):
            continue

        if not volume.empty:
            metrics.loc[ticker, "average_daily_volume"] = volume.mean()
        if len(close) >= 2 and close.iloc[0] != 0:
            metrics.loc[ticker, "one_year_return"] = close.iloc[-1] / close.iloc[0] - 1
    return metrics


def fetch_market_caps(
    tickers,
    ticker_factory=yf.Ticker,
    max_workers=8,
    progress_every=25,
    progress_callback=None,
):
    def fetch_one(ticker):
        market_cap = ticker_factory(ticker).fast_info.get("marketCap")
        return ticker, market_cap

    market_caps = {}
    total = len(tickers)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_one, ticker) for ticker in tickers]
        for processed, future in enumerate(as_completed(futures), 1):
            try:
                ticker, market_cap = future.result()
                if market_cap is not None:
                    market_caps[ticker] = float(market_cap)
            except Exception:
                pass
            if progress_callback and (
                processed % progress_every == 0 or processed == total
            ):
                progress_callback(f"Market caps: {processed}/{total} processed")
    return pd.Series(market_caps, dtype="float64", name="market_cap")


def build_rankings(metrics, limit=50):
    rankings = {}
    for metric in ("market_cap", "average_daily_volume", "one_year_return"):
        ranked = metrics[metric].dropna().sort_values(ascending=False).head(limit)
        rankings[metric] = pd.DataFrame(
            {
                "rank": range(1, len(ranked) + 1),
                "ticker": ranked.index,
                "value": ranked.values,
            }
        )
    return rankings


def combine_rankings(rankings):
    frames = []
    for criterion, ranking in rankings.items():
        frame = ranking.copy()
        frame.insert(0, "criterion", criterion)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def subset_monthly_prices(monthly_prices, ranking):
    tickers = [ticker for ticker in ranking["ticker"] if ticker in monthly_prices]
    return monthly_prices[tickers]


def write_ranking_outputs(monthly_prices, rankings, output_directory="."):
    output_directory = Path(output_directory)
    combine_rankings(rankings).to_csv(
        output_directory / "sp500_top50_rankings.csv", index=False
    )
    filenames = {
        "market_cap": "sp500_top50_market_cap_monthly.csv",
        "average_daily_volume": "sp500_top50_volume_monthly.csv",
        "one_year_return": "sp500_top50_performance_monthly.csv",
    }
    for criterion, filename in filenames.items():
        subset_monthly_prices(monthly_prices, rankings[criterion]).to_csv(
            output_directory / filename
        )


def download_sp500_monthly_data():
    print("Step 1: Fetching current S&P 500 ticker list from Wikipedia...")
    try:
        # Pull the live S&P 500 table from Wikipedia
        payload = fetch_sp500_tables()
        df_tickers = payload[0]
        # Clean ticker symbols (replace dots with dashes for Yahoo Finance compatibility)
        tickers = (
            df_tickers["Symbol"].str.replace(".", "-", regex=False).tolist()
        )
        print(f"Success: Found {len(tickers)} tickers.")
    except Exception as e:
        print(f"Error fetching tickers: {e}")
        return

    # Calculate date range for the last 10 years
    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=10 * 365)
    print(f"Step 2: Downloading data from {start_date} to {end_date}...")

    # Download historical data (interval='1mo' pulls monthly intervals)
    # group_by='ticker' organizes the data structure by stock symbol
    data = yf.download(
        tickers,
        start=start_date,
        end=end_date,
        interval="1mo",
        group_by="ticker",
    )

    print("Step 3: Processing and structuring the data...")
    monthly_prices = pd.DataFrame()
    for ticker in tickers:
        if ticker in data.columns.levels[0]:
            # Extract the Adjusted Close price (accounts for stock splits and dividends)
            if "Adj Close" in data[ticker].columns:
                monthly_prices[ticker] = data[ticker]["Adj Close"]
            elif "Close" in data[ticker].columns:
                monthly_prices[ticker] = data[ticker]["Close"]

    # Clean up index formatting (keep only the Year-Month-Day date)
    monthly_prices.index = monthly_prices.index.date

    # Drop rows that are completely empty
    monthly_prices.dropna(how="all", inplace=True)
    # Drop tickers that came back with no data at all (delisted/renamed/etc.)
    monthly_prices.dropna(axis=1, how="all", inplace=True)

    # Save output to a CSV file
    output_filename = "sp500_monthly_prices_10yr.csv"
    monthly_prices.to_csv(output_filename)
    print(
        f"Success! Data saved to '{output_filename}' ({len(monthly_prices)} months recorded, "
        f"{len(monthly_prices.columns)} tickers)."
    )

    print("Step 4: Calculating top-50 rankings...")
    one_year_start = end_date - datetime.timedelta(days=365)
    daily_data = yf.download(
        tickers,
        start=one_year_start,
        end=end_date,
        interval="1d",
        group_by="column",
        auto_adjust=False,
        progress=False,
    )
    metrics = calculate_historical_metrics(daily_data, tickers)
    print("Fetching current market capitalizations...")
    metrics["market_cap"] = fetch_market_caps(
        tickers, progress_callback=lambda message: print(message, flush=True)
    )
    rankings = build_rankings(metrics)
    write_ranking_outputs(monthly_prices, rankings)
    print("Success! Top-50 ranking and monthly-price CSV files saved.")


if __name__ == "__main__":
    download_sp500_monthly_data()
