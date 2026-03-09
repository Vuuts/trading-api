from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import os
from datetime import datetime, timezone

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
OANDA_TOKEN   = os.environ.get("OANDA_TOKEN",   "")
OANDA_BASE    = os.environ.get("OANDA_BASE",    "https://api-fxtrade.oanda.com")
ALPACA_KEY    = os.environ.get("ALPACA_KEY",    "PKZTOKDIQMIP2TIE7YTEU7R3M4")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET", "9R1FFhBJ5DEWGJzqU3JsteDRTLUG2tbWHKfFsNM6p3FZ")
ALPACA_BASE   = os.environ.get("ALPACA_BASE",   "https://paper-api.alpaca.markets")

# ─── HEALTH ──────────────────────────────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "ok", "message": "Trading API running"})

# ─── SHARED OANDA FETCH ───────────────────────────────────────────────────────
def fetch_oanda_trades():
    """Fetch all 2026 closed OANDA trades, tagged by bot via clientExtensions comment."""
    headers = {
        "Authorization": f"Bearer {OANDA_TOKEN}",
        "Content-Type": "application/json"
    }
    url = f"{OANDA_BASE}/v3/accounts/{OANDA_ACCOUNT}/trades?state=CLOSED&count=500"
    res = requests.get(url, headers=headers, timeout=10)
    res.raise_for_status()
    data = res.json()

    trades = []
    for t in data.get("trades", []):
        open_time  = t.get("openTime", "")
        close_time = t.get("closeTime", open_time)

        # Only 2026
        if not open_time[:4] == "2026":
            continue

        pnl      = float(t.get("realizedPL", 0))
        initial  = int(float(t.get("initialUnits", 1)))
        direction = "buy" if initial > 0 else "sell"

        try:
            ot  = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
            ct  = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            dur = int((ct - ot).total_seconds() / 60)
        except:
            dur = 0

        # ── BOT TAG — read from clientExtensions comment ──────────────────────
        # ORB bots tag with "ORB", Sharkfin tags with "SHARKFIN"
        # Fallback: guess by duration (ORB trades are longer, SF are 5-pip scalps)
        comment = ""
        ce = t.get("clientExtensions", {})
        if ce:
            comment = ce.get("comment", "").upper()

        if "ORB" in comment:
            bot = "ORB"
        elif "SHARKFIN" in comment or "SF" in comment:
            bot = "SHARKFIN"
        else:
            # Fallback heuristic until tags propagate:
            # Sharkfin targets 5 pips — very short duration
            # ORB holds longer with trailing stop
            bot = "ORB" if dur >= 10 else "SHARKFIN"

        # R-multiple — ORB uses ~1R risk, SF uses 15pip stop / 5pip TP
        if bot == "SHARKFIN":
            # SF: 5 pip TP / 15 pip SL = 0.33R on win, -1R on loss
            r_mult = round(0.33 if pnl > 0 else -1.0, 2)
        else:
            r_mult = round(1.0 if pnl > 0 else -1.0, 2)

        trades.append({
            "id":          t.get("id"),
            "bot":         bot,
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
            "rMultiple":   r_mult,
            "comment":     comment,
        })

    return trades

# ─── ALL OANDA TRADES (tagged) ────────────────────────────────────────────────
@app.route("/trades/oanda")
def oanda_trades_all():
    try:
        trades = fetch_oanda_trades()
        return jsonify({"ok": True, "trades": trades, "count": len(trades)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── ORB TRADES ONLY ──────────────────────────────────────────────────────────
@app.route("/trades/orb")
def orb_trades():
    try:
        trades = fetch_oanda_trades()
        orb = [t for t in trades if t["bot"] == "ORB"]
        return jsonify({"ok": True, "trades": orb, "count": len(orb)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── SHARKFIN TRADES ONLY ─────────────────────────────────────────────────────
@app.route("/trades/sharkfin")
def sharkfin_trades():
    try:
        trades = fetch_oanda_trades()
        sf = [t for t in trades if t["bot"] == "SHARKFIN"]
        return jsonify({"ok": True, "trades": sf, "count": len(sf)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── ALPACA / MABOUNCER ───────────────────────────────────────────────────────
@app.route("/trades/alpaca")
def alpaca_trades():
    try:
        headers = {
            "APCA-API-KEY-ID":     ALPACA_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
        }
        url = f"{ALPACA_BASE}/v2/account/activities?activity_types=FILL&page_size=100"
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        fills = res.json()

        acct_res = requests.get(f"{ALPACA_BASE}/v2/account", headers=headers, timeout=10)
        acct = acct_res.json() if acct_res.ok else {}

        trades = []
        for i, f in enumerate(fills if isinstance(fills, list) else []):
            qty   = float(f.get("qty", 1))
            price = float(f.get("price", 0))
            side  = f.get("side", "buy")
            pnl   = qty * price * (1 if side == "sell" else -1)
            date_str = (f.get("transaction_time") or "")[:10]
            if not date_str.startswith("2026"):
                continue
            trades.append({
                "id":          f.get("id", str(i)),
                "bot":         "MABOUNCER",
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
                "rMultiple":   round(1.0 if pnl > 0 else -1.0, 2),
            })

        return jsonify({
            "ok": True, "trades": trades, "count": len(trades),
            "account": {
                "equity":       float(acct.get("equity", 0)),
                "buying_power": float(acct.get("buying_power", 0)),
                "cash":         float(acct.get("cash", 0)),
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── OANDA ACCOUNT ────────────────────────────────────────────────────────────
@app.route("/account/oanda")
def oanda_account():
    try:
        headers = {"Authorization": f"Bearer {OANDA_TOKEN}"}
        res = requests.get(f"{OANDA_BASE}/v3/accounts/{OANDA_ACCOUNT}/summary", headers=headers, timeout=10)
        res.raise_for_status()
        acct = res.json().get("account", {})
        return jsonify({
            "ok":           True,
            "balance":      float(acct.get("balance", 0)),
            "nav":          float(acct.get("NAV", 0)),
            "pl":           float(acct.get("pl", 0)),
            "unrealizedPL": float(acct.get("unrealizedPL", 0)),
            "openTrades":   int(acct.get("openTradeCount", 0)),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
