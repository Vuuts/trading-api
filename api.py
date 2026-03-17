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
    """Returns dict of trade_id -> stats from Supabase trades table."""
    try:
        r = requests.get(
            f"{SUPA_URL}/rest/v1/trades?select=*&limit=500",
            headers=SUPA_HDR, timeout=5
        )
        if r.ok:
            return {s["trade_id"]: s for s in r.json()}
    except:
        pass
    return {}

def fetch_supabase_trades(limit=500, status=None, bot=None) -> list:
    """Fetch trades from Supabase with optional filters."""
    try:
        url = f"{SUPA_URL}/rest/v1/trades?select=*&order=opened_at.desc&limit={limit}"
        if status:
            url += f"&status=eq.{status}"
        if bot:
            url += f"&bot=eq.{bot}"
        r = requests.get(url, headers=SUPA_HDR, timeout=5)
        return r.json() if r.ok else []
    except:
        return []

@app.route("/trades/detail/<trade_id>")
def trade_detail(trade_id):
    """Return full data for a single trade including signal metadata."""
    try:
        r = requests.get(
            f"{SUPA_URL}/rest/v1/trades?trade_id=eq.{trade_id}&select=*",
            headers=SUPA_HDR, timeout=5
        )
        if r.ok and r.json():
            return jsonify({"ok": True, "trade": r.json()[0]})
        return jsonify({"ok": False, "error": "Trade not found"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/trades/history")
def trades_history():
    """All trades from Supabase with full signal metadata. Used by Trade Analyst."""
    try:
        bot    = request.args.get("bot")
        status = request.args.get("status")
        limit  = int(request.args.get("limit", 200))
        trades = fetch_supabase_trades(limit=limit, status=status, bot=bot)
        return jsonify({"ok": True, "trades": trades, "count": len(trades)})
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
                # Parse timeframe from client_order_id — format: MA-5M-AAPL-timestamp
                coid = o.get("client_order_id", "")
                tf = "1D"  # default
                if coid.startswith("MA-"):
                    parts = coid.split("-")
                    if len(parts) >= 2:
                        tf = parts[1]  # e.g. 5M, 15M, 1H, 4H, 1D

                by_symbol[o["symbol"]].append({
                    "side":      o["side"],
                    "qty":       float(o.get("filled_qty", 0)),
                    "price":     float(o.get("filled_avg_price", 0)),
                    "date":      date_str,
                    "time":      o.get("filled_at") or o.get("submitted_at", ""),
                    "id":        o["id"],
                    "timeframe": tf,
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
                    "timeframe":   buy.get("timeframe", "1D"),
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

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ─── CLAUDE PROXY ─────────────────────────────────────────────────────────────
@app.route("/claude", methods=["POST", "OPTIONS"])
def claude_proxy():
    """
    Proxy Claude API calls from the browser.
    Accepts: { "system": "...", "messages": [...], "max_tokens": N }
    Returns: Anthropic API response
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        body = request.get_json()
        if not body:
            return jsonify({"error": "No JSON body"}), 400

        headers = {
            "x-api-key":         ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }
        payload = {
            "model":      body.get("model", "claude-sonnet-4-20250514"),
            "max_tokens": body.get("max_tokens", 1000),
            "messages":   body.get("messages", []),
        }
        if body.get("system"):
            payload["system"] = body["system"]

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers, json=payload, timeout=60
        )
        return jsonify(r.json()), r.status_code

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── BACKTEST HELPERS ────────────────────────────────────────────────────────
import math

def bt_get_candles(pair, granularity, count=500, date_from=None):
    """Fetch historical candles from OANDA for backtesting."""
    headers = {
        "Authorization": f"Bearer {OANDA_TOKEN}",
        "Content-Type": "application/json"
    }
    url = f"{OANDA_BASE}/v3/instruments/{pair}/candles?granularity={granularity}&count={count}&price=M"
    if date_from:
        url += f"&from={date_from}"
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    candles = [c for c in r.json().get("candles", []) if c.get("complete", True)]
    return candles

def bt_candles_to_ohlc(candles):
    opens  = [float(c["mid"]["o"]) for c in candles]
    highs  = [float(c["mid"]["h"]) for c in candles]
    lows   = [float(c["mid"]["l"]) for c in candles]
    closes = [float(c["mid"]["c"]) for c in candles]
    times  = [c["time"][:16] for c in candles]
    return opens, highs, lows, closes, times

PIP_MAP = {
    "USD_JPY":0.01,"EUR_JPY":0.01,"GBP_JPY":0.01,"AUD_JPY":0.01,
    "NZD_JPY":0.01,"CAD_JPY":0.01,"CHF_JPY":0.01,
}

def get_pip(pair):
    return PIP_MAP.get(pair, 0.0001)

def bt_calc_bb(closes, period, dev):
    upper, mid, lower = [], [], []
    for i in range(len(closes)):
        if i < period - 1:
            upper.append(None); mid.append(None); lower.append(None)
        else:
            w = closes[i-period+1:i+1]
            m = sum(w) / period
            s = math.sqrt(sum((x-m)**2 for x in w) / period)
            mid.append(m)
            upper.append(m + dev*s)
            lower.append(m - dev*s)
    return upper, mid, lower

def bt_calc_rsi_ewm(closes, period):
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    vals = [None] * period
    gains = [max(closes[i]-closes[i-1], 0) for i in range(1, period+1)]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, period+1)]
    ag = sum(gains)/period; al = sum(losses)/period
    vals.append(100 - 100/(1 + ag/al) if al > 0 else 100.0)
    alpha = 1/period
    for i in range(period+1, len(closes)):
        g = max(closes[i]-closes[i-1], 0)
        l = max(closes[i-1]-closes[i], 0)
        ag = alpha*g + (1-alpha)*ag
        al = alpha*l + (1-alpha)*al
        vals.append(100 - 100/(1 + ag/al) if al > 0 else 100.0)
    return vals

def bt_calc_rsi_bb(rsi_vals, period=34, std=2.0):
    result = []
    for i in range(len(rsi_vals)):
        if rsi_vals[i] is None or i < period - 1:
            result.append((None, None, None))
            continue
        w = [v for v in rsi_vals[i-period+1:i+1] if v is not None]
        if len(w) < period:
            result.append((None, None, None))
            continue
        m = sum(w)/len(w)
        s = math.sqrt(sum((x-m)**2 for x in w)/len(w))
        result.append((m - std*s, m, m + std*s))
    return result

def bt_simulate_trade(direction, entry, sl, tp1, tp2, candles_after, pip):
    """
    Walk forward through candles after entry.
    Returns: (outcome, exit_price, bars_held, tp1_hit)
    outcome: "tp1", "tp2", "sl", "open"
    """
    tp1_hit = False
    for i, c in enumerate(candles_after):
        h = float(c["mid"]["h"])
        l = float(c["mid"]["l"])
        if direction == "sell":
            if l <= tp1 and not tp1_hit:
                tp1_hit = True
            if tp1_hit and l <= tp2:
                return "tp2", tp2, i+1, True
            if h >= sl:
                return "sl", sl, i+1, tp1_hit
        else:
            if h >= tp1 and not tp1_hit:
                tp1_hit = True
            if tp1_hit and h >= tp2:
                return "tp2", tp2, i+1, True
            if l <= sl:
                return "sl", sl, i+1, tp1_hit
    return "open", float(candles_after[-1]["mid"]["c"]), len(candles_after), tp1_hit

# ─── SHARKFIN BACKTEST ───────────────────────────────────────────────────────
@app.route("/backtest/sharkfin", methods=["GET"])
def backtest_sharkfin():
    """
    Backtest Sharkfin strategy on historical M3 candles.
    Params: pair (default EUR_USD), count (default 500)
    """
    try:
        pair       = request.args.get("pair", "EUR_USD")
        count      = int(request.args.get("count", 500))
        bb_period  = int(request.args.get("bb_period", 34))
        bb_dev25   = float(request.args.get("bb_dev25", 2.5))
        bb_dev30   = float(request.args.get("bb_dev30", 3.0))
        rsi_period = int(request.args.get("rsi_period", 13))
        rsi_ob     = float(request.args.get("rsi_ob", 75))
        rsi_os     = float(request.args.get("rsi_os", 25))
        rsi_ext_hi = float(request.args.get("rsi_ext_hi", 93))
        rsi_ext_lo = float(request.args.get("rsi_ext_lo", 7))
        target_pips = float(request.args.get("target_pips", 5))
        stop_pips   = float(request.args.get("stop_pips", 15))
        tdi_period  = int(request.args.get("tdi_period", 34))
        tdi_std     = float(request.args.get("tdi_std", 2.0))

        candles = bt_get_candles(pair, "M3", count)
        if len(candles) < bb_period + 50:
            return jsonify({"ok": False, "error": "Not enough candles"}), 400

        # Fetch M5 candles — need 3x the count since M5 covers less time than M3
        candles5 = bt_get_candles(pair, "M5", min(count * 2, 5000))
        _, _h5, _l5, c5, t5 = bt_candles_to_ohlc(candles5) if candles5 else ([],[],[],[],[])
        u25_5, _, l25_5 = bt_calc_bb(c5, bb_period, bb_dev25) if len(c5) > bb_period else ([],[],[])
        rsi5_vals = bt_calc_rsi_ewm(c5, rsi_period) if c5 else []

        _, highs, lows, closes, times = bt_candles_to_ohlc(candles)
        pip = get_pip(pair)

        u25, mid, l25 = bt_calc_bb(closes, bb_period, bb_dev25)
        u30, _,   l30 = bt_calc_bb(closes, bb_period, bb_dev30)
        rsi_vals      = bt_calc_rsi_ewm(closes, rsi_period)
        tdi_bands     = bt_calc_rsi_bb(rsi_vals, tdi_period, tdi_std)

        def bt_flare(cls, period, dev, lookback=3, max_growth=0.15):
            if len(cls) < period + lookback + 2: return True
            u_n, _, l_n = bt_calc_bb(cls, period, dev)
            u_p, _, l_p = bt_calc_bb(cls[:-lookback], period, dev)
            w_n = (u_n[-1] - l_n[-1]) if u_n[-1] else 0
            w_p = (u_p[-1] - l_p[-1]) if u_p[-1] else 0
            if w_p == 0: return True
            return (w_n - w_p) / w_p > max_growth

        def bt_squeeze(cls, period, dev, ratio=0.003):
            u, m, l = bt_calc_bb(cls, period, dev)
            if u[-1] is None or m[-1] is None or m[-1] == 0: return False
            pw = [(u[j]-l[j])/m[j] for j in range(-15,-5) if u[j] is not None and m[j]]
            if not pw: return False
            was_sq = min(pw) < ratio * 1.5
            curr_w = (u[-1]-l[-1])/m[-1]
            return was_sq and curr_w > (sum(pw)/len(pw)) * 1.8

        def bt_5m_conf(direction, m3_time):
            if not c5 or not t5: return True
            # Find closest M5 candle to the M3 signal time (within 10 min)
            m3_ts = m3_time[:16]
            best_idx = None
            best_diff = 999
            for idx, t in enumerate(t5):
                # Compare time strings directly (both ISO format YYYY-MM-DDTHH:MM)
                diff = abs(ord(t[11])*60 + ord(t[14]) - ord(m3_ts[11])*60 - ord(m3_ts[14]))
                if t[:10] == m3_ts[:10] and diff < best_diff:
                    best_diff = diff
                    best_idx = idx
            # Fall back to first M5 on same day if no close match
            if best_idx is None:
                for idx, t in enumerate(t5):
                    if t[:10] == m3_ts[:10]:
                        best_idx = idx
                        break
            if best_idx is None or best_idx < bb_period + 5:
                return True  # no M5 data for this time — don't block the signal
            r5 = rsi5_vals[best_idx] if best_idx < len(rsi5_vals) else None
            if r5 is None: return True
            u5v = u25_5[best_idx] if best_idx < len(u25_5) else None
            l5v = l25_5[best_idx] if best_idx < len(l25_5) else None
            c5v = c5[best_idx]
            if bt_flare(c5[:best_idx+1], bb_period, bb_dev30): return False
            rsi5_ok   = (direction=="sell" and r5 > rsi_ob) or (direction=="buy" and r5 < rsi_os)
            price5_ok = (direction=="sell" and u5v and c5v >= u5v) or (direction=="buy" and l5v and c5v <= l5v)
            return rsi5_ok or price5_ok

        trades = []
        in_trade_until = -1
        cooldown_until = -1

        for i in range(bb_period + tdi_period + 5, len(closes) - 20):
            if i <= in_trade_until or i <= cooldown_until:
                continue
            if u25[i] is None or rsi_vals[i] is None:
                continue

            rsi = rsi_vals[i]
            tdi_lo, _, tdi_hi = tdi_bands[i]

            # Direction
            if rsi > rsi_ob:
                direction = "sell"
            elif rsi < rsi_os:
                direction = "buy"
            else:
                continue

            # TDI check — RSI must poke outside TDI bands
            if tdi_lo is None:
                continue
            if direction == "sell" and rsi <= tdi_hi:
                continue
            if direction == "buy"  and rsi >= tdi_lo:
                continue

            # RSI extreme filter
            if rsi > rsi_ext_hi or rsi < rsi_ext_lo:
                continue

            # Price at 2.5 band
            if direction == "sell" and closes[i] < u25[i]:
                continue
            if direction == "buy"  and closes[i] > l25[i]:
                continue

            # Not far outside 3.0 band
            if direction == "sell" and closes[i] > u30[i] * 1.001:
                continue
            if direction == "buy"  and closes[i] < l30[i] * 0.999:
                continue

            # Flare guard
            if bt_flare(closes[:i+1], bb_period, bb_dev30):
                continue

            # Squeeze check
            if bt_squeeze(closes[:i+1], bb_period, bb_dev25):
                continue

            # 5m double confirmation
            if not bt_5m_conf(direction, times[i]):
                continue

            # Entry
            entry = closes[i]
            spread = pip * 1.5  # estimated spread
            candle_h = highs[i]
            candle_l = lows[i]

            if direction == "sell":
                sl   = round(candle_h + 2*pip + spread*2, 5)
                tp1  = round(entry - target_pips*pip - spread, 5)
                tp2  = mid[i] + spread if mid[i] else tp1  # midline
            else:
                sl   = round(candle_l - 2*pip - spread*2, 5)
                tp1  = round(entry + target_pips*pip + spread, 5)
                tp2  = mid[i] - spread if mid[i] else tp1

            risk_pips = abs(entry - sl) / pip

            # Simulate trade on next candles
            outcome, exit_price, bars, tp1_hit = bt_simulate_trade(
                direction, entry, sl, tp1, tp2, candles[i+1:i+100], pip
            )

            # P&L — split position 60/40
            pos1_pnl = (tp1 - entry if direction=="buy" else entry - tp1) / pip * 0.6
            pos2_pnl = (exit_price - entry if direction=="buy" else entry - exit_price) / pip * 0.4

            if outcome == "sl":
                pnl_pips = -(risk_pips)
            elif outcome == "tp1":
                pnl_pips = target_pips * 0.6 - risk_pips * 0.4
            elif outcome == "tp2":
                pnl_pips = target_pips * 0.6 + abs(tp2 - entry) / pip * 0.4
            else:
                pnl_pips = (exit_price - entry) / pip * (1 if direction=="buy" else -1)

            trades.append({
                "i": i, "time": times[i], "pair": pair,
                "direction": direction, "entry": round(entry, 5),
                "sl": round(sl, 5), "tp1": round(tp1, 5), "tp2": round(tp2, 5),
                "exit": round(exit_price, 5), "outcome": outcome,
                "bars": bars, "rsi": round(rsi, 1),
                "pnl_pips": round(pnl_pips, 2),
                "risk_pips": round(risk_pips, 1),
                "r_multiple": round(pnl_pips / risk_pips, 2) if risk_pips > 0 else 0,
                "tp1_hit": tp1_hit,
            })
            in_trade_until = i + bars
            cooldown_until = i + bars + 30

        # Stats
        wins   = [t for t in trades if t["pnl_pips"] > 0]
        losses = [t for t in trades if t["pnl_pips"] <= 0]
        total_pips = sum(t["pnl_pips"] for t in trades)
        wr = len(wins)/len(trades)*100 if trades else 0
        avg_win  = sum(t["pnl_pips"] for t in wins)/len(wins) if wins else 0
        avg_loss = sum(t["pnl_pips"] for t in losses)/len(losses) if losses else 0
        pf = abs(avg_win * len(wins) / (avg_loss * len(losses))) if losses and avg_loss != 0 else 999

        # Equity curve
        equity = []
        running = 0
        for t in trades:
            running += t["pnl_pips"]
            equity.append(round(running, 2))

        # Max drawdown
        peak = 0; max_dd = 0; running2 = 0
        for t in trades:
            running2 += t["pnl_pips"]
            if running2 > peak: peak = running2
            dd = peak - running2
            if dd > max_dd: max_dd = dd

        return jsonify({
            "ok": True, "pair": pair, "strategy": "SHARKFIN",
            "candles_analyzed": len(candles),
            "params": {
                "bb_period": bb_period, "bb_dev25": bb_dev25, "bb_dev30": bb_dev30,
                "rsi_period": rsi_period, "rsi_ob": rsi_ob, "rsi_os": rsi_os,
                "rsi_ext_hi": rsi_ext_hi, "rsi_ext_lo": rsi_ext_lo,
                "target_pips": target_pips, "stop_pips": stop_pips,
            },
            "stats": {
                "total_trades": len(trades),
                "wins": len(wins), "losses": len(losses),
                "win_rate": round(wr, 1),
                "total_pips": round(total_pips, 2),
                "avg_win_pips": round(avg_win, 2),
                "avg_loss_pips": round(avg_loss, 2),
                "profit_factor": round(pf, 2),
                "max_drawdown_pips": round(max_dd, 2),
                "tp1_hit_rate": round(len([t for t in trades if t["tp1_hit"]])/len(trades)*100, 1) if trades else 0,
                "tp2_hit_rate": round(len([t for t in trades if t["outcome"]=="tp2"])/len(trades)*100, 1) if trades else 0,
            },
            "equity": equity,
            "trades": trades,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── HARMONIC BACKTEST ───────────────────────────────────────────────────────
@app.route("/backtest/harmonic", methods=["GET"])
def backtest_harmonic():
    """
    Backtest Harmonic pattern detection on H1 or H4.
    Params: pair, granularity (H1/H4), count
    """
    try:
        pair        = request.args.get("pair", "EUR_USD")
        granularity = request.args.get("granularity", "H1")
        count       = int(request.args.get("count", 500))
        fib_tol     = float(request.args.get("fib_tol", 0.02))
        pivot_lb    = int(request.args.get("pivot_lookback", 3))
        min_leg     = int(request.args.get("min_leg_bars", 5))
        min_pattern = int(request.args.get("min_pattern_bars", 30))
        sl_buf_pips = float(request.args.get("sl_buffer_pips", 5))
        patterns_filter = request.args.get("patterns", "all")

        candles = bt_get_candles(pair, granularity, count)
        if len(candles) < min_pattern + 20:
            return jsonify({"ok": False, "error": "Not enough candles"}), 400

        pip = get_pip(pair)
        _, highs_all, lows_all, closes_all, times_all = bt_candles_to_ohlc(candles)

        def fib_ok(ratio, target):
            return abs(ratio - target) <= fib_tol

        def fib_range(ratio, lo, hi):
            return (lo - fib_tol) <= ratio <= (hi + fib_tol)

        def retrace(start, end, pt):
            leg = abs(end - start)
            return abs(pt - end) / leg if leg > 0 else 0

        def extend(start, end, pt):
            leg = abs(end - start)
            return abs(pt - end) / leg if leg > 0 else 0

        def find_pivots(highs, lows, lb):
            pivots = []
            n = len(highs)
            for i in range(lb, n - lb):
                if all(highs[i] >= highs[i-j] for j in range(1,lb+1)) and                    all(highs[i] >= highs[i+j] for j in range(1,lb+1)):
                    pivots.append((i, highs[i], "H"))
                if all(lows[i] <= lows[i-j] for j in range(1,lb+1)) and                    all(lows[i] <= lows[i+j] for j in range(1,lb+1)):
                    pivots.append((i, lows[i], "L"))
            # Clean consecutive same-type
            clean = []
            for p in sorted(pivots, key=lambda x: x[0]):
                if clean and clean[-1][2] == p[2]:
                    if (p[2]=="H" and p[1]>=clean[-1][1]) or (p[2]=="L" and p[1]<=clean[-1][1]):
                        clean[-1] = p
                else:
                    clean.append(p)
            return clean

        def check_pattern(X, A, B, C, D, name):
            bxa = retrace(X, A, B)
            cab = retrace(A, B, C)
            dxa_r = retrace(X, A, D)
            dxa_e = extend(A, X, D)
            dbc = extend(B, C, D)
            cxa_e = extend(X, A, C)
            dxc_r = retrace(X, C, D)

            if name == "GARTLEY":
                return fib_ok(bxa,0.618) and fib_range(cab,0.382,0.886) and fib_ok(dxa_r,0.786) and fib_range(dbc,1.27,1.618)
            elif name == "BAT":
                return fib_range(bxa,0.382,0.500) and fib_range(cab,0.382,0.886) and fib_ok(dxa_r,0.886) and fib_range(dbc,1.618,2.618)
            elif name == "BUTTERFLY":
                return fib_ok(bxa,0.786) and fib_range(cab,0.382,0.886) and fib_range(dxa_e,1.27,1.618) and fib_range(dbc,1.618,2.618)
            elif name == "CRAB":
                return fib_range(bxa,0.382,0.618) and fib_range(cab,0.382,0.886) and fib_ok(dxa_e,1.618) and fib_range(dbc,2.618,3.618)
            elif name == "CYPHER":
                return fib_range(bxa,0.382,0.618) and fib_range(cxa_e,1.272,1.414) and fib_ok(dxc_r,0.786)
            return False

        def check_shark(O, X, A, B, C):
            ab_xa = extend(X, A, B)
            bc_ox = retrace(O, X, C)
            return fib_range(ab_xa,1.13,1.618) and fib_range(bc_ox,0.886,1.13)

        def calc_targets(pattern, direction, X, A, B, C, D, spread):
            is_bull = direction == "bull"
            sl = round(X - sl_buf_pips*pip - spread, 5) if is_bull else round(X + sl_buf_pips*pip + spread, 5)
            if pattern in ("GARTLEY","BAT"):
                tp1 = C; tp2 = A
            elif pattern in ("BUTTERFLY","CRAB"):
                tp1 = B
                bc = abs(C - B)
                tp2 = D + bc*1.618 if is_bull else D - bc*1.618
            else:  # SHARK, CYPHER
                xa = abs(A - X)
                tp1 = D + xa*0.50 if is_bull else D - xa*0.50
                tp2 = D + xa*0.886 if is_bull else D - xa*0.886
            if is_bull:
                tp1 = round(tp1 - spread, 5); tp2 = round(tp2 - spread, 5)
            else:
                tp1 = round(tp1 + spread, 5); tp2 = round(tp2 + spread, 5)
            return sl, tp1, tp2

        PATTERNS = ["GARTLEY","BAT","BUTTERFLY","CRAB","CYPHER"]
        pf_filter = None if patterns_filter == "all" else patterns_filter.upper().split(",")

        trades = []
        seen = set()
        in_trade_until_h = -1

        # Walk forward — for each candle i, build swing list from candles[0:i]
        # Use stride to speed up — check every 5 candles
        stride = 3
        for i in range(min_pattern + 20, len(candles) - 20, stride):
            if i <= in_trade_until_h:
                continue
            highs_sub  = highs_all[:i]
            lows_sub   = lows_all[:i]
            pivots     = find_pivots(highs_sub, lows_sub, pivot_lb)
            if len(pivots) < 5:
                continue

            for pi in range(len(pivots)-4):
                pts = pivots[pi:pi+5]
                types = [p[2] for p in pts]
                if not all(types[j] != types[j+1] for j in range(4)):
                    continue

                idxs = [p[0] for p in pts]
                prices = [p[1] for p in pts]
                X,A,B,C,D = prices

                full_span = idxs[4] - idxs[0]
                if full_span < min_pattern:
                    continue
                if any(idxs[j+1]-idxs[j] < min_leg for j in range(4)):
                    continue
                # D must be the most recently completed pivot
                if pi + 4 < len(pivots) - 2:
                    continue

                is_bull = types[0]=="L" and types[1]=="H"
                is_bear = types[0]=="H" and types[1]=="L"
                if not (is_bull or is_bear):
                    continue
                direction = "bull" if is_bull else "bear"

                for name in PATTERNS:
                    if pf_filter and name not in pf_filter:
                        continue
                    if not check_pattern(X,A,B,C,D,name):
                        continue
                    sig_key = (idxs[0], idxs[4], name, direction)
                    if sig_key in seen:
                        continue
                    seen.add(sig_key)

                    entry = D
                    spread = pip * 1.5
                    sl, tp1, tp2 = calc_targets(name, direction, X, A, B, C, D, spread)
                    risk_pips = abs(entry - sl) / pip
                    if risk_pips <= 0 or risk_pips > 500:
                        continue

                    outcome, exit_price, bars, tp1_hit = bt_simulate_trade(
                        direction, entry, sl, tp1, tp2, candles[idxs[4]+1:idxs[4]+200], pip
                    )

                    if outcome == "sl":
                        pnl_pips = -risk_pips
                    elif outcome == "tp2":
                        pnl_pips = abs(tp1-entry)/pip*0.6 + abs(tp2-entry)/pip*0.4
                        if direction=="sell": pnl_pips = abs(entry-tp1)/pip*0.6 + abs(entry-tp2)/pip*0.4
                    elif outcome == "tp1":
                        pnl_pips = abs(tp1-entry)/pip*0.6 - risk_pips*0.4
                    else:
                        cur_pnl = (exit_price - entry)/pip if direction=="buy" else (entry - exit_price)/pip
                        pnl_pips = cur_pnl

                    trades.append({
                        "time": times_all[idxs[4]], "pair": pair,
                        "pattern": name, "direction": direction,
                        "entry": round(entry,5), "sl": round(sl,5),
                        "tp1": round(tp1,5), "tp2": round(tp2,5),
                        "X": round(X,5), "A": round(A,5), "B": round(B,5),
                        "C": round(C,5), "D": round(D,5),
                        "exit": round(exit_price,5), "outcome": outcome,
                        "bars": bars, "risk_pips": round(risk_pips,1),
                        "pnl_pips": round(pnl_pips,2),
                        "r_multiple": round(pnl_pips/risk_pips,2) if risk_pips>0 else 0,
                        "tp1_hit": tp1_hit, "full_span": full_span,
                    })
                    in_trade_until_h = idxs[4] + bars + 5

        # Shark scan
        if not pf_filter or "SHARK" in pf_filter:
            for i in range(min_pattern+20, len(candles)-20, stride):
                highs_sub = highs_all[:i]; lows_sub = lows_all[:i]
                pivots = find_pivots(highs_sub, lows_sub, pivot_lb)
                if len(pivots) < 5: continue
                for pi in range(len(pivots)-4):
                    pts = pivots[pi:pi+5]
                    types = [p[2] for p in pts]
                    if not all(types[j]!=types[j+1] for j in range(4)): continue
                    idxs = [p[0] for p in pts]; prices = [p[1] for p in pts]
                    O,X,A,B,C = prices
                    full_span = idxs[4]-idxs[0]
                    if full_span < min_pattern: continue
                    if any(idxs[j+1]-idxs[j]<min_leg for j in range(4)): continue
                    if pi+4 < len(pivots)-2: continue
                    is_bull = types[0]=="L" and types[1]=="H"
                    direction = "bull" if is_bull else "bear"
                    if not check_shark(O,X,A,B,C): continue
                    sig_key = (idxs[0],idxs[4],"SHARK",direction)
                    if sig_key in seen: continue
                    seen.add(sig_key)
                    entry=C; spread=pip*1.5
                    xa=abs(X-O); tp1=C+xa*0.50 if is_bull else C-xa*0.50; tp2=C+xa*0.886 if is_bull else C-xa*0.886
                    sl=round(O-sl_buf_pips*pip-spread,5) if is_bull else round(O+sl_buf_pips*pip+spread,5)
                    tp1=round(tp1-spread,5) if is_bull else round(tp1+spread,5)
                    tp2=round(tp2-spread,5) if is_bull else round(tp2+spread,5)
                    risk_pips=abs(entry-sl)/pip
                    if risk_pips<=0 or risk_pips>500: continue
                    outcome,exit_price,bars,tp1_hit = bt_simulate_trade(direction,entry,sl,tp1,tp2,candles[idxs[4]+1:idxs[4]+200],pip)
                    pnl_pips = -risk_pips if outcome=="sl" else (abs(tp2-entry)/pip if outcome=="tp2" else (abs(exit_price-entry)/pip*(1 if is_bull else -1)))
                    trades.append({
                        "time":times_all[idxs[4]],"pair":pair,"pattern":"SHARK","direction":direction,
                        "entry":round(entry,5),"sl":round(sl,5),"tp1":round(tp1,5),"tp2":round(tp2,5),
                        "X":round(O,5),"A":round(X,5),"B":round(A,5),"C":round(B,5),"D":round(C,5),
                        "exit":round(exit_price,5),"outcome":outcome,"bars":bars,
                        "risk_pips":round(risk_pips,1),"pnl_pips":round(pnl_pips,2),
                        "r_multiple":round(pnl_pips/risk_pips,2) if risk_pips>0 else 0,
                        "tp1_hit":tp1_hit,"full_span":full_span,
                    })

        trades.sort(key=lambda t: t["time"])

        wins   = [t for t in trades if t["pnl_pips"]>0]
        losses = [t for t in trades if t["pnl_pips"]<=0]
        total_pips = sum(t["pnl_pips"] for t in trades)
        wr = len(wins)/len(trades)*100 if trades else 0
        avg_win  = sum(t["pnl_pips"] for t in wins)/len(wins) if wins else 0
        avg_loss = sum(t["pnl_pips"] for t in losses)/len(losses) if losses else 0
        pf = abs(avg_win*len(wins)/(avg_loss*len(losses))) if losses and avg_loss!=0 else 999

        equity=[]; running=0
        for t in trades:
            running+=t["pnl_pips"]; equity.append(round(running,2))

        peak=0; max_dd=0; running2=0
        for t in trades:
            running2+=t["pnl_pips"]
            if running2>peak: peak=running2
            dd=peak-running2
            if dd>max_dd: max_dd=dd

        # Pattern breakdown
        pat_map={}
        for t in trades:
            p=t["pattern"]
            if p not in pat_map: pat_map[p]={"wins":0,"count":0,"pips":0}
            pat_map[p]["count"]+=1
            if t["pnl_pips"]>0: pat_map[p]["wins"]+=1
            pat_map[p]["pips"]+=t["pnl_pips"]
        pat_stats=[{"pattern":k,"count":v["count"],"wr":round(v["wins"]/v["count"]*100,1),
                    "pips":round(v["pips"],2)} for k,v in pat_map.items()]

        return jsonify({
            "ok":True,"pair":pair,"strategy":"HARMONIC","granularity":granularity,
            "candles_analyzed":len(candles),
            "params":{"fib_tol":fib_tol,"pivot_lookback":pivot_lb,
                      "min_leg_bars":min_leg,"min_pattern_bars":min_pattern,
                      "sl_buffer_pips":sl_buf_pips},
            "stats":{
                "total_trades":len(trades),"wins":len(wins),"losses":len(losses),
                "win_rate":round(wr,1),"total_pips":round(total_pips,2),
                "avg_win_pips":round(avg_win,2),"avg_loss_pips":round(avg_loss,2),
                "profit_factor":round(pf,2),"max_drawdown_pips":round(max_dd,2),
                "tp1_hit_rate":round(len([t for t in trades if t["tp1_hit"]])/len(trades)*100,1) if trades else 0,
                "tp2_hit_rate":round(len([t for t in trades if t["outcome"]=="tp2"])/len(trades)*100,1) if trades else 0,
            },
            "pattern_breakdown":pat_stats,
            "equity":equity,
            "trades":trades,
        })
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

# ─── DIVERGE BACKTEST ────────────────────────────────────────────────────────
@app.route("/backtest/diverge", methods=["GET"])
def backtest_diverge():
    """
    Backtest DIVERGE strategy -- identical filters to live bot.
    Flare guard, 3m squeeze (hard), HTF squeeze (soft/logged),
    sharkfin demotion, SMA retest (soft), state expiry.
    """
    try:
        pair        = request.args.get("pair", "EUR_USD")
        count       = int(request.args.get("count", 500))
        bb_period   = int(request.args.get("bb_period", 20))
        bb_entry    = float(request.args.get("bb_entry", 2.5))
        bb_outer    = float(request.args.get("bb_outer", 3.0))
        rsi_period  = int(request.args.get("rsi_period", 14))
        sl_buf      = float(request.args.get("sl_buffer_pips", 13))
        sma_period  = int(request.args.get("sma_period", 8))
        state_max   = int(request.args.get("state_max_candles", 60))
        require_sma = request.args.get("require_sma", "false").lower() == "true"
        flare_lb    = 3
        flare_max   = 0.15
        htf_sq_pct  = 0.003
        tdi_period  = 34
        tdi_std     = 2.0
        rsi_ob      = 75.0
        rsi_os      = 25.0

        candles = bt_get_candles(pair, "M3", count)
        if len(candles) < bb_period + rsi_period + sma_period + 20:
            return jsonify({"ok": False, "error": "Not enough candles"}), 400

        # HTF H1 candles for soft squeeze check
        candles_h1 = bt_get_candles(pair, "H1", 30)
        htf_squeezed_global = False
        if candles_h1 and len(candles_h1) >= 20:
            _, _, _, c_h1, _ = bt_candles_to_ohlc(candles_h1)
            u_h1, _, l_h1 = bt_calc_bb(c_h1, 20, 2.0)
            if u_h1[-1] and c_h1[-1] > 0:
                htf_squeezed_global = (u_h1[-1] - l_h1[-1]) / c_h1[-1] < htf_sq_pct

        _, highs, lows, closes, times = bt_candles_to_ohlc(candles)
        pip = get_pip(pair)

        # ── Indicator helpers ─────────────────────────────────────────────
        def dv_calc_bb_width(cls, period, dev, offset=0):
            sub = cls[:-offset] if offset else cls
            lo, _, hi = bt_calc_bb(sub, period, dev)
            return (hi[-1] - lo[-1]) if hi[-1] is not None else None

        def dv_flare(cls):
            if len(cls) < bb_period + flare_lb + 2: return True
            w_now  = dv_calc_bb_width(cls, bb_period, bb_outer, 0)
            w_past = dv_calc_bb_width(cls, bb_period, bb_outer, flare_lb)
            if not w_now or not w_past or w_past == 0: return True
            return (w_now - w_past) / w_past > flare_max

        def dv_3m_squeeze(cls):
            u, _, l = bt_calc_bb(cls, bb_period, bb_entry)
            if u[-1] is None or cls[-1] == 0: return False
            return (u[-1] - l[-1]) / cls[-1] < 0.0015

        def dv_rsi_simple(cls):
            if len(cls) < rsi_period + 1: return None
            gains = [max(cls[-(rsi_period+1)+i] - cls[-(rsi_period+2)+i], 0) for i in range(1, rsi_period+1)]
            losses= [max(cls[-(rsi_period+2)+i] - cls[-(rsi_period+1)+i], 0) for i in range(1, rsi_period+1)]
            ag = sum(gains)/rsi_period; al = sum(losses)/rsi_period
            if al == 0: return 100.0
            return 100 - 100/(1 + ag/al)

        def dv_tdi_bb(cls):
            if len(cls) < rsi_period + tdi_period + 1: return None, None, None
            lookback = tdi_period + rsi_period + 5
            sub = cls[-lookback:] if len(cls) > lookback else cls
            alpha = 1.0 / rsi_period
            ag = sum(max(sub[i]-sub[i-1], 0) for i in range(1, rsi_period+1)) / rsi_period
            al = sum(max(sub[i-1]-sub[i], 0) for i in range(1, rsi_period+1)) / rsi_period
            rsi_ser = []
            for i in range(rsi_period, len(sub)):
                if i > rsi_period:
                    d = sub[i] - sub[i-1]
                    ag = alpha*max(d,0)  + (1-alpha)*ag
                    al = alpha*max(-d,0) + (1-alpha)*al
                rsi_ser.append(100.0 if al==0 else 100 - 100/(1 + ag/al))
            if len(rsi_ser) < tdi_period: return None, None, None
            w = rsi_ser[-tdi_period:]
            m = sum(w)/tdi_period
            s = math.sqrt(sum((x-m)**2 for x in w)/tdi_period)
            return m - tdi_std*s, m, m + tdi_std*s

        def dv_is_sharkfin(cls, rsi_val):
            lo, _, hi = dv_tdi_bb(cls)
            if lo is None: return False
            return (rsi_val > rsi_ob and rsi_val > hi) or (rsi_val < rsi_os and rsi_val < lo)

        def dv_sma(cls):
            if len(cls) < sma_period: return None
            return sum(cls[-sma_period:]) / sma_period

        # ── Walk-forward state machine ────────────────────────────────────
        trades = []
        in_trade_until = -1
        cooldown_until = -1
        ds = {}

        start_i = bb_period + rsi_period + tdi_period + sma_period + 10

        for i in range(start_i, len(closes) - 20):
            if i <= in_trade_until or i <= cooldown_until:
                ds = {}
                continue

            cls  = closes[:i+1]
            u25_all, mid_all, l25_all = bt_calc_bb(cls, bb_period, bb_entry)
            if u25_all[-1] is None: continue

            cur_c = closes[i]; cur_h = highs[i]; cur_l = lows[i]
            u25 = u25_all[-1]; l25 = l25_all[-1]; mid = mid_all[-1]
            rsi  = dv_rsi_simple(cls)
            sma8 = dv_sma(cls)
            if rsi is None or sma8 is None: continue

            # ── Phase 1: Touch1 ──────────────────────────────────────────
            if not ds:
                if dv_flare(cls): continue
                if dv_3m_squeeze(cls): continue
                if cur_c >= u25:
                    ds = {"direction":"BEAR","touch1_price":cur_h,"touch1_rsi":rsi,
                          "retested_sma":False,"sma_crossed":False,"candle_count":0}
                elif cur_c <= l25:
                    ds = {"direction":"BULL","touch1_price":cur_l,"touch1_rsi":rsi,
                          "retested_sma":False,"sma_crossed":False,"candle_count":0}
                continue

            direction = ds["direction"]

            # Stale state guard
            if direction=="BEAR" and cur_c < l25: ds={}; continue
            if direction=="BULL" and cur_c > u25: ds={}; continue

            ds["candle_count"] += 1
            if ds["candle_count"] > state_max: ds={}; continue

            # ── Phase 2: SMA retest (soft) ───────────────────────────────
            if not ds["retested_sma"]:
                if direction=="BEAR" and cur_c < sma8:
                    ds["sma_crossed"]=True; ds["retested_sma"]=True
                elif direction=="BULL" and cur_c > sma8:
                    ds["sma_crossed"]=True; ds["retested_sma"]=True
                if not ds["retested_sma"]:
                    if direction=="BEAR" and cur_c>=u25 and cur_h>ds["touch1_price"]:
                        ds["touch1_price"]=cur_h; ds["touch1_rsi"]=rsi; ds["sma_crossed"]=False; ds["retested_sma"]=False
                    elif direction=="BULL" and cur_c<=l25 and cur_l<ds["touch1_price"]:
                        ds["touch1_price"]=cur_l; ds["touch1_rsi"]=rsi; ds["sma_crossed"]=False; ds["retested_sma"]=False
                    if require_sma: continue

            # ── Phase 3: Touch2 ──────────────────────────────────────────
            fired = False
            if direction=="BEAR" and cur_c>=u25 and rsi<ds["touch1_rsi"]:
                # Sharkfin demotion
                if dv_is_sharkfin(cls, rsi):
                    ds={"direction":"BEAR","touch1_price":cur_h,"touch1_rsi":rsi,
                        "retested_sma":False,"sma_crossed":False,"candle_count":0}
                    continue
                # Hard filters at entry
                if dv_flare(cls): ds={}; continue
                if dv_3m_squeeze(cls): ds={}; continue
                # HTF squeeze -- soft, just log it
                htf_sq = htf_squeezed_global
                spread = pip * 1.5
                sl     = round(cur_h + sl_buf*pip + spread, 5)
                tp1    = round(mid - spread, 5)
                tp2    = round(l25 - spread, 5)
                risk_p = abs(cur_c - sl) / pip
                outcome, exit_price, bars, tp1_hit = bt_simulate_trade(
                    "sell", cur_c, sl, tp1, tp2, candles[i+1:i+150], pip)
                pnl = -risk_p if outcome=="sl" else (
                    abs(tp2-cur_c)/pip if outcome=="tp2" else
                    abs(cur_c-exit_price)/pip if outcome in ("tp1","open") else 0)
                trades.append({
                    "time":times[i],"pair":pair,"pattern":"DIVERGE","direction":"sell",
                    "entry":round(cur_c,5),"sl":round(sl,5),"tp1":round(tp1,5),"tp2":round(tp2,5),
                    "exit":round(exit_price,5),"outcome":outcome,"bars":bars,
                    "risk_pips":round(risk_p,1),"pnl_pips":round(pnl,2),
                    "r_multiple":round(pnl/risk_p,2) if risk_p>0 else 0,
                    "tp1_hit":tp1_hit,"sma_retested":ds.get("retested_sma",False),
                    "htf_squeezed":htf_sq,
                })
                in_trade_until = i + bars
                cooldown_until = i + bars + 30
                ds={}; fired=True

            elif direction=="BULL" and cur_c<=l25 and rsi>ds["touch1_rsi"]:
                if dv_is_sharkfin(cls, rsi):
                    ds={"direction":"BULL","touch1_price":cur_l,"touch1_rsi":rsi,
                        "retested_sma":False,"sma_crossed":False,"candle_count":0}
                    continue
                if dv_flare(cls): ds={}; continue
                if dv_3m_squeeze(cls): ds={}; continue
                htf_sq = htf_squeezed_global
                spread = pip * 1.5
                sl     = round(cur_l - sl_buf*pip - spread, 5)
                tp1    = round(mid + spread, 5)
                tp2    = round(u25 + spread, 5)
                risk_p = abs(cur_c - sl) / pip
                outcome, exit_price, bars, tp1_hit = bt_simulate_trade(
                    "buy", cur_c, sl, tp1, tp2, candles[i+1:i+150], pip)
                pnl = -risk_p if outcome=="sl" else (
                    abs(tp2-cur_c)/pip if outcome=="tp2" else
                    abs(exit_price-cur_c)/pip if outcome in ("tp1","open") else 0)
                trades.append({
                    "time":times[i],"pair":pair,"pattern":"DIVERGE","direction":"buy",
                    "entry":round(cur_c,5),"sl":round(sl,5),"tp1":round(tp1,5),"tp2":round(tp2,5),
                    "exit":round(exit_price,5),"outcome":outcome,"bars":bars,
                    "risk_pips":round(risk_p,1),"pnl_pips":round(pnl,2),
                    "r_multiple":round(pnl/risk_p,2) if risk_p>0 else 0,
                    "tp1_hit":tp1_hit,"sma_retested":ds.get("retested_sma",False),
                    "htf_squeezed":htf_sq,
                })
                in_trade_until = i + bars
                cooldown_until = i + bars + 30
                ds={}; fired=True

        wins   = [t for t in trades if t["pnl_pips"]>0]
        losses = [t for t in trades if t["pnl_pips"]<=0]
        total  = sum(t["pnl_pips"] for t in trades)
        wr     = len(wins)/len(trades)*100 if trades else 0
        avg_w  = sum(t["pnl_pips"] for t in wins)/len(wins) if wins else 0
        avg_l  = sum(t["pnl_pips"] for t in losses)/len(losses) if losses else 0
        pf     = abs(avg_w*len(wins)/(avg_l*len(losses))) if losses and avg_l!=0 else 999

        equity=[]; running=0
        for t in trades: running+=t["pnl_pips"]; equity.append(round(running,2))
        peak=0; max_dd=0; running2=0
        for t in trades:
            running2+=t["pnl_pips"]
            if running2>peak: peak=running2
            dd=peak-running2
            if dd>max_dd: max_dd=dd

        sma_rate = len([t for t in trades if t.get("sma_retested")])/len(trades)*100 if trades else 0
        htf_rate = len([t for t in trades if t.get("htf_squeezed")])/len(trades)*100 if trades else 0

        return jsonify({
            "ok":True,"pair":pair,"strategy":"DIVERGE","granularity":"M3",
            "candles_analyzed":len(candles),
            "params":{"bb_period":bb_period,"bb_entry":bb_entry,"rsi_period":rsi_period,
                      "sl_buffer_pips":sl_buf,"sma_period":sma_period,
                      "require_sma":require_sma,"state_max_candles":state_max},
            "stats":{
                "total_trades":len(trades),"wins":len(wins),"losses":len(losses),
                "win_rate":round(wr,1),"total_pips":round(total,2),
                "avg_win_pips":round(avg_w,2),"avg_loss_pips":round(avg_l,2),
                "profit_factor":round(pf,2),"max_drawdown_pips":round(max_dd,2),
                "tp1_hit_rate":round(len([t for t in trades if t["tp1_hit"]])/len(trades)*100,1) if trades else 0,
                "tp2_hit_rate":round(len([t for t in trades if t["outcome"]=="tp2"])/len(trades)*100,1) if trades else 0,
                "sma_retest_rate":round(sma_rate,1),
                "htf_squeezed_rate":round(htf_rate,1),
            },
            "equity":equity,"trades":trades,
        })
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

# ─── ORB BACKTEST ─────────────────────────────────────────────────────────────
@app.route("/backtest/orb", methods=["GET"])
def backtest_orb():
    """
    Backtest ORB strategy on M5 candles.
    Marks first N candles of each session as the opening range box,
    then enters on breakout with MA50 alignment.
    Sessions: Asia (19:00 ET), NY (08:00 ET)
    """
    try:
        pair         = request.args.get("pair", "EUR_USD")
        count        = int(request.args.get("count", 1000))
        box_candles  = int(request.args.get("box_candles", 3))   # candles to form box (3×5m = 15min)
        risk_reward  = float(request.args.get("risk_reward", 1.0))  # TP1 = 1R
        ma_period    = int(request.args.get("ma_period", 50))
        min_box_pips = float(request.args.get("min_box_pips", 5))
        max_box_pips = float(request.args.get("max_box_pips", 50))
        session_filter = request.args.get("session", "both")  # "asia","ny","both"

        candles = bt_get_candles(pair, "M5", count)
        if len(candles) < ma_period + box_candles + 20:
            return jsonify({"ok": False, "error": "Not enough candles"}), 400

        _, highs, lows, closes, times = bt_candles_to_ohlc(candles)
        pip = get_pip(pair)

        def get_et_hour(ts):
            # ts format: "2026-03-13T14:30"
            try:
                dt = datetime.fromisoformat(ts.replace("Z",""))
                utc_h = dt.hour
                # EDT (UTC-4) Mar-Nov, EST (UTC-5) Nov-Mar
                offset = 4 if 2 <= dt.month <= 11 else 5
                return (utc_h - offset) % 24
            except:
                return -1

        def is_session_open(ts, session):
            h = get_et_hour(ts)
            if session == "asia": return h == 19
            if session == "ny":   return h == 8
            return h in (8, 19)

        def calc_ma50(closes_slice, period):
            if len(closes_slice) < period: return None
            return sum(closes_slice[-period:]) / period

        trades = []
        in_trade_until = -1
        i = ma_period

        while i < len(candles) - box_candles - 30:
            if i <= in_trade_until:
                i += 1
                continue

            ts = times[i]
            sessions = []
            if session_filter in ("asia","both") and get_et_hour(ts) == 19:
                sessions.append("Asia")
            if session_filter in ("ny","both") and get_et_hour(ts) == 8:
                sessions.append("NY")

            if not sessions:
                i += 1
                continue

            session = sessions[0]

            # Form box from next box_candles candles
            box_end = i + box_candles
            if box_end >= len(candles) - 20:
                i += 1
                continue

            box_high = max(highs[i:box_end])
            box_low  = min(lows[i:box_end])
            box_size = (box_high - box_low) / pip

            if box_size < min_box_pips or box_size > max_box_pips:
                i = box_end
                continue

            # Check MA50 alignment at box end
            ma50 = calc_ma50(closes[:box_end], ma_period)
            if ma50 is None:
                i = box_end
                continue

            # Look for breakout candle after box
            broke = False
            for j in range(box_end, min(box_end + 20, len(candles) - 20)):
                c = closes[j]
                h = highs[j]
                l = lows[j]

                # Bullish breakout — close above box high, MA50 below box
                if c > box_high and ma50 < box_high:
                    direction = "buy"
                    entry = box_high
                    sl    = round(box_low - pip, 5)
                    risk_p = (entry - sl) / pip
                    tp1   = round(entry + risk_p * risk_reward * pip, 5)
                    tp2   = round(entry + risk_p * 2.0 * pip, 5)

                    outcome, exit_price, bars, tp1_hit = bt_simulate_trade(
                        "buy", entry, sl, tp1, tp2, candles[j+1:j+100], pip
                    )
                    pnl = -risk_p if outcome=="sl" else (
                        risk_p*risk_reward*0.6 + risk_p*2.0*0.4 if outcome=="tp2" else
                        risk_p*risk_reward*0.6 - risk_p*0.4 if outcome=="tp1" else
                        (exit_price-entry)/pip
                    )
                    trades.append({
                        "time":times[j],"pair":pair,"pattern":"ORB","direction":"buy",
                        "session":session,"entry":round(entry,5),"sl":round(sl,5),
                        "tp1":round(tp1,5),"tp2":round(tp2,5),
                        "box_high":round(box_high,5),"box_low":round(box_low,5),
                        "box_size_pips":round(box_size,1),"ma50":round(ma50,5),
                        "exit":round(exit_price,5),"outcome":outcome,"bars":bars,
                        "risk_pips":round(risk_p,1),"pnl_pips":round(pnl,2),
                        "r_multiple":round(pnl/risk_p,2) if risk_p>0 else 0,
                        "tp1_hit":tp1_hit,
                    })
                    in_trade_until = j + bars
                    broke = True
                    break

                # Bearish breakout — close below box low, MA50 above box
                elif c < box_low and ma50 > box_low:
                    direction = "sell"
                    entry = box_low
                    sl    = round(box_high + pip, 5)
                    risk_p = (sl - entry) / pip
                    tp1   = round(entry - risk_p * risk_reward * pip, 5)
                    tp2   = round(entry - risk_p * 2.0 * pip, 5)

                    outcome, exit_price, bars, tp1_hit = bt_simulate_trade(
                        "sell", entry, sl, tp1, tp2, candles[j+1:j+100], pip
                    )
                    pnl = -risk_p if outcome=="sl" else (
                        risk_p*risk_reward*0.6 + risk_p*2.0*0.4 if outcome=="tp2" else
                        risk_p*risk_reward*0.6 - risk_p*0.4 if outcome=="tp1" else
                        (entry-exit_price)/pip
                    )
                    trades.append({
                        "time":times[j],"pair":pair,"pattern":"ORB","direction":"sell",
                        "session":session,"entry":round(entry,5),"sl":round(sl,5),
                        "tp1":round(tp1,5),"tp2":round(tp2,5),
                        "box_high":round(box_high,5),"box_low":round(box_low,5),
                        "box_size_pips":round(box_size,1),"ma50":round(ma50,5),
                        "exit":round(exit_price,5),"outcome":outcome,"bars":bars,
                        "risk_pips":round(risk_p,1),"pnl_pips":round(pnl,2),
                        "r_multiple":round(pnl/risk_p,2) if risk_p>0 else 0,
                        "tp1_hit":tp1_hit,
                    })
                    in_trade_until = j + bars
                    broke = True
                    break

            i = box_end if not broke else i + 1

        wins   = [t for t in trades if t["pnl_pips"]>0]
        losses = [t for t in trades if t["pnl_pips"]<=0]
        total  = sum(t["pnl_pips"] for t in trades)
        wr     = len(wins)/len(trades)*100 if trades else 0
        avg_w  = sum(t["pnl_pips"] for t in wins)/len(wins) if wins else 0
        avg_l  = sum(t["pnl_pips"] for t in losses)/len(losses) if losses else 0
        pf     = abs(avg_w*len(wins)/(avg_l*len(losses))) if losses and avg_l!=0 else 999

        equity=[]; running=0
        for t in trades: running+=t["pnl_pips"]; equity.append(round(running,2))
        peak=0; max_dd=0; running2=0
        for t in trades:
            running2+=t["pnl_pips"]
            if running2>peak: peak=running2
            dd=peak-running2
            if dd>max_dd: max_dd=dd

        # Session breakdown
        sess_map={}
        for t in trades:
            s=t.get("session","?")
            if s not in sess_map: sess_map[s]={"wins":0,"count":0,"pips":0}
            sess_map[s]["count"]+=1
            if t["pnl_pips"]>0: sess_map[s]["wins"]+=1
            sess_map[s]["pips"]+=t["pnl_pips"]
        sess_stats=[{"pattern":k,"count":v["count"],"wr":round(v["wins"]/v["count"]*100,1),
                     "pips":round(v["pips"],2)} for k,v in sess_map.items()]

        return jsonify({
            "ok":True,"pair":pair,"strategy":"ORB","granularity":"M5",
            "candles_analyzed":len(candles),
            "params":{"box_candles":box_candles,"risk_reward":risk_reward,
                      "ma_period":ma_period,"min_box_pips":min_box_pips,
                      "max_box_pips":max_box_pips,"session":session_filter},
            "stats":{
                "total_trades":len(trades),"wins":len(wins),"losses":len(losses),
                "win_rate":round(wr,1),"total_pips":round(total,2),
                "avg_win_pips":round(avg_w,2),"avg_loss_pips":round(avg_l,2),
                "profit_factor":round(pf,2),"max_drawdown_pips":round(max_dd,2),
                "tp1_hit_rate":round(len([t for t in trades if t["tp1_hit"]])/len(trades)*100,1) if trades else 0,
                "tp2_hit_rate":round(len([t for t in trades if t["outcome"]=="tp2"])/len(trades)*100,1) if trades else 0,
            },
            "pattern_breakdown":sess_stats,
            "equity":equity,"trades":trades,
        })
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
