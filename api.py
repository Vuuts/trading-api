from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app, origins="*")

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response

@app.route("/options-handler", methods=["OPTIONS"])
def options_handler():
    return jsonify({}), 200

# ─── CREDENTIALS ─────────────────────────────────────────────────────────────
OANDA_ACCOUNT = os.environ.get("OANDA_ACCOUNT", "001-001-438810-004")
OANDA_TOKEN   = os.environ.get("OANDA_TOKEN",   "640f59ed62a2aca58db8ec35f0fb3014-1d2ee48b85b3f9fda734801f8f84773e")
OANDA_BASE    = os.environ.get("OANDA_BASE",    "https://api-fxtrade.oanda.com")
ALPACA_KEY    = os.environ.get("ALPACA_KEY",    "PKZTOKDIQMIP2TIE7YTEU7R3M4")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET", "9R1FFhBJ5DEWGJzqU3JsteDRTLUG2tbWHKfFsNM6p3FZ")
ALPACA_BASE   = os.environ.get("ALPACA_BASE",   "https://paper-api.alpaca.markets")

# ─── HEALTH CHECK ────────────────────────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "ok", "message": "Trading API running"})

# ─── OANDA TRADES (ORB + SHARKFIN) ───────────────────────────────────────────
@app.route("/trades/oanda")
def oanda_trades():
    try:
        headers = {
            "Authorization": f"Bearer {OANDA_TOKEN}",
            "Content-Type": "application/json"
        }
        url = f"{OANDA_BASE}/v3/accounts/{OANDA_ACCOUNT}/trades?state=CLOSED&count=200"
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        data = res.json()

        trades = []
        for t in data.get("trades", []):
            open_time  = t.get("openTime", "")
            close_time = t.get("closeTime", open_time)
            pnl        = float(t.get("realizedPL", 0))
            initial    = int(float(t.get("initialUnits", 1)))
            direction  = "buy" if initial > 0 else "sell"

            # Duration in minutes
            from datetime import datetime, timezone
            try:
                ot = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
                ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                dur = int((ct - ot).total_seconds() / 60)
            except:
                dur = 0

            trades.append({
                "id":          t.get("id"),
                "pair":        t.get("instrument", "").replace("_", "/"),
                "direction":   direction,
                "pnl":         round(pnl, 4),
                "win":         pnl > 0,
                "date":        open_time[:10],
                "openTime":    open_time,
                "closeTime":   close_time,
                "durationMin": dur,
                "entry":       float(t.get("price", 0)),
                "exit":        float(t.get("averageClosePrice", t.get("price", 0))),
                "units":       abs(initial),
                "rMultiple":   round(pnl / (abs(pnl) * 0.8) * (1 if pnl > 0 else -1), 2) if pnl != 0 else 0,
            })

        # Filter to 2026 only
        trades = [t for t in trades if t["date"].startswith("2026")]

        return jsonify({"ok": True, "trades": trades, "count": len(trades)})

    except requests.exceptions.RequestException as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── ALPACA TRADES (MABOUNCER) ────────────────────────────────────────────────
@app.route("/trades/alpaca")
def alpaca_trades():
    try:
        headers = {
            "APCA-API-KEY-ID":     ALPACA_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
        }
        # Get closed positions history via activities
        url = f"{ALPACA_BASE}/v2/account/activities?activity_types=FILL&page_size=100"
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        fills = res.json()

        # Also get account info
        acct_res = requests.get(f"{ALPACA_BASE}/v2/account", headers=headers, timeout=10)
        acct = acct_res.json() if acct_res.ok else {}

        trades = []
        for i, f in enumerate(fills if isinstance(fills, list) else []):
            qty   = float(f.get("qty", 1))
            price = float(f.get("price", 0))
            side  = f.get("side", "buy")
            # Estimate PnL — buys are costs, sells are revenue
            pnl = qty * price * (1 if side == "sell" else -1)
            date_str = (f.get("transaction_time") or "")[:10]

            trades.append({
                "id":          f.get("id", str(i)),
                "pair":        f.get("symbol", ""),
                "direction":   side,
                "pnl":         round(pnl, 2),
                "win":         pnl > 0,
                "date":        date_str,
                "openTime":    f.get("transaction_time", ""),
                "closeTime":   f.get("transaction_time", ""),
                "durationMin": 0,
                "entry":       price,
                "exit":        price,
                "units":       qty,
                "rMultiple":   round(pnl / (abs(pnl) * 20) * (1 if pnl > 0 else -1), 2) if pnl != 0 else 0,
            })

        return jsonify({
            "ok": True,
            "trades": trades,
            "count": len(trades),
            "account": {
                "equity":       float(acct.get("equity", 0)),
                "buying_power": float(acct.get("buying_power", 0)),
                "cash":         float(acct.get("cash", 0)),
            }
        })

    except requests.exceptions.RequestException as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── OANDA ACCOUNT INFO ───────────────────────────────────────────────────────
@app.route("/account/oanda")
def oanda_account():
    try:
        headers = {"Authorization": f"Bearer {OANDA_TOKEN}"}
        url = f"{OANDA_BASE}/v3/accounts/{OANDA_ACCOUNT}/summary"
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        data = res.json()
        acct = data.get("account", {})
        return jsonify({
            "ok":      True,
            "balance": float(acct.get("balance", 0)),
            "nav":     float(acct.get("NAV", 0)),
            "pl":      float(acct.get("pl", 0)),
            "unrealizedPL": float(acct.get("unrealizedPL", 0)),
            "openTrades": int(acct.get("openTradeCount", 0)),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
