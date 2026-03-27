"""
Shared Supabase trade stats module.
All 3 bots import this to track max profit/drawdown on open trades.
"""
import requests
import os
from datetime import datetime, timezone

SUPA_URL = os.environ.get("SUPABASE_URL", "https://myetabcvnbltfrupuod.supabase.co")
SUPA_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im15ZXRhYmN2bmJsdGZydXBwdW9kIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjY0NDkwNiwiZXhwIjoyMDg4MjIwOTA2fQ.09Me5NQ-FVvm7w8JGvdNsZbJjHZtrS1EhS2BRb1KgAQ")

HEADERS = {
    "apikey": SUPA_KEY,
    "Authorization": f"Bearer {SUPA_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates"
}

def upsert_trade(trade_id: str, bot: str, pair: str, entry: float,
                 current_price: float, direction: str, session: str = None,
                 opened_at: str = None):
    """
    Called every scan for each open trade.
    Upserts max_profit, max_drawdown, max_price, min_price.
    """
    try:
        # Fetch existing record
        r = requests.get(
            f"{SUPA_URL}/rest/v1/trade_stats?trade_id=eq.{trade_id}&select=*",
            headers=HEADERS, timeout=5
        )
        existing = r.json()[0] if r.ok and r.json() else None

        # Calculate unrealized PnL direction
        if direction == "buy":
            unrealized = current_price - entry
        else:
            unrealized = entry - current_price

        now_str = datetime.now(timezone.utc).isoformat()

        if existing:
            max_profit   = max(float(existing.get("max_profit", 0)), unrealized)
            max_drawdown = min(float(existing.get("max_drawdown", 0)), unrealized)
            max_price    = max(float(existing.get("max_price", 0)), current_price)
            min_price    = min(float(existing.get("min_price", 999999)), current_price)
        else:
            max_profit   = max(0, unrealized)
            max_drawdown = min(0, unrealized)
            max_price    = current_price
            min_price    = current_price

        payload = {
            "trade_id":     str(trade_id),
            "bot":          bot,
            "pair":         pair,
            "entry":        entry,
            "direction":    direction,
            "session":      session,
            "max_profit":   round(max_profit, 6),
            "max_drawdown": round(max_drawdown, 6),
            "max_price":    round(max_price, 6),
            "min_price":    round(min_price, 6),
            "opened_at":    opened_at or now_str,
            "last_updated": now_str,
        }

        requests.post(
            f"{SUPA_URL}/rest/v1/trade_stats",
            headers=HEADERS, json=payload, timeout=5
        )
    except Exception as e:
        print(f"[supabase] upsert_trade failed {trade_id}: {e}")

def close_trade(trade_id: str, final_pnl: float, closed_at: str = None):
    """Call when a trade closes to record final PnL."""
    try:
        now_str = closed_at or datetime.now(timezone.utc).isoformat()
        payload = {"final_pnl": round(final_pnl, 6), "closed_at": now_str}
        requests.patch(
            f"{SUPA_URL}/rest/v1/trade_stats?trade_id=eq.{trade_id}",
            headers=HEADERS, json=payload, timeout=5
        )
    except Exception as e:
        print(f"[supabase] close_trade failed {trade_id}: {e}")

def get_trade_stats(trade_id: str) -> dict:
    """Fetch stats for a single trade."""
    try:
        r = requests.get(
            f"{SUPA_URL}/rest/v1/trade_stats?trade_id=eq.{trade_id}&select=*",
            headers=HEADERS, timeout=5
        )
        if r.ok and r.json():
            return r.json()[0]
    except:
        pass
    return {}

def get_all_stats() -> list:
    """Fetch all trade stats — used by API."""
    try:
        r = requests.get(
            f"{SUPA_URL}/rest/v1/trade_stats?select=*&order=opened_at.desc&limit=500",
            headers=HEADERS, timeout=5
        )
        return r.json() if r.ok else []
    except:
        return []
