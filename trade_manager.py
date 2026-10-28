import json
from datetime import datetime

import streamlit as st
from supabase import create_client

from utils import calculate_atr, get_current_price
from notifications import send_sell_signal_email, send_trade_closed_email


# ============================================================
# DATABASE CONNECTION (cached, one connection per app session)
# ============================================================
@st.cache_resource
def get_db():
    """Create a persistent Supabase connection using Streamlit Secrets."""
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["service_role_key"]
    return create_client(url, key)


# ============================================================
# LOAD / SAVE
# ============================================================
def load_trades():
    """Load all trades from the external database."""
    try:
        result = (
            get_db()
            .table("trades")
            .select("payload")
            .order("id")
            .execute()
        )
        return [row["payload"] for row in result.data]
    except Exception as e:
        st.error(f"Database connection failed: {e}")
        return []


def save_trade(trade):
    """Insert or update a single trade (safe, no overwrite of other trades)."""
    try:
        get_db().table("trades").upsert(
            {"id": int(trade["id"]), "payload": trade}
        ).execute()
    except Exception as e:
        st.error(f"Could not save trade {trade.get('ticker')}: {e}")


def save_trades(trades):
    """Bulk insert or update trades."""
    if not trades:
        return
    try:
        rows = [
            {"id": int(t["id"]), "payload": t}
            for t in trades
        ]
        get_db().table("trades").upsert(rows).execute()
    except Exception as e:
        st.error(f"Could not save trades: {e}")


# ============================================================
# CREATE
# ============================================================
def add_trade(ticker, entry_price, stop_loss_pct, trail_multiplier):
    """Add a new trade to the database."""
    trades = load_trades()

    # ATR at entry
    atr = calculate_atr(ticker.upper())
    if atr is None:
        atr = entry_price * 0.02

    # Unique ID (never reuse an ID, even after deleting trades)
    next_id = max([t["id"] for t in trades], default=-1) + 1

    trade = {
        "id": next_id,
        "ticker": ticker.upper(),
        "entry_price": entry_price,
        "highest_price": entry_price,
        "stop_loss_pct": stop_loss_pct,
        "trail_multiplier": trail_multiplier,
        "atr_at_entry": atr,
        "status": "ACTIVE",
        "current_price": entry_price,
        "trailing_stop": entry_price - (trail_multiplier * atr),
        "hard_stop": entry_price * (1 - stop_loss_pct),
        "pnl_pct": 0.0,
        "email_sent": False,
        "entry_time": datetime.now().isoformat(),
        "exit_time": None,
    }

    save_trade(trade)
    return trade


# ============================================================
# MONITOR
# ============================================================
def update_trades():
    """Refresh prices and evaluate stop levels for all open trades."""
    trades = load_trades()

    for trade in trades:
        if trade["status"] == "CLOSED":
            continue

        current_price = get_current_price(trade["ticker"])
        if current_price is None:
            continue

        # Track the peak price (never moves down)
        if current_price > trade["highest_price"]:
            trade["highest_price"] = current_price

        atr = trade["atr_at_entry"]
        hard_stop = trade["entry_price"] * (1 - trade["stop_loss_pct"])
        trailing_stop = trade["highest_price"] - (trade["trail_multiplier"] * atr)

        trade["current_price"] = current_price
        trade["trailing_stop"] = trailing_stop
        trade["hard_stop"] = hard_stop
        trade["pnl_pct"] = (
            (current_price - trade["entry_price"]) / trade["entry_price"]
        ) * 100

        # Effective stop = the HIGHER of the two levels
        effective_stop = max(hard_stop, trailing_stop)

        if current_price <= effective_stop:
            if trade["status"] != "SELL_SIGNAL":
                trade["status"] = "SELL_SIGNAL"

                if not trade.get("email_sent", False):
                    if send_sell_signal_email(trade):
                        trade["email_sent"] = True

        save_trade(trade)

    return trades


# ============================================================
# CLOSE
# ============================================================
def close_trade(trade_id):
    """Mark a trade as closed, persist it, and send confirmation email."""
    trades = load_trades()
    closed_trade = None

    for trade in trades:
        if trade["id"] == trade_id:
            trade["status"] = "CLOSED"
            trade["exit_time"] = datetime.now().isoformat()
            closed_trade = trade
            break

    if closed_trade is None:
        return

    save_trade(closed_trade)

    if send_trade_closed_email(closed_trade):
        print(f"✅ Trade closed confirmation sent for {closed_trade['ticker']}")
