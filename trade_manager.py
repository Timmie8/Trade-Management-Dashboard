import streamlit as st
from supabase import create_client, Client
from datetime import datetime
from utils import calculate_atr, get_current_price

# Initialize Supabase client (cached for performance)
@st.cache_resource
def init_supabase():
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["key"]
    return create_client(url, key)

supabase: Client = init_supabase()

def load_trades():
    """Load trades from Supabase database"""
    try:
        response = supabase.table("trades").select("*").order("id", desc=False).execute()
        return response.data if response.data else []
    except Exception as e:
        st.error(f"Failed to load trades: {e}")
        return []

def save_trade_to_db(trade):
    """Upsert (insert or update) a single trade"""
    try:
        supabase.table("trades").upsert(trade).execute()
    except Exception as e:
        st.error(f"Failed to save trade: {e}")

def add_trade(ticker, entry_price, stop_loss_pct, trail_multiplier):
    """Add a new trade to the database"""
    trades = load_trades()
    
    atr = calculate_atr(ticker)
    if atr is None:
        atr = entry_price * 0.02
    
    new_id = max([t["id"] for t in trades], default=-1) + 1
    
    trade = {
        "id": new_id,
        "ticker": ticker.upper(),
        "entry_price": float(entry_price),
        "highest_price": float(entry_price),
        "stop_loss_pct": float(stop_loss_pct),
        "trail_multiplier": float(trail_multiplier),
        "atr_at_entry": float(atr),
        "status": "ACTIVE",
        "current_price": float(entry_price),
        "trailing_stop": None,
        "hard_stop": float(entry_price * (1 - stop_loss_pct)),
        "pnl_pct": 0.0,
        "entry_time": datetime.now().isoformat(),
        "exit_time": None
    }
    
    save_trade_to_db(trade)
    return trade

def update_trades():
    """Update all active trades with current prices"""
    trades = load_trades()
    
    for trade in trades:
        if trade["status"] == "CLOSED":
            continue
        
        current_price = get_current_price(trade["ticker"])
        if current_price is None:
            continue
        
        if current_price > trade["highest_price"]:
            trade["highest_price"] = float(current_price)
        
        atr = trade["atr_at_entry"]
        hard_stop = trade["entry_price"] * (1 - trade["stop_loss_pct"])
        trailing_stop = trade["highest_price"] - (trade["trail_multiplier"] * atr)
        
        trade["current_price"] = float(current_price)
        trade["trailing_stop"] = float(trailing_stop)
        trade["hard_stop"] = float(hard_stop)
        trade["pnl_pct"] = float(((current_price - trade["entry_price"]) / trade["entry_price"]) * 100)
        
        if current_price <= hard_stop or current_price <= trailing_stop:
            if trade["status"] != "SELL_SIGNAL":
                trade["status"] = "SELL_SIGNAL"
        
        save_trade_to_db(trade)
    
    return trades

def close_trade(trade_id):
    """Mark a trade as closed"""
    trades = load_trades()
    closed_trade = None
    
    for trade in trades:
        if trade["id"] == trade_id:
            trade["status"] = "CLOSED"
            trade["exit_time"] = datetime.now().isoformat()
            closed_trade = trade
            break
    
    if closed_trade is not None:
        save_trade_to_db(closed_trade)
