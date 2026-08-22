# S&P 500 Stock Picker Simulator

A browser-based educational backtest comparing monthly investing, lump-sum investing, and a rule-based stock picker over downloaded monthly S&P 500 constituent data.

## Features

- Select 1–50 top-ranked stocks.
- Rank and weight by market capitalization, trailing-year average daily volume, or trailing-year performance.
- Configure dip sales, take-profit sales, rebuy cooldowns, fees, and idle-cash interest.
- Apply a 25% tax to realized picker profits using average cost basis; losses receive no tax credit.
- Review every ticker held during the period, with end-of-period holdings shown in bold.
- Optimize rule parameters for maximum gain, minimum trades, or minimum contribution-adjusted volatility among rules that outperform monthly investing.
- Compare results with an equal-weight current-constituent benchmark proxy.

## Methodology warning

The backtest applies current S&P 500 membership and current ranking snapshots to historical prices. This introduces survivorship and look-ahead bias. The benchmark is an equal-weight proxy, not the official S&P 500 index. Optimizer results are in-sample and may overfit the selected historical window.

This project is for education and research only, not financial advice.
