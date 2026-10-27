import json
import os
from utils import calculate_atr, get_current_price

DB_FILE = "trades.json"

def load_trades():
    """Load trades from JSON file"""
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    return []

def save_trades(trades):
    """Save trades to JSON file"""
    with open(DB_FILE, 'w') as f:
        json.dump(trades, f, indent=2)

def add_trade(ticker, entry_price, stop_loss_pct, trail_multiplier):
    """Add a new trade to the database"""
    trades = load_trades()
    
    # Calculate ATR at entry
    atr = calculate_atr(ticker)
    if atr is None:
        atr = entry_price * 0.02  # Fallback: 2% of price
    
    trade = {
        "id": len(trades),
        "ticker": ticker.upper(),
        "entry_price": entry_price,
        "highest_price": entry_price,
        "stop_loss_pct": stop_loss_pct,
        "trail_multiplier": trail_multiplier,
        "atr_at_entry": atr,
        "status": "ACTIVE",
        "current_price": entry_price,
        "trailing_stop": None,
        "hard_stop": entry_price * (1 - stop_loss_pct),
        "pnl_pct": 0.0
    }
    
    trades.append(trade)
    save_trades(trades)
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
        
        # Update highest price
        if current_price > trade["highest_price"]:
            trade["highest_price"] = current_price
        
        # Calculate stops
        atr = trade["atr_at_entry"]
        hard_stop = trade["entry_price"] * (1 - trade["stop_loss_pct"])
        trailing_stop = trade["highest_price"] - (trade["trail_multiplier"] * atr)
        
        # Update trade data
        trade["current_price"] = current_price
        trade["trailing_stop"] = trailing_stop
        trade["hard_stop"] = hard_stop
        trade["pnl_pct"] = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
        
        # Check if sell signal triggered
        if current_price <= hard_stop or current_price <= trailing_stop:
            trade["status"] = "SELL_SIGNAL"
    
    save_trades(trades)
    return trades

def close_trade(trade_id):
    """Mark a trade as closed"""
    trades = load_trades()
    for trade in trades:
        if trade["id"] == trade_id:
            trade["status"] = "CLOSED"
            break
    save_trades(trades)
