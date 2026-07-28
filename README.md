# Options Payoff Chart Builder for IBKR

A lightweight, browser-based options payoff visualizer that integrates directly with Interactive Brokers (IBKR) via TWS or IB Gateway. It allows you to model multi-leg option strategies, incorporate underlying stock positions, and visualize expiration payoffs alongside dynamic Black-Scholes curves.

## Architecture & Logic

The tool is designed to be completely local and consists of standalone components:

1. **[ibkr_proxy.py](file:///k:/WIT/Python/options_payoff_chart_builder/ibkr_proxy.py)**: A Python Flask application that acts as a bridge via the **TWS Socket API** (`ibapi`). It connects to TWS or IB Gateway (default socket port `7497` paper / `7496` live).
2. **[ibkr_proxy_rest.py](file:///k:/WIT/Python/options_payoff_chart_builder/ibkr_proxy_rest.py)**: A Python Flask application that acts as a bridge via the **IBKR Client Portal REST Gateway** (`clientportal.gw`). It connects to the local REST API endpoint (default `https://127.0.0.1:5000/v1/api`).
3. **[options_payoff_ibkr.html](file:///k:/WIT/Python/options_payoff_chart_builder/options_payoff_ibkr.html)**: A single-page HTML application. It connects to the local proxy (listening on port `5001`) to import your positions, calculates theoretical option pricing using the Black-Scholes model, and renders the interactive payoff chart using Chart.js.

## How to Start

### 1. Prerequisites

Ensure you have Python installed, then install the required dependencies:

```bash
# For TWS Socket API proxy:
pip install ibapi flask flask-cors

# For Client Portal REST API proxy:
pip install requests flask flask-cors
```

### 2. Choose and Run Your Preferred Proxy

Both proxies listen on local port `5001` and provide the exact same interface for `options_payoff_ibkr.html`.

#### Option A: TWS / IB Gateway (Socket API)
Configure TWS / IB Gateway (**Settings > API > Settings**: check *Enable ActiveX and Socket Clients*).

```bash
python ibkr_proxy.py
# or launch ibkr_proxy_start.bat
```
*(Optional flags: `--tws-port 7496` for live TWS, `--tws-port 4001`/`4002` for Gateway, or `--market-data-type 1|2|3|4`.)*

#### Option B: Client Portal Gateway (REST API)
Start the IBKR Client Portal Gateway (`clientportal.gw`) and log in at `https://localhost:5000`.

```bash
python ibkr_proxy_rest.py
# or launch ibkr_proxy_rest_start.bat
```
*(Optional flags: `--gateway-url https://127.0.0.1:5000/v1/api`, `--proxy-port 5001`, `--no-ssl-verify`.)*

## Features

- Syncs directly with IBKR portfolio and retrieves exact positions and average cost bases.
- Gracefully attempts to pull Implied Volatility even when the market is closed (using frozen data).
- Interactive crosshair showing precise payoff expectations on both the expiration curve and the time-adjusted and IV-adjusted Black-Scholes curve.
- Master volatility slider for "what-if" analysis on vega exposure.
- Export/Import portfolio layouts to JSON.
- Manually add custom option legs and underlying positions to test hypothetical trades.

### 💡 Pro Tip: Shifting the P&L Curve

If you want to account for previously collected premiums (e.g., from closed or rolled legs), you can shift the entire payoff curve up or down. To do this, manually add two opposite options that cancel each other out (e.g., +1 Call and -1 Call at the exact same strike and expiry). Set the premium of one to `0`, and the other to the net premium amount you want to adjust by. This shifts your net profit/loss baseline without altering the underlying shape of the options curve.

## Disclaimer

This project is provided as-is. No ongoing support, maintenance, or future updates are planned.

Use at your own risk.

## Donations

If you find this project useful and would like to support the work that went into its development, donations are appreciated.

Ethereum / Base (ETH): `0xBA1903cEb50F92dDBde94498D14cdCc31fEFB7f9`<br>
Solana (SOL): `GrXWomqeGMzAcCCbYQ1Qq32t9qtsrsr4bTZTNcar1QLQ`

<img src="screenshots/demo.png" width="800">
