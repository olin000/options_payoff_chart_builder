#!/usr/bin/env python3
"""
ibkr_proxy_rest.py  —  Lightweight bridge between Payoff Builder and IBKR REST Client Portal Gateway
───────────────────────────────────────────────────────────────────────────────────────────────────
Usage:
    python ibkr_proxy_rest.py [--gateway-url https://127.0.0.1:5000/v1/api] [--proxy-port 5001]

Defaults:
    Client Portal Gateway URL : https://127.0.0.1:5000/v1/api
    Proxy listens on          : 5001   (must match the URL in the Payoff Builder)

Requirements:
    pip install requests flask flask-cors

Client Portal Gateway setup:
    1. Start IBKR Client Portal Gateway (clientportal.gw) or IBKR Desktop / Web API Gateway.
    2. Complete authentication via browser at https://localhost:5000.
───────────────────────────────────────────────────────────────────────────────────────────────────
"""

import argparse
import sys
import time
import urllib3
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("Missing dependencies. Run:  pip install requests flask flask-cors")

try:
    from flask import Flask, jsonify, request as flask_request
    from flask_cors import CORS
except ImportError:
    sys.exit("Missing dependencies. Run:  pip install flask flask-cors requests")

# Disable SSL verification warnings for local self-signed gateway certificate
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MARKET_DATA_TYPE_LABELS = {
    1: "Live",
    2: "Frozen",
    3: "Delayed",
    4: "Delayed frozen",
}


class IBKRRestConnection:
    """Manages HTTP session & requests to IBKR Client Portal REST API."""

    def __init__(self, gateway_url="https://127.0.0.1:5000/v1/api", account_id=None, ssl_verify=False):
        self.gateway_url = gateway_url.rstrip("/")
        self.account_id = account_id
        self.ssl_verify = ssl_verify

        self.session = requests.Session()
        self.session.verify = self.ssl_verify
        self.session.headers.update({
            "User-Agent": "IBKR-Payoff-Proxy/1.0",
            "Accept": "application/json",
        })

    def req(self, method, endpoint, **kwargs):
        """Helper to call gateway endpoint."""
        url = f"{self.gateway_url}{endpoint}" if endpoint.startswith("/") else f"{self.gateway_url}/{endpoint}"
        kwargs.setdefault("timeout", 10)
        kwargs.setdefault("verify", self.ssl_verify)
        resp = self.session.request(method, url, **kwargs)
        return resp

    def check_auth(self):
        """Check authentication status with Client Portal Gateway."""
        try:
            r = self.req("GET", "/iserver/auth/status")
            if r.status_code == 200:
                data = r.json()
                # Gateway returns authenticated: True / False
                return bool(data.get("authenticated", False))
            
            # Fallback check tickle
            r_tickle = self.req("GET", "/tickle")
            if r_tickle.status_code == 200:
                data = r_tickle.json()
                return bool(data.get("iserver", {}).get("authStatus", {}).get("authenticated", False))
            return False
        except Exception as e:
            print(f"[REST] Auth check error: {e}")
            return False

    def get_account_id(self):
        """Get the primary IBKR account ID."""
        if self.account_id:
            return self.account_id

        r = self.req("GET", "/portfolio/accounts")
        if r.status_code == 200:
            accounts = r.json()
            if isinstance(accounts, list) and len(accounts) > 0:
                # Can be list of dicts with 'id' or list of strings
                acct = accounts[0]
                self.account_id = acct.get("id") if isinstance(acct, dict) else str(acct)
                return self.account_id
        
        # Alternative endpoint check
        r_sub = self.req("GET", "/portfolio/subaccounts")
        if r_sub.status_code == 200:
            subs = r_sub.json()
            if isinstance(subs, list) and len(subs) > 0:
                self.account_id = subs[0].get("id")
                return self.account_id

        raise ValueError("Could not retrieve account ID from Client Portal Gateway")

    def fetch_positions(self):
        """Fetch portfolio positions for account."""
        acct_id = self.get_account_id()
        r = self.req("GET", f"/portfolio/{acct_id}/positions/0")
        if r.status_code != 200:
            raise ConnectionError(f"Failed to fetch positions (HTTP {r.status_code}): {r.text}")
        return r.json()

    def search_secdef(self, symbol, sec_type="STK"):
        """Search symbol contract info to obtain conid."""
        r = self.req("GET", f"/iserver/secdef/search?symbol={requests.utils.quote(symbol)}")
        if r.status_code == 200:
            results = r.json()
            if isinstance(results, list) and len(results) > 0:
                # Filter by sec_type if possible
                for item in results:
                    if item.get("secType") == sec_type:
                        return item
                return results[0]
        return None

    def resolve_option_conid(self, und_conid, expiry_month, strike, right, exchange="SMART"):
        """Resolve specific option contract ID."""
        # month format MMMYY e.g. MAR25 or YYYYMMDD
        params = {
            "conid": und_conid,
            "secType": "OPT",
            "month": expiry_month.upper(),
            "strike": strike,
            "right": right.upper(),
        }
        if exchange:
            params["exchange"] = exchange

        r = self.req("GET", "/iserver/secdef/info", params=params)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                return data[0].get("conid")
        return None

    def get_market_data_snapshot(self, conids, fields="31,70,71,84,85,7283,7633,74"):
        """
        Request market data snapshot for contract conids.
        Field 31: Last Price, 70: High, 71: Low, 84: Bid, 85: Ask, 74: Und Price, 7283/7633: Implied Vol.
        CP Gateway snapshot often requires an initial request to kick off subscriptions, followed by a second fetch.
        """
        if isinstance(conids, (list, tuple)):
            conid_str = ",".join(str(c) for c in conids)
        else:
            conid_str = str(conids)

        url = f"/iserver/marketdata/snapshot?conids={conid_str}&fields={fields}"
        r1 = self.req("GET", url)
        time.sleep(0.3)
        r2 = self.req("GET", url)
        
        data = r2.json() if r2.status_code == 200 else (r1.json() if r1.status_code == 200 else [])
        return data if isinstance(data, list) else []


def classify_rest_items(items):
    """Normalize Client Portal Gateway position objects into Payoff Builder format."""
    options_out = []
    underlyings_out = []

    for item in items:
        pos = float(item.get("position", 0))
        if pos == 0:
            continue

        sec_type = (item.get("assetClass") or item.get("secType") or "").upper()
        
        # Extract root underlying symbol (e.g. "AAOI" instead of "AAOI 260731P00089000 100")
        raw_sym = (
            item.get("underlyingSymbol") or
            item.get("undSymbol") or
            item.get("symbol") or
            item.get("ticker") or
            item.get("contractDesc") or
            ""
        ).strip()
        symbol = raw_sym.split()[0].upper() if raw_sym else ""

        avg_cost = float(item.get("avgCost", 0))
        avg_price = float(item.get("avgPrice", 0)) if "avgPrice" in item else None
        mkt_price = float(item.get("mktPrice", 0)) if item.get("mktPrice") is not None else None
        currency = item.get("currency") or "USD"
        exchange = item.get("listingExchange") or item.get("exchange") or "SMART"
        conid = item.get("conid")

        if sec_type == "OPT":
            right = (item.get("right") or item.get("putOrCall") or "C")[0].upper()
            strike = float(item.get("strike", 0))
            
            exp_raw = str(item.get("expiry") or item.get("lastTradingDay") or "")
            
            # Fallback OCC parsing if strike or expiry missing
            ticker_str = (item.get("ticker") or item.get("contractDesc") or "").strip()
            parts = ticker_str.split()
            if len(parts) >= 2 and len(parts[1]) >= 15:
                occ = parts[1]  # e.g. "260731P00089000"
                if not exp_raw:
                    yymmdd = occ[:6]
                    exp_raw = f"20{yymmdd[:2]}{yymmdd[2:4]}{yymmdd[4:6]}"
                if strike == 0:
                    right = occ[6].upper()
                    try:
                        strike = float(occ[7:]) / 1000.0
                    except ValueError:
                        pass

            if len(exp_raw) == 8:
                expiry_str = f"{exp_raw[:4]}-{exp_raw[4:6]}-{exp_raw[6:8]}"
            else:
                expiry_str = exp_raw

            try:
                mult = int(item.get("multiplier", 100))
            except (ValueError, TypeError):
                mult = 100

            # Calculate per-share premium
            if avg_price is not None and avg_price > 0:
                premium_per_share = avg_price
            elif mult > 0 and avg_cost > 0:
                premium_per_share = avg_cost / mult if avg_cost < 10000 else avg_cost / (abs(pos) * mult)
            else:
                premium_per_share = avg_cost

            print(f"  OPT {symbol} {right} {strike} {expiry_str}: "
                  f"avgCost={avg_cost} mult={mult} → premium_per_share={premium_per_share:.6f}")

            options_out.append({
                "symbol":      symbol,
                "type":        "call" if right == "C" else "put",
                "pos":         pos,
                "strike":      strike,
                "premium":     premium_per_share,
                "avgCost":     avg_cost,
                "marketPrice": mkt_price,
                "mult":        mult,
                "iv":          None,
                "expiry":      expiry_str,
                "currency":    currency,
                "exchange":    exchange,
                "conid":       conid,
            })

        elif sec_type in ("STK", "FUT", "CFD", "CASH"):
            try:
                mult = int(item.get("multiplier", 1))
            except (ValueError, TypeError):
                mult = 1

            entry_price = avg_price if avg_price is not None and avg_price > 0 else avg_cost

            underlyings_out.append({
                "symbol":      symbol,
                "secType":     sec_type,
                "pos":         pos,
                "entry":       entry_price,
                "avgCost":     avg_cost,
                "marketPrice": mkt_price,
                "mult":        mult,
                "currency":    currency,
                "exchange":    exchange,
                "conid":       conid,
            })

    options_out.sort(key=lambda o: (o["symbol"], o["expiry"], o["strike"]))
    underlyings_out.sort(key=lambda u: u["symbol"])
    return options_out, underlyings_out


def make_rest_app(gateway_url, account_id=None, ssl_verify=False):
    flask_app = Flask(__name__)
    CORS(flask_app)

    ibkr = IBKRRestConnection(gateway_url=gateway_url, account_id=account_id, ssl_verify=ssl_verify)
    _cache = {"options": [], "underlyings": []}

    @flask_app.route("/ping")
    def ping():
        connected = ibkr.check_auth()
        return jsonify({
            "status":    "ok" if connected else "disconnected",
            "connected": connected,
            "time":      datetime.now(timezone.utc).isoformat(),
        })

    @flask_app.route("/portfolio")
    def portfolio():
        try:
            items = ibkr.fetch_positions()
            opts, unds = classify_rest_items(items)
            _cache["options"] = opts
            _cache["underlyings"] = unds
            return jsonify({
                "options":     opts,
                "underlyings": unds,
                "fetchedAt":   datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            print(f"[REST] /portfolio error: {e}")
            return jsonify({"error": str(e)}), 503

    @flask_app.route("/iv", methods=["GET", "POST"])
    def iv():
        symbol = flask_request.args.get("symbol", "").upper()
        if not symbol:
            return jsonify({"error": "symbol param required"}), 400

        opts = []
        if flask_request.method == "POST":
            payload = flask_request.get_json(silent=True) or {}
            opts = payload.get("options", [])

        if not opts:
            opts = [o for o in _cache["options"] if o["symbol"] == symbol]

        if not opts:
            return jsonify({
                "error": f"No options found for {symbol}. Please add option legs manually or refresh portfolio first"
            }), 404

        conid_map = {}
        # Fetch conid for underlying
        und_secdef = ibkr.search_secdef(symbol, sec_type="STK")
        und_conid = und_secdef.get("conid") if und_secdef else None

        for o in opts:
            conid = o.get("conid")
            key = (o["symbol"], o.get("expiry", "").replace("-", ""), float(o.get("strike", 0)), "C" if o.get("type", "call").lower() == "call" else "P")
            
            if not conid and und_conid:
                exp = o.get("expiry", "").replace("-", "")
                # Format expiry month e.g. YYYYMM
                exp_month = exp[:6] if len(exp) >= 6 else exp
                conid = ibkr.resolve_option_conid(und_conid, exp_month, float(o.get("strike", 0)), key[3], o.get("exchange"))
            
            if conid:
                conid_map[conid] = key

        if not conid_map:
            return jsonify({"error": f"Could not resolve option contract IDs for {symbol}"}), 404

        print(f"[REST IV] Requesting market snapshot for {len(conid_map)} contract(s)...")
        snapshots = ibkr.get_market_data_snapshot(list(conid_map.keys()))

        iv_results = {}
        und_price = None

        for snap in snapshots:
            c_id = snap.get("conid")
            if c_id in conid_map:
                key = conid_map[c_id]
                # Field 7283 or 7633 = Implied Volatility
                raw_iv = snap.get("7283") or snap.get("7633") or snap.get("7284")
                if raw_iv is not None:
                    try:
                        iv_val = float(str(raw_iv).replace("%", ""))
                        iv_pct = round(iv_val * 100, 2) if iv_val < 2.0 else round(iv_val, 2)
                        iv_results[key] = iv_pct
                    except (ValueError, TypeError):
                        pass

                # Field 74 = Underlying price
                if snap.get("74") is not None and und_price is None:
                    try:
                        und_price = float(snap.get("74"))
                    except (ValueError, TypeError):
                        pass

        # If underlying price was not in option snapshot, query underlying snapshot directly
        if und_price is None and und_conid:
            und_snaps = ibkr.get_market_data_snapshot([und_conid], fields="31,84,85")
            if und_snaps and isinstance(und_snaps, list):
                snap = und_snaps[0]
                p_str = snap.get("31") or snap.get("84") or snap.get("85")
                if p_str:
                    try:
                        und_price = float(p_str)
                    except (ValueError, TypeError):
                        pass

        iv_out = {
            f"{exp}|{strike}|{right}": pct
            for (sym, exp, strike, right), pct in iv_results.items()
        }

        return jsonify({
            "symbol":          symbol,
            "iv":              iv_out,
            "underlyingPrice": und_price,
            "fetchedAt":       datetime.now(timezone.utc).isoformat(),
        })

    @flask_app.route("/price")
    def price():
        symbol = flask_request.args.get("symbol", "").upper()
        if not symbol:
            return jsonify({"error": "symbol param required"}), 400

        try:
            secdef = ibkr.search_secdef(symbol, sec_type="STK")
            if secdef and secdef.get("conid"):
                conid = secdef["conid"]
                snaps = ibkr.get_market_data_snapshot([conid], fields="31,84,85")
                if snaps and isinstance(snaps, list):
                    snap = snaps[0]
                    p_val = snap.get("31") or snap.get("84") or snap.get("85")
                    if p_val:
                        return jsonify({"symbol": symbol, "price": float(p_val)})

            # Fallback to cache
            unds = [u for u in _cache["underlyings"] if u["symbol"] == symbol]
            if unds and unds[0].get("marketPrice"):
                return jsonify({"symbol": symbol, "price": unds[0]["marketPrice"]})

            return jsonify({"error": f"Price for {symbol} not available"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return flask_app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IBKR Payoff Builder REST Client Portal Gateway Proxy")
    parser.add_argument("--gateway-url", default="https://127.0.0.1:5000/v1/api",
                        help="Client Portal Gateway API base URL (default: https://127.0.0.1:5000/v1/api)")
    parser.add_argument("--proxy-port", default=5001, type=int,
                        help="Proxy port (default: 5001)")
    parser.add_argument("--account-id", default=None,
                        help="Optional specific IBKR account ID")
    parser.add_argument("--ssl-verify", action="store_true", default=False,
                        help="Enable SSL certificate verification")
    parser.add_argument("--no-ssl-verify", dest="ssl_verify", action="store_false",
                        help="Disable SSL certificate verification (default)")
    args = parser.parse_args()

    proxy_str = f"http://localhost:{args.proxy_port}"

    def banner_line(label, value):
        return f"║  {label:<15}: {value:<35}║"

    print(f"""
╔══════════════════════════════════════════════════════╗
║   IBKR Payoff Builder REST Gateway Proxy  v1.0       ║
╠══════════════════════════════════════════════════════╣
{banner_line('CP Gateway URL', args.gateway_url)}
{banner_line('Proxy URL', proxy_str)}
{banner_line('SSL Verify', str(args.ssl_verify))}
╚══════════════════════════════════════════════════════╝

  Endpoints:
    /ping        — health check & Client Portal Gateway auth status
    /portfolio   — all positions (options & underlyings)
    /iv?symbol=X — live IV for options of ticker X
    /price?symbol=X — live underlying price for ticker X

  Connecting via IBKR Client Portal REST API.
  Press Ctrl+C to stop.
""")

    app = make_rest_app(gateway_url=args.gateway_url, account_id=args.account_id, ssl_verify=args.ssl_verify)
    app.run(host="127.0.0.1", port=args.proxy_port, debug=False)
