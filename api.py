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

# ─── SUPABASE ─────────────────────────────────────────────────────────────────
SUPA_URL = os.environ.get("SUPABASE_URL", "https://myetabcvnbltfruppuod.supabase.co")
SUPA_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im15ZXRhYmN2bmJsdGZydXBwdW9kIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjY0NDkwNiwiZXhwIjoyMDg4MjIwOTA2fQ.09Me5NQ-FVvm7w8JGvdNsZbJjHZtrS1EhS2BRb1KgAQ")
SUPA_HDR = {
    "apikey": SUPA_KEY,
    "Authorization": f"Bearer {SUPA_KEY}",
    "Content-Type": "application/json"
}

def fetch_supabase_stats() -> dict:
    """Returns dict of trade_id -> stats from Supabase."""
    try:
        r = requests.get(
            f"{SUPA_URL}/rest/v1/trade_stats?select=*&limit=500",
            headers=SUPA_HDR, timeout=5
        )
        if r.ok:
            return {s["trade_id"]: s for s in r.json()}
    except:
        pass
    return {}

@app.route("/trades/detail/<trade_id>")
def trade_detail(trade_id):
    """Return full stats for a single trade including max profit/drawdown."""
    try:
        r = requests.get(
            f"{SUPA_URL}/rest/v1/trade_stats?trade_id=eq.{trade_id}&select=*",
            headers=SUPA_HDR, timeout=5
        )
        if r.ok and r.json():
            return jsonify({"ok": True, "stats": r.json()[0]})
        return jsonify({"ok": False, "error": "Trade not found"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── SESSION TAGGER ──────────────────────────────────────────────────────────
def utc_to_et_hour(open_time: str) -> int:
    """Convert OANDA UTC timestamp to ET hour (handles EDT/EST automatically)."""
    dt    = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
    month = dt.month
    # EDT = UTC-4 (Mar-Nov), EST = UTC-5 (Nov-Mar)
    offset = 4 if 3 <= month <= 11 else 5
    return (dt.hour - offset) % 24

def get_orb_session(open_time: str) -> str:
    try:
        hour = utc_to_et_hour(open_time)
        if hour >= 19 or hour < 3:  return "Asia"
        if 9 <= hour < 17:          return "NY"
        return "Other"
    except:
        return "Other"

def get_sf_session(open_time: str) -> str:
    try:
        hour = utc_to_et_hour(open_time)
        if 3  <= hour < 8:  return "London"
        if 8  <= hour < 12: return "London/NY"
        if 12 <= hour < 17: return "NY"
        return "Other"
    except:
        return "Other"

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
        elif "DIVERGE" in comment or "DIV" in comment:
            bot = "DIVERGE"
        elif "SHARKFIN" in comment or "SF" in comment:
            bot = "SHARKFIN"
        else:
            # Fallback heuristic until tags propagate
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
            "session":     get_orb_session(open_time) if bot == "ORB" else get_sf_session(open_time) if bot in ("SHARKFIN","DIVERGE") else None,
        })

    # Enrich with Supabase stats (max profit/drawdown)
    supa_stats = fetch_supabase_stats()
    for t in trades:
        tid  = str(t.get("id",""))
        stat = supa_stats.get(tid, {})
        t["maxProfit"]   = stat.get("max_profit")
        t["maxDrawdown"] = stat.get("max_drawdown")
        t["maxPrice"]    = stat.get("max_price")
        t["minPrice"]    = stat.get("min_price")

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

# ─── DIVERGE TRADES ONLY ─────────────────────────────────────────────────────
@app.route("/trades/diverge")
def diverge_trades():
    try:
        trades = fetch_oanda_trades()
        div = [t for t in trades if t["bot"] == "DIVERGE"]
        return jsonify({"ok": True, "trades": div, "count": len(div)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── ALPACA / MABOUNCER ───────────────────────────────────────────────────────
@app.route("/trades/alpaca")
def alpaca_trades():
    """
    Reconstruct completed trades from Alpaca order history.
    Match buy orders to subsequent sell orders on the same symbol.
    """
    try:
        headers = {
            "APCA-API-KEY-ID":     ALPACA_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
        }

        # Get all filled orders (closed trades)
        url = f"{ALPACA_BASE}/v2/orders?status=closed&limit=200&direction=desc"
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        orders = res.json() if isinstance(res.json(), list) else []

        # Also get account
        acct_res = requests.get(f"{ALPACA_BASE}/v2/account", headers=headers, timeout=10)
        acct = acct_res.json() if acct_res.ok else {}

        # Group filled orders by symbol — match buys to sells
        from collections import defaultdict
        by_symbol = defaultdict(list)
        for o in orders:
            if o.get("status") == "filled" and o.get("filled_qty"):
                date_str = (o.get("filled_at") or o.get("submitted_at") or "")[:10]
                if not date_str.startswith("2026"):
                    continue
                by_symbol[o["symbol"]].append({
                    "side":      o["side"],
                    "qty":       float(o.get("filled_qty", 0)),
                    "price":     float(o.get("filled_avg_price", 0)),
                    "date":      date_str,
                    "time":      o.get("filled_at") or o.get("submitted_at", ""),
                    "id":        o["id"],
                })

        # Match buy/sell pairs into completed trades
        trades = []
        trade_idx = 0
        for symbol, fills in by_symbol.items():
            # Sort by time
            fills.sort(key=lambda x: x["time"])
            buys  = [f for f in fills if f["side"] == "buy"]
            sells = [f for f in fills if f["side"] == "sell"]

            # Pair them up in order
            pairs = zip(buys, sells)
            for buy, sell in pairs:
                entry = buy["price"]
                exit_p = sell["price"]
                qty   = min(buy["qty"], sell["qty"])
                pnl   = round((exit_p - entry) * qty, 2)

                # Estimate duration
                try:
                    from datetime import datetime, timezone
                    t1 = datetime.fromisoformat(buy["time"].replace("Z", "+00:00"))
                    t2 = datetime.fromisoformat(sell["time"].replace("Z", "+00:00"))
                    dur = max(1, int((t2 - t1).total_seconds() / 60))
                except:
                    dur = 0

                trades.append({
                    "id":          buy["id"],
                    "bot":         "MABOUNCER",
                    "pair":        symbol,
                    "direction":   "buy",
                    "pnl":         pnl,
                    "win":         pnl > 0,
                    "date":        buy["date"],
                    "openTime":    buy["time"],
                    "closeTime":   sell["time"],
                    "durationMin": dur,
                    "entry":       entry,
                    "exit":        exit_p,
                    "units":       qty,
                    "rMultiple":   round(1.0 if pnl > 0 else -1.0, 2),
                })
                trade_idx += 1

        # Sort by date desc
        trades.sort(key=lambda t: t["openTime"], reverse=True)

        return jsonify({
            "ok":    True,
            "trades": trades,
            "count": len(trades),
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
