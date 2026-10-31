"""
trading_core.py
---------------
Pure logic for the ATR Trading Dashboard: real market data (yfinance),
ATR(14) volatility calculation, ATR-based take-profit / stop-loss levels,
and a JSON-backed paper-trading store.

No UI code lives here, so every function can be unit-tested head-less.

Position sizing / exit philosophy
---------------------------------
A share's ATR tells you how much it *normally* moves in one day. Instead of
using a fixed % stop (which is far too tight for a volatile stock and far too
wide for a quiet one), the dashboard expresses both exits as a multiple of the
stock's own ATR:

    take_profit = entry + tp_atr_mult * ATR
    stop_loss   = entry - sl_atr_mult * ATR

So a 2x ATR target on a 1% ATR stock is a 2% move, and on a 4% ATR stock it is an
8% move: the same *behavioural* expectation, adapted per ticker.

Trailing stop (chandelier exit)
-------------------------------
Once a trade is in profit the fixed stop becomes dangerous: it gives back the whole
move. The dashboard therefore trails the stop behind the best price reached since
entry, always `k x ATR` below it, and it may only ratchet **towards** profit:

    long : stop = max(previous_stop, highest_price_since_entry - k x ATR)
    short: stop = min(previous_stop, lowest_price_since_entry  + k x ATR)

Trailing only switches on after a configurable profit threshold (default +1 ATR),
so a fresh trade is not shaken out by normal noise before it has any cushion.
Because of the max()/min() the stop can never move backwards -- a property the
smoke test asserts bar by bar.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import yfinance as yf

DEFAULT_ATR_PERIOD = 14
DEFAULT_TP_ATR = 2.0
DEFAULT_SL_ATR = 1.0
DEFAULT_TRAIL_K = 3.0              # chandelier distance, in ATRs
DEFAULT_TRAIL_ACTIVATION_ATR = 1.0  # start trailing once the trade is +1 ATR in profit


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    """Current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Market data (real, via yfinance)
# --------------------------------------------------------------------------- #
def fetch_ohlc(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Download daily OHLCV history for `ticker`. Returns an empty frame on failure."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return pd.DataFrame()
    try:
        df = yf.Ticker(ticker).history(
            period=period, interval=interval, auto_adjust=False, raise_errors=False
        )
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={c: c.title() for c in df.columns})
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    df = df[keep].dropna(subset=["High", "Low", "Close"])
    # Plotly is happiest with naive timestamps.
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    return df


def get_last_price(ticker: str) -> Optional[float]:
    """Latest traded price. Tries intraday, then fast_info, then last daily close."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None
    try:
        intraday = yf.Ticker(ticker).history(period="1d", interval="1m", auto_adjust=False)
        if intraday is not None and not intraday.empty:
            close = intraday["Close"].dropna()
            if len(close):
                return float(close.iloc[-1])
    except Exception:
        pass
    try:
        fast = yf.Ticker(ticker).fast_info
        px = fast["last_price"] if "last_price" in fast else fast.get("lastPrice")
        if px:
            return float(px)
    except Exception:
        pass
    df = fetch_ohlc(ticker, period="5d")
    if not df.empty:
        return float(df["Close"].iloc[-1])
    return None


# --------------------------------------------------------------------------- #
# ATR
# --------------------------------------------------------------------------- #
def true_range(df: pd.DataFrame) -> pd.Series:
    """Wilder's True Range: max(H-L, |H-prevC|, |L-prevC|)."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    return pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr_series(df: pd.DataFrame, period: int = DEFAULT_ATR_PERIOD) -> pd.Series:
    """Wilder-smoothed ATR series (EMA with alpha = 1/period)."""
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    return true_range(df).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def compute_atr(df: pd.DataFrame, period: int = DEFAULT_ATR_PERIOD) -> Optional[float]:
    """Latest ATR value, or None when there is not enough history."""
    if df is None or df.empty or len(df) < period + 1:
        return None
    series = atr_series(df, period).dropna()
    if series.empty:
        return None
    value = float(series.iloc[-1])
    return value if value > 0 else None


def atr_percent(price: float, atr: float) -> float:
    """ATR expressed as a percentage of price (the stock's daily 'normal' move)."""
    if not price or not atr:
        return 0.0
    return atr / price * 100.0


# --------------------------------------------------------------------------- #
# Exit levels
# --------------------------------------------------------------------------- #
def build_levels(
    entry: float,
    atr: float,
    tp_atr: float = DEFAULT_TP_ATR,
    sl_atr: float = DEFAULT_SL_ATR,
) -> Dict[str, float]:
    """Translate ATR multiples into concrete take-profit / stop-loss prices."""
    tp = entry + tp_atr * atr
    sl = max(entry - sl_atr * atr, 0.01)
    risk = entry - sl
    reward = tp - entry
    return {
        "entry": round(entry, 4),
        "take_profit": round(tp, 4),
        "stop_loss": round(sl, 4),
        "tp_pct": (tp / entry - 1.0) * 100.0 if entry else 0.0,
        "sl_pct": (sl / entry - 1.0) * 100.0 if entry else 0.0,
        "risk_per_share": risk,
        "reward_per_share": reward,
        "rr": (reward / risk) if risk > 0 else 0.0,
    }


def compute_trailing_stop(
    entry: float,
    extreme: float,
    current_stop: float,
    atr: float,
    k: float = DEFAULT_TRAIL_K,
    activation_atr: float = DEFAULT_TRAIL_ACTIVATION_ATR,
    direction: str = "long",
) -> Dict[str, Any]:
    """Chandelier-style ATR trailing stop.

    Parameters
    ----------
    entry          : fill price of the position
    extreme        : highest high since entry (long) / lowest low since entry (short)
    current_stop   : the stop currently stored on the position
    atr            : ATR at the time of the check
    k              : how many ATRs the stop trails behind the extreme
    activation_atr : profit (in ATRs) required before trailing switches on
    direction      : 'long' or 'short'

    Returns a dict with the new stop, whether trailing is active, the raw candidate
    level and whether the stop actually moved. The returned stop is guaranteed to be
    monotonically non-decreasing for longs (non-increasing for shorts), because the
    candidate is only accepted when it is *better* than the stop already stored.
    """
    if not atr or atr <= 0 or not k or k <= 0 or not extreme:
        return {"stop": current_stop, "active": False, "candidate": current_stop, "moved": False}

    is_long = direction != "short"
    if is_long:
        activation_level = entry + activation_atr * atr
        active = extreme >= activation_level
        candidate = extreme - k * atr
        stop = max(current_stop, candidate) if active else current_stop
    else:
        activation_level = entry - activation_atr * atr
        active = extreme <= activation_level
        candidate = extreme + k * atr
        stop = min(current_stop, candidate) if active else current_stop

    stop = round(float(stop), 4)
    # Rounding must never be allowed to nudge the stop backwards, so clamp against the
    # (rounded) stored stop after rounding. This keeps the ratchet strictly one-way.
    stored = round(float(current_stop), 4)
    stop = max(stop, stored) if is_long else min(stop, stored)
    return {
        "stop": stop,
        "active": bool(active),
        "candidate": round(float(candidate), 4),
        "moved": stop != stored,
        "activation_level": round(float(activation_level), 4),
    }


def check_exit(position: Dict[str, Any], price: float) -> Optional[str]:
    """Return the exit reason when `price` breaches a level, else None.

    A breach of the stop that has been ratcheted up by the trail is reported as
    'Trailing stop' so the history distinguishes locked-in profit from an ordinary
    initial stop-out.
    """
    if price is None or position.get("status") != "open":
        return None
    if position.get("take_profit") and price >= position["take_profit"]:
        return "Take profit"
    if position.get("stop_loss") and price <= position["stop_loss"]:
        trailed = position.get("trail_active") and position.get("stop_loss", 0) > position.get(
            "initial_stop_loss", position.get("stop_loss", 0)
        )
        return "Trailing stop" if trailed else "Stop loss"
    return None


def suggest_quantity(account: float, risk_pct: float, entry: float, stop: float) -> int:
    """Position size so that a stop-out costs ~risk_pct of the account."""
    per_share = max(entry - stop, 0.0)
    if per_share <= 0 or account <= 0 or risk_pct <= 0:
        return 1
    budget = account * (risk_pct / 100.0)
    return max(int(budget // per_share), 1)


# --------------------------------------------------------------------------- #
# Paper trading store (JSON persisted)
# --------------------------------------------------------------------------- #
class TradingStore:
    """Tiny JSON-backed store for open positions and closed trades."""

    def __init__(self, path: str = "state.json"):
        self.path = path
        self.positions: List[Dict[str, Any]] = []
        self.history: List[Dict[str, Any]] = []
        self.load()

    # -- persistence ------------------------------------------------------ #
    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.positions = data.get("positions", [])
            self.history = data.get("history", [])
        except (json.JSONDecodeError, OSError):
            self.positions, self.history = [], []

    def save(self) -> None:
        payload = {"positions": self.positions, "history": self.history, "updated": now_iso()}
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self.path)

    def reset(self) -> None:
        self.positions, self.history = [], []
        if os.path.exists(self.path):
            os.remove(self.path)

    # -- trading ---------------------------------------------------------- #
    def open_position(
        self,
        ticker: str,
        qty: int,
        entry_price: float,
        atr: float,
        tp_atr: float = DEFAULT_TP_ATR,
        sl_atr: float = DEFAULT_SL_ATR,
        atr_pct: float = 0.0,
        trail_k: float = DEFAULT_TRAIL_K,
        trail_activation_atr: float = DEFAULT_TRAIL_ACTIVATION_ATR,
    ) -> Dict[str, Any]:
        levels = build_levels(entry_price, atr, tp_atr, sl_atr)
        pos = {
            "id": uuid.uuid4().hex[:8],
            "ticker": ticker.upper(),
            "qty": int(qty),
            "entry_price": float(entry_price),
            "entry_time": now_iso(),
            "atr_at_entry": float(atr),
            "atr_pct_at_entry": float(atr_pct),
            "tp_atr": float(tp_atr),
            "sl_atr": float(sl_atr),
            "take_profit": float(levels["take_profit"]),
            "initial_stop_loss": float(levels["stop_loss"]),
            "stop_loss": float(levels["stop_loss"]),
            "peak_price": float(entry_price),      # extreme reached since entry
            "trail_k": float(trail_k),
            "trail_activation_atr": float(trail_activation_atr),
            "trail_active": False,
            "trail_lock_in": 0.0,                  # profit per share already secured
            "status": "open",
            "last_price": float(entry_price),
            "last_check": now_iso(),
        }
        self.positions.append(pos)
        self.save()
        return pos

    def close_position(self, pos_id: str, price: float, reason: str) -> Dict[str, Any]:
        for pos in list(self.positions):
            if pos["id"] != pos_id:
                continue
            pnl = (price - pos["entry_price"]) * pos["qty"]
            pnl_pct = (price / pos["entry_price"] - 1.0) * 100.0 if pos["entry_price"] else 0.0
            closed = dict(pos)
            closed.update(
                {
                    "status": "closed",
                    "exit_price": float(price),
                    "exit_time": now_iso(),
                    "exit_reason": reason,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "notional": pos["entry_price"] * pos["qty"],
                }
            )
            self.positions.remove(pos)
            self.history.append(closed)
            self.save()
            return closed
        raise KeyError(f"No open position with id {pos_id}")

    # -- trailing stop ---------------------------------------------------- #
    def apply_trailing(
        self, pos: Dict[str, Any], price: float, atr: Optional[float]
    ) -> Dict[str, Any]:
        """Ratchet one position's stop behind the best price seen so far.

        Mutates `pos` in place and returns the trail result dict. Safe to call on
        positions created by an older version of the dashboard (missing keys fall
        back to the initial stop).
        """
        pos.setdefault("peak_price", pos["entry_price"])
        pos.setdefault("initial_stop_loss", pos["stop_loss"])
        pos.setdefault("trail_k", DEFAULT_TRAIL_K)
        pos.setdefault("trail_activation_atr", DEFAULT_TRAIL_ACTIVATION_ATR)
        pos.setdefault("trail_active", False)

        # The extreme is sticky: it never falls back, which is what makes the trail
        # monotonic even when price retreats.
        pos["peak_price"] = max(float(pos["peak_price"]), float(price))

        result = compute_trailing_stop(
            entry=float(pos["entry_price"]),
            extreme=float(pos["peak_price"]),
            current_stop=float(pos["stop_loss"]),
            atr=atr or 0.0,
            k=float(pos["trail_k"]),
            activation_atr=float(pos["trail_activation_atr"]),
        )
        if result["active"]:
            pos["trail_active"] = True
        if result["moved"]:
            pos["stop_loss"] = result["stop"]
        pos["trail_lock_in"] = max(pos["stop_loss"] - pos["entry_price"], 0.0)
        return result


# --------------------------------------------------------------------------- #
# Performance stats
# --------------------------------------------------------------------------- #
def compute_stats(history: List[Dict[str, Any]]) -> Dict[str, float]:
    """Summary statistics over closed trades."""
    empty = {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "realised_pnl": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "profit_factor": 0.0,
        "expectancy": 0.0,
        "best": 0.0,
        "worst": 0.0,
        "tp_exits": 0,
        "sl_exits": 0,
        "trail_exits": 0,
    }
    if not history:
        return empty
    df = pd.DataFrame(history)
    if "pnl" not in df:
        return empty
    df = df.dropna(subset=["pnl"])
    if df.empty:
        return empty
    wins = df[df["pnl"] > 0]["pnl"]
    losses = df[df["pnl"] <= 0]["pnl"]
    gross_win, gross_loss = float(wins.sum()), float(abs(losses.sum()))
    return {
        "trades": int(len(df)),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate": float(len(wins) / len(df) * 100.0),
        "realised_pnl": float(df["pnl"].sum()),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win else 0.0,
        "expectancy": float(df["pnl"].mean()),
        "best": float(df["pnl"].max()),
        "worst": float(df["pnl"].min()),
        "tp_exits": int((df.get("exit_reason") == "Take profit").sum()),
        "sl_exits": int((df.get("exit_reason") == "Stop loss").sum()),
        "trail_exits": int((df.get("exit_reason") == "Trailing stop").sum()),
    }
