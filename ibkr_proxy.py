#!/usr/bin/env python3
"""
ibkr_proxy.py  —  Lightweight bridge between the Payoff Builder and IBKR TWS / IB Gateway
─────────────────────────────────────────────────────────────────────────────────────────────
Usage:
    python ibkr_proxy.py [--tws-host 127.0.0.1] [--tws-port 7497] [--proxy-port 5001] [--market-data-type 2]

Defaults:
    TWS paper trading port : 7497   (live trading: 7496)
    IB Gateway paper port  : 4002   (live: 4001)
    Proxy listens on       : 5001   (must match the URL in the Payoff Builder)
    Market data type      : 2 (Frozen)

Requirements:
    pip install ibapi flask flask-cors

TWS / Gateway setup:
    TWS  → Edit → Global Configuration → API → Settings
           ✓ Enable ActiveX and Socket Clients
           ✓ Allow connections from localhost only (recommended)
    Gateway → Configure → API → Settings (same checkboxes)
─────────────────────────────────────────────────────────────────────────────────────────────
"""

import argparse
import threading
import time
import sys
from datetime import datetime, timezone

try:
    from flask import Flask, jsonify, request as flask_request
    from flask_cors import CORS
except ImportError:
    sys.exit("Missing dependencies. Run:  pip install flask flask-cors ibapi")

try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
except ImportError:
    sys.exit("Missing ibapi. Run:  pip install ibapi")

SENTINEL = 1.7976931348623157e+308
MARKET_DATA_TYPE_LABELS = {
    1: "Live",
    2: "Frozen",
    3: "Delayed",
    4: "Delayed frozen",
}


# ──────────────────────────────────────────────────────────
# Persistent IBKR app — one long-lived connection
# ──────────────────────────────────────────────────────────

class IBApp(EWrapper, EClient):
    def __init__(self):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)

        self._connected = False
        self._ready = threading.Event()

        # Portfolio state — reset before each reqAccountUpdates call
        self.portfolio_items = []
        self._acct_done = threading.Event()

        # IV state
        self._iv_data = {}   # reqId → dict: {"iv": float, "undPrice": float}
        self._iv_events = {}   # reqId → threading.Event
        self._iv_lock = threading.Lock()

        # Price state
        self._price_data = {}   # reqId → float
        self._price_events = {}  # reqId → threading.Event
        self._price_lock = threading.Lock()

        # Serialise all TWS request sequences (portfolio vs IV)
        self._request_lock = threading.Lock()

    # ── Connection ──────────────────────────────────────────

    def connectAck(self): pass

    def nextValidId(self, orderId):
        self._connected = True
        self._ready.set()

    def connectionClosed(self):
        self._connected = False
        print("[IBKR] connection closed")

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # Silence purely informational codes
        if errorCode in (2100, 2104, 2106, 2107, 2108, 2158, 2119, 2176):
            return
        # No market data / bad request — unblock any waiting IV event
        if errorCode in (354, 300, 321, 10168):
            print(
                f"  [IV] reqId={reqId} code={errorCode}: {errorString.strip()} — skipping")
            with self._iv_lock:
                ev = self._iv_events.get(reqId)
            if ev:
                ev.set()
            return
        print(f"[IBKR] error reqId={reqId} code={errorCode}: {errorString}")

    # ── Portfolio callbacks ──────────────────────────────────

    def updatePortfolio(self, contract, position, marketPrice, marketValue,
                        averageCost, unrealizedPNL, realizedPNL, accountName):
        if position == 0:
            return
        self.portfolio_items.append({
            "contract":      contract,
            "position":      position,
            "marketPrice":   marketPrice,
            "averageCost":   averageCost,
            "unrealizedPNL": unrealizedPNL,
            "realizedPNL":   realizedPNL,
        })

    def accountDownloadEnd(self, accountName):
        self._acct_done.set()

    # ── Market data callbacks ────────────────────────────────

    def tickPrice(self, reqId, tickType, price, attrib):
        # 4=Last, 68=Delayed Last, 9=Close, 75=Delayed Close
        if tickType in (4, 68, 9, 75) and price > 0:
            with self._price_lock:
                if reqId in self._price_events:
                    self._price_data[reqId] = price
                    self._price_events[reqId].set()

    def tickSize(self, reqId, tickType, size): pass
    def tickSnapshotEnd(self, reqId): pass

    def tickGenericValue(self, reqId, tickType, value):
        # tickType 23 = historical volatility, 24 = implied volatility
        if tickType in (23, 24) and value and 0 < value < SENTINEL:
            print(
                f"  [tick-gen] reqId={reqId} tickType={tickType} val={value:.4f}")
            with self._iv_lock:
                if reqId not in self._iv_data:
                    self._iv_data[reqId] = {}
                self._iv_data[reqId]["iv"] = value
                ev = self._iv_events.get(reqId)
            if ev:
                ev.set()

    def tickOptionComputation(self, reqId, tickType, tickAttrib,
                              impliedVol, delta, optPrice, pvDividend,
                              gamma, vega, theta, undPrice):
        with self._iv_lock:
            if reqId not in self._iv_data:
                self._iv_data[reqId] = {}

            updated = False
            if impliedVol is not None and 0 < impliedVol < SENTINEL:
                self._iv_data[reqId]["iv"] = impliedVol
                updated = True
            if undPrice is not None and 0 < undPrice < SENTINEL:
                self._iv_data[reqId]["undPrice"] = undPrice
                updated = True

            if updated:
                ev = self._iv_events.get(reqId)
                if ev:
                    ev.set()


# ──────────────────────────────────────────────────────────
# Singleton connection manager
# ──────────────────────────────────────────────────────────

class IBKRConnection:
    def __init__(self, host, port, client_id=1, market_data_type=2):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.market_data_type = market_data_type
        self.app = None
        self._thread = None
        self._lock = threading.Lock()   # protects connect/reconnect

    def ensure_connected(self, timeout=15):
        """Return a live IBApp, (re)connecting if necessary."""
        with self._lock:
            if self.app and self.app._connected:
                return self.app

            print("[IBKR] (re)connecting…")
            app = IBApp()
            try:
                app.connect(self.host, self.port, clientId=self.client_id)
            except Exception as e:
                raise ConnectionError(
                    f"Could not connect to TWS/Gateway at {self.host}:{self.port} — {e}")

            t = threading.Thread(target=app.run, daemon=True)
            t.start()

            if not app._ready.wait(timeout=timeout):
                app.disconnect()
                raise TimeoutError(
                    "Timed out waiting for TWS connection acknowledgement")

            self.app = app
            self._thread = t
            print("[IBKR] connected")
            return self.app

    def fetch_portfolio(self, timeout=15):
        app = self.ensure_connected(timeout)
        with app._request_lock:
            app.portfolio_items = []
            app._acct_done.clear()
            app.reqAccountUpdates(True, "")
            app._acct_done.wait(timeout=timeout)
            time.sleep(0.8)              # let any stragglers arrive
            app.reqAccountUpdates(False, "")
            time.sleep(0.3)
        return list(app.portfolio_items)

    def fetch_iv(self, contracts, farm_wait=1.5, data_wait=8):
        """
        Subscribe to all contracts on the persistent connection.
        Strategy:
          1. Send subscriptions (live data, type 1).
          2. Wait up to farm_wait seconds for the farm to connect (2104).
          3. If farm was connecting, resubscribe so it pushes fresh ticks.
          4. Wait up to data_wait seconds for ticks; extend 5 s after first tick.
        """
        if not contracts:
            return {}

        app = self.ensure_connected()
        base_req = 3000

        with app._request_lock:
            # 1 = Live, 2 = Frozen, 3 = Delayed, 4 = Delayed Frozen
            app.reqMarketDataType(self.market_data_type)
            time.sleep(0.05)

            def build_contracts():
                opts = []
                for i, c in enumerate(contracts):
                    req = base_req + i
                    opt = Contract()
                    opt.symbol = c["symbol"]
                    opt.secType = "OPT"
                    opt.exchange = c.get("exchange") or "SMART"
                    opt.currency = c.get("currency") or "USD"
                    opt.lastTradeDateOrContractMonth = c["expiry"]
                    opt.strike = float(c["strike"])
                    opt.right = c["right"]
                    opt.multiplier = str(c.get("multiplier", 100))
                    key = (c["symbol"], c["expiry"],
                           float(c["strike"]), c["right"])
                    opts.append((req, opt, key))
                return opts

            contract_list = build_contracts()
            req_map = {}   # reqId → (key, event)

            def subscribe_all():
                for req, opt, key in contract_list:
                    ev = threading.Event()
                    with app._iv_lock:
                        app._iv_data.pop(req, None)
                        app._iv_events[req] = ev
                    # Request generic ticks 106 (Impl Vol), 100 (Opt Vol), 101 (Opt OI), 104 (Hist Vol)
                    print(
                        f"  [sub] reqId={req} {opt.symbol} {opt.right} {opt.strike} {opt.lastTradeDateOrContractMonth}")
                    app.reqMktData(req, opt, "106,100,101,104", False, False, [])
                    req_map[req] = (key, ev)
                    # Stagger slightly to avoid TWS model throttling
                    time.sleep(0.02)

            def cancel_all():
                for req in req_map:
                    try:
                        app.cancelMktData(req)
                    except Exception:
                        pass

            # ── First subscription pass ──
            subscribe_all()

            # ── Wait briefly to see if farm was connecting ──
            # 2104 (farm OK) and 2119 (connecting) come on reqId=-1 via error(),
            # so we just pause to let the farm settle then resubscribe.
            # If farm was already up, ticks arrive within ~1 s; skip resubscribe.
            any_event = threading.Event()
            # Peek: did anything fire within farm_wait?
            deadline_farm = time.monotonic() + farm_wait
            for req, (key, ev) in req_map.items():
                remaining = deadline_farm - time.monotonic()
                if remaining <= 0:
                    break
                if ev.wait(timeout=remaining):
                    any_event.set()
                    break

            if not any_event.is_set():
                # Farm was likely connecting — cancel and resubscribe now it's up
                print(
                    f"  [IV] no ticks in {farm_wait}s — farm may have just connected, resubscribing…")
                cancel_all()
                req_map.clear()
                time.sleep(0.3)
                subscribe_all()

            # ── Final wait: data_wait seconds, extending 5 s after first tick ──
            deadline = time.monotonic() + data_wait
            first_tick_received = any_event.is_set()  # already got one above
            for req, (key, ev) in req_map.items():
                while True:
                    with app._iv_lock:
                        has_iv = "iv" in app._iv_data.get(req, {})
                    if has_iv:
                        if not first_tick_received:
                            first_tick_received = True
                            new_deadline = time.monotonic() + 5
                            if new_deadline > deadline:
                                deadline = new_deadline
                                print(
                                    f"  [IV] first tick — extending deadline +5s")
                        break

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    ev.wait(timeout=remaining)
                    ev.clear()

            # Log and cancel
            for req, (key, ev) in req_map.items():
                print(
                    f"  [wait] reqId={req} fired={ev.is_set()} data={app._iv_data.get(req)}")
            cancel_all()
            time.sleep(0.1)

        # Collect results
        results = {}
        und_price = None
        for req, (key, ev) in req_map.items():
            data = app._iv_data.get(req, {})
            iv_raw = data.get("iv")
            if "undPrice" in data and und_price is None:
                und_price = data["undPrice"]
            iv_pct = round(iv_raw * 100, 2) if iv_raw is not None else None
            results[key] = iv_pct
            sym, exp, strike, right = key
            if iv_pct is not None:
                print(f"  [IV] {sym} {right} {strike} {exp}: {iv_pct:.2f}%")
            else:
                print(f"  [IV] {sym} {right} {strike} {exp}: not available")

        return results, und_price

    def fetch_stk_price(self, symbol, sec_type="STK", exchange="SMART", currency="USD", timeout=5):
        """Fetches the actual underlying price (Last/Close) directly."""
        app = self.ensure_connected()
        req = 6000
        ev = threading.Event()

        c = Contract()
        c.symbol = symbol
        c.secType = sec_type
        c.exchange = exchange
        c.currency = currency

        with app._request_lock:
            app.reqMarketDataType(self.market_data_type)
            with app._price_lock:
                app._price_data.pop(req, None)
                app._price_events[req] = ev

            app.reqMktData(req, c, "", False, False, [])

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ev.wait(timeout=deadline - time.monotonic())
                ev.clear()
                with app._price_lock:
                    if req in app._price_data:
                        break

            app.cancelMktData(req)
            with app._price_lock:
                return app._price_data.get(req)

# ──────────────────────────────────────────────────────────
# Classify portfolio items
# ──────────────────────────────────────────────────────────


def classify_items(items):
    options_out = []
    underlyings_out = []

    for item in items:
        c = item["contract"]
        pos = item["position"]
        avg_cost = item["averageCost"]

        if c.secType == "OPT":
            exp_raw = c.lastTradeDateOrContractMonth or ""
            expiry_str = (f"{exp_raw[:4]}-{exp_raw[4:6]}-{exp_raw[6:8]}"
                          if len(exp_raw) == 8 else exp_raw)
            try:
                mult = int(c.multiplier) if c.multiplier else 100
            except (ValueError, TypeError):
                mult = 100

            # TWS averageCost for options is per-contract (already × multiplier).
            # Divide by mult → per-share price for the UI formula:
            #   P&L = pos × mult × (intrinsic − premium_per_share)
            premium_per_share = avg_cost / mult if mult else avg_cost
            print(f"  OPT {c.symbol} {c.right} {c.strike} {expiry_str}: "
                  f"avgCost={avg_cost} mult={mult} "
                  f"→ premium_per_share={premium_per_share:.6f}")

            options_out.append({
                "symbol":   c.symbol,
                "type":     "call" if c.right == "C" else "put",
                "pos":      pos,
                "strike":   c.strike,
                "premium":  premium_per_share,
                "avgCost":  avg_cost,
                "marketPrice": item.get("marketPrice"),
                "mult":     mult,
                "iv":       None,
                "expiry":   expiry_str,
                "currency": c.currency,
                "exchange": c.exchange,
            })

        elif c.secType in ("STK", "FUT", "CFD", "CASH"):
            try:
                mult = int(c.multiplier) if c.multiplier else 1
            except (ValueError, TypeError):
                mult = 1

            underlyings_out.append({
                "symbol":   c.symbol,
                "secType":  c.secType,
                "pos":      pos,
                "entry":    avg_cost,
                "avgCost":  avg_cost,
                "marketPrice": item.get("marketPrice"),
                "mult":     mult,
                "currency": c.currency,
                "exchange": c.exchange,
            })

    options_out.sort(key=lambda o: (o["symbol"], o["expiry"], o["strike"]))
    underlyings_out.sort(key=lambda u: u["symbol"])
    return options_out, underlyings_out


# ──────────────────────────────────────────────────────────
# Flask proxy
# ──────────────────────────────────────────────────────────

def make_app(tws_host, tws_port, market_data_type=2):
    flask_app = Flask(__name__)
    CORS(flask_app)

    ibkr = IBKRConnection(tws_host, tws_port, client_id=1,
                          market_data_type=market_data_type)
    _cache = {"options": [], "underlyings": []}

    @flask_app.route("/ping")
    def ping():
        try:
            ibkr.ensure_connected(timeout=5)
            connected = True
        except Exception:
            connected = False
        return jsonify({
            "status":    "ok" if connected else "disconnected",
            "connected": connected,
            "time":      datetime.now(timezone.utc).isoformat(),
        })

    @flask_app.route("/portfolio")
    def portfolio():
        try:
            items = ibkr.fetch_portfolio()
            opts, unds = classify_items(items)
            _cache["options"] = opts
            _cache["underlyings"] = unds
            return jsonify({
                "options":     opts,
                "underlyings": unds,
                "fetchedAt":   datetime.now(timezone.utc).isoformat(),
            })
        except (ConnectionError, TimeoutError) as e:
            return jsonify({"error": str(e)}), 503
        except Exception as e:
            return jsonify({"error": f"Unexpected error: {e}"}), 500

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

        contracts = [{
            "symbol":     o["symbol"],
            "expiry":     o.get("expiry", "").replace("-", ""),
            "strike":     float(o.get("strike", 0)),
            "right":      "C" if o.get("type", "call").lower() == "call" else "P",
            "multiplier": str(o.get("mult", 100)),
            "exchange":   o.get("exchange") or "SMART",
            "currency":   o.get("currency") or "USD",
        } for o in opts if o.get("expiry") and o.get("strike")]

        if not contracts and opts:
            return jsonify({"error": f"One or more {symbol} legs are missing an expiry or strike"}), 400
        elif not contracts:
            return jsonify({"error": f"No valid option expirations/strikes provided for {symbol}"}), 400

        print(f"[IV] Fetching IV for {len(contracts)} {symbol} option(s)…")
        try:
            raw, und_price = ibkr.fetch_iv(contracts)
        except (ConnectionError, TimeoutError) as e:
            return jsonify({"error": str(e)}), 503
        except Exception as e:
            return jsonify({"error": f"IV fetch error: {e}"}), 500

        # Attempt to get exact underlying price instead of the option model midpoint
        clean_price = ibkr.fetch_stk_price(symbol, "STK", timeout=2)
        if clean_price is None:
            clean_price = ibkr.fetch_stk_price(symbol, "IND", timeout=2)

        final_price = clean_price if clean_price is not None else und_price

        iv_out = {
            f"{exp}|{strike}|{right}": pct
            for (sym, exp, strike, right), pct in raw.items()
        }
        return jsonify({
            "symbol":    symbol,
            "iv":        iv_out,
            "underlyingPrice": final_price,
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        })

    @flask_app.route("/price")
    def price():
        symbol = flask_request.args.get("symbol", "").upper()
        if not symbol:
            return jsonify({"error": "symbol param required"}), 400

        # Fetch clean price
        try:
            p = ibkr.fetch_stk_price(symbol, "STK", timeout=3)
            if p is None:
                p = ibkr.fetch_stk_price(symbol, "IND", timeout=3)
            if p is not None:
                return jsonify({"symbol": symbol, "price": p})

            # Fallback: Try to get from underlying cache if we hold the stock
            unds = [u for u in _cache["underlyings"] if u["symbol"] == symbol]
            if unds and unds[0].get("marketPrice"):
                return jsonify({"symbol": symbol, "price": unds[0]["marketPrice"]})

            return jsonify({"error": "Price not available"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return flask_app


# ──────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IBKR Payoff Builder proxy")
    parser.add_argument("--tws-host",   default="127.0.0.1")
    parser.add_argument("--tws-port",   default=7497, type=int,
                        help="7497=TWS paper, 7496=TWS live, 4002=GW paper, 4001=GW live")
    parser.add_argument("--proxy-port", default=5001, type=int)
    parser.add_argument("--market-data-type", default=2, type=int,
                        choices=[1, 2, 3, 4],
                        help="1=Live, 2=Frozen (default), 3=Delayed, 4=Delayed frozen")
    args = parser.parse_args()

    tws_str = f"{args.tws_host}:{args.tws_port}"
    proxy_str = f"http://localhost:{args.proxy_port}"
    md_label = MARKET_DATA_TYPE_LABELS.get(args.market_data_type, "unknown")

    def banner_line(label, value):
        return f"║  {label:<15}: {value:<35}║"

    print(f"""
╔══════════════════════════════════════════════════════╗
║       IBKR Payoff Builder Proxy  v1.4                ║
╠══════════════════════════════════════════════════════╣
{banner_line('TWS / Gateway', tws_str)}
{banner_line('Proxy URL', proxy_str)}
{banner_line('Market data', f'{args.market_data_type} ({md_label})')}
╚══════════════════════════════════════════════════════╝

  Endpoints:
    /ping        — health check (also verifies TWS connection)
    /portfolio   — all positions (fast, no IV)
    /iv?symbol=X — live IV for all options of ticker X
                   (accepts POST with custom options payload)
    /price?symbol=X — live underlying Last/Close price for ticker X

  One persistent TWS connection is kept alive.
  Press Ctrl+C to stop.
""")

    app = make_app(args.tws_host, args.tws_port, args.market_data_type)
    app.run(host="127.0.0.1", port=args.proxy_port, debug=False)
