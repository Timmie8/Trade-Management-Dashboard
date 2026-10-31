"""
ATR Trading Dashboard
=====================

A self-contained Streamlit paper-trading dashboard that uses REAL market data
from Yahoo Finance (yfinance) and sizes every trade's exits off the stock's own
ATR(14) volatility, instead of a fixed percentage.

    pip install -r requirements.txt
    streamlit run app.py

Educational / simulation only — no broker connection, no real orders.
"""

from __future__ import annotations

import os
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
from streamlit_autorefresh import st_autorefresh

from trading_core import (
    DEFAULT_ATR_PERIOD,
    DEFAULT_SL_ATR,
    DEFAULT_TP_ATR,
    DEFAULT_TRAIL_ACTIVATION_ATR,
    DEFAULT_TRAIL_K,
    TradingStore,
    atr_percent,
    atr_series,
    build_levels,
    check_exit,
    compute_atr,
    compute_stats,
    compute_trailing_stop,
    fetch_ohlc,
    get_last_price,
    suggest_quantity,
)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
DEFAULT_WATCHLIST = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "SPY"]

st.set_page_config(
    page_title="ATR Trading Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --------------------------------------------------------------------------- #
# Styling
# --------------------------------------------------------------------------- #
st.markdown(
    """
    <style>
      .block-container {padding-top: 1.4rem; padding-bottom: 2rem; max-width: 1500px;}
      div[data-testid="stMetricValue"] {font-size: 1.55rem;}
      div[data-testid="stMetricLabel"] {opacity: .72;}
      .hero {
          background: linear-gradient(120deg, #0f172a 0%, #1e293b 55%, #0b3d3a 100%);
          border: 1px solid #1f2b3d; border-radius: 16px;
          padding: 20px 26px; margin-bottom: 14px;
      }
      .hero h1 {margin: 0; font-size: 1.65rem; letter-spacing: .2px; color: #f8fafc;}
      .hero p {margin: 6px 0 0 0; color: #a7b6cc; font-size: .9rem;}
      .pill {
          display: inline-block; padding: 3px 10px; border-radius: 999px;
          font-size: .72rem; font-weight: 600; letter-spacing: .4px;
      }
      .pill-live {background: #052e1b; color: #4ade80; border: 1px solid #14532d;}
      .pill-sim  {background: #2a1e05; color: #fbbf24; border: 1px solid #713f12;}
      .card {
          border: 1px solid #24303f; border-radius: 12px; padding: 12px 14px;
          background: #0d1420; margin-bottom: 8px;
      }
      code {font-size: .82rem;}
    </style>
    """,
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Session / store
# --------------------------------------------------------------------------- #
if "store" not in st.session_state:
    st.session_state.store = TradingStore(STATE_FILE)
store: TradingStore = st.session_state.store

if "ticker" not in st.session_state:
    st.session_state.ticker = "AAPL"
if "auto_close_log" not in st.session_state:
    st.session_state.auto_close_log = []


# Cache market data briefly so a UI interaction does not hammer Yahoo Finance,
# while the auto-refresh still picks up fresh prices.
@st.cache_data(ttl=45, show_spinner=False)
def cached_ohlc(ticker: str, period: str = "1y") -> pd.DataFrame:
    return fetch_ohlc(ticker, period=period)


@st.cache_data(ttl=20, show_spinner=False)
def cached_price(ticker: str) -> float | None:
    return get_last_price(ticker)


@st.cache_data(ttl=45, show_spinner=False)
def cached_atr(ticker: str, period: int = DEFAULT_ATR_PERIOD) -> float | None:
    return compute_atr(cached_ohlc(ticker, "1y"), int(period))


def quote_block(ticker: str) -> dict:
    """Single-ticker snapshot: price, ATR, ATR%, day change, name."""
    df = cached_ohlc(ticker, "1y")
    price = cached_price(ticker)
    atr = compute_atr(df, DEFAULT_ATR_PERIOD)
    if price is None and not df.empty:
        price = float(df["Close"].iloc[-1])
    change_pct = None
    if not df.empty and len(df) > 1 and price:
        prev = float(df["Close"].iloc[-2])
        if prev:
            change_pct = (price / prev - 1.0) * 100.0
    name = ticker
    try:
        info = yf.Ticker(ticker).info or {}
        name = info.get("shortName") or info.get("longName") or ticker
    except Exception:
        pass
    return {
        "ticker": ticker,
        "name": name,
        "price": price,
        "atr": atr,
        "atr_pct": atr_percent(price, atr) if (price and atr) else None,
        "change_pct": change_pct,
        "df": df,
        "ok": bool(price),
    }


# --------------------------------------------------------------------------- #
# Auto-close engine
# --------------------------------------------------------------------------- #
def refresh_positions(quiet: bool = False) -> list[dict]:
    """Re-price every position, ratchet its trailing stop, then test for exits.

    Order matters: the trail is advanced **first** using the price that just printed;
    only then is the exit tested. So a print that pushes the stop up can never also be
    the print that closes the trade against the old, lower stop.
    """
    closed_now = []
    for pos in list(store.positions):
        price = cached_price(pos["ticker"])
        if price is None:
            continue
        pos["last_price"] = price
        pos["last_check"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"

        atr = cached_atr(pos["ticker"], int(atr_period))
        if atr:
            pos["last_atr"] = float(atr)
            store.apply_trailing(pos, price, atr)

        reason = check_exit(pos, price)
        if reason:
            closed = store.close_position(pos["id"], price, reason)
            closed_now.append(closed)
            if not quiet:
                emoji = {"Take profit": "🎯", "Trailing stop": "🪜"}.get(reason, "🛑")
                st.toast(
                    f"{emoji} {reason} hit on {closed['ticker']} @ ${price:,.2f} "
                    f"({closed['pnl']:+,.2f})",
                    icon="✅",
                )
    store.save()
    st.session_state.auto_close_log = closed_now + st.session_state.auto_close_log
    return closed_now


def equity_snapshot() -> tuple[float, float, float]:
    """(open exposure, unrealised P&L, realised P&L)."""
    exposure, unreal = 0.0, 0.0
    for pos in store.positions:
        px = pos.get("last_price") or pos["entry_price"]
        exposure += pos["entry_price"] * pos["qty"]
        unreal += (px - pos["entry_price"]) * pos["qty"]
    realised = sum(t.get("pnl", 0.0) for t in store.history)
    return exposure, unreal, realised


# --------------------------------------------------------------------------- #
# Sidebar — controls
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("### ⚙️ Engine")
    auto = st.toggle("Auto-refresh & auto-close", value=True)
    interval = st.select_slider(
        "Refresh interval (seconds)", options=[10, 15, 30, 60, 120, 300], value=30
    )
    if auto:
        st_autorefresh(interval=interval * 1000, key="atr_autorefresh")
    st.caption(
        "Every refresh re-prices all open positions from Yahoo Finance, ratchets the "
        "trailing stop and closes the trade the moment price touches the take-profit "
        "or the (trailing) stop."
    )

    st.divider()
    st.markdown("### 🧮 Default risk model")
    atr_period = st.number_input("ATR period (days)", 5, 50, DEFAULT_ATR_PERIOD, 1)
    tp_atr_default = st.slider("Default take-profit (× ATR)", 0.5, 6.0, DEFAULT_TP_ATR, 0.25)
    sl_atr_default = st.slider("Default stop-loss (× ATR)", 0.3, 4.0, DEFAULT_SL_ATR, 0.1)
    account_size = st.number_input("Account size ($)", 1_000, 10_000_000, 25_000, 500)
    risk_pct = st.slider("Risk per trade (% of account)", 0.25, 5.0, 1.0, 0.25)

    st.markdown("### 🪜 Trailing stop")
    trail_k_default = st.slider(
        "Trail distance (× ATR behind peak)",
        1.0, 8.0, float(DEFAULT_TRAIL_K), 0.25,
        help="Chandelier exit: the stop sits this many ATRs below the highest price "
             "since entry, and only ever moves up.",
    )
    trail_act_default = st.slider(
        "Start trailing after profit of (× ATR)",
        0.0, 4.0, float(DEFAULT_TRAIL_ACTIVATION_ATR), 0.25,
        help="0 = trail from the first tick. Higher = give the trade room to breathe "
             "before the stop starts following price.",
    )
    st.caption(
        "`stop = max(prev_stop, peak − k×ATR)`. The `max()` is what makes it one-way: "
        "the stop can never slide back down."
    )

    st.divider()
    st.markdown("### 📋 Watchlist")
    wl_raw = st.text_input(
        "Tickers (comma separated)", ", ".join(DEFAULT_WATCHLIST), label_visibility="collapsed"
    )
    watchlist = [t.strip().upper() for t in wl_raw.split(",") if t.strip()][:8]

    st.divider()
    with st.expander("🗑️ Danger zone"):
        if st.button("Reset dashboard", use_container_width=True):
            store.reset()
            st.session_state.auto_close_log = []
            st.cache_data.clear()
            st.rerun()

# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
now_label = datetime.now().strftime("%d %b %Y · %H:%M:%S")
st.markdown(
    f"""
    <div class="hero">
      <h1>📈 ATR Trading Dashboard</h1>
      <p>Real Yahoo Finance market data · volatility-aware exits sized on each stock's ATR(14)
         · automatic take-profit &amp; stop-loss execution on a simulated book.</p>
      <p style="margin-top:10px">
        <span class="pill pill-live">● LIVE PRICES</span>&nbsp;
        <span class="pill pill-sim">SIMULATED EXECUTION</span>&nbsp;
        <span style="color:#64748b;font-size:.75rem">last refresh {now_label}</span>
      </p>
    </div>
    """,
    unsafe_allow_html=True,
)

# Run the auto-close engine before rendering anything else.
just_closed = refresh_positions(quiet=False)

# --------------------------------------------------------------------------- #
# Account metrics
# --------------------------------------------------------------------------- #
exposure, unrealised, realised = equity_snapshot()
stats = compute_stats(store.history)
equity = account_size + realised + unrealised

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Equity", f"${equity:,.0f}", f"{((equity / account_size) - 1) * 100:+.2f}% vs start")
c2.metric("Open positions", f"{len(store.positions)}", f"${exposure:,.0f} exposure")
c3.metric("Unrealised P&L", f"${unrealised:+,.2f}")
c4.metric("Realised P&L", f"${realised:+,.2f}", f"{stats['trades']} closed trades")
c5.metric("Win rate", f"{stats['win_rate']:.1f}%", f"{stats['wins']}W / {stats['losses']}L")
pf = stats["profit_factor"]
c6.metric("Profit factor", "∞" if pf == float("inf") else f"{pf:.2f}", f"expectancy ${stats['expectancy']:,.2f}")

if just_closed:
    for cl in just_closed:
        st.success(
            f"**Auto-closed {cl['ticker']}** — {cl['exit_reason']} at ${cl['exit_price']:,.2f} · "
            f"P&L {cl['pnl']:+,.2f} ({cl['pnl_pct']:+.2f}%)"
        )

st.divider()

# --------------------------------------------------------------------------- #
# Watchlist heat strip
# --------------------------------------------------------------------------- #
st.markdown("#### Watchlist — volatility snapshot")
wl_cols = st.columns(len(watchlist)) if watchlist else []
for col, tk in zip(wl_cols, watchlist):
    q = quote_block(tk)
    with col:
        if q["ok"]:
            label = f"{tk}"
            col.metric(
                label,
                f"${q['price']:,.2f}",
                f"{q['change_pct']:+.2f}%" if q["change_pct"] is not None else None,
            )
            col.caption(f"ATR14 ${q['atr']:,.2f} · {q['atr_pct']:.2f}% of price")
            if st.button("Load", key=f"load_{tk}", use_container_width=True):
                st.session_state.ticker = tk
                st.rerun()
        else:
            col.metric(tk, "—")
            col.caption("No data for this symbol.")

st.divider()

# --------------------------------------------------------------------------- #
# Chart + order ticket
# --------------------------------------------------------------------------- #
left, right = st.columns([2.1, 1], gap="large")

with left:
    ticker_input = st.text_input(
        "Ticker", value=st.session_state.ticker, help="Any Yahoo Finance symbol: AAPL, NVDA, ASML.AS, BTC-USD …"
    ).strip().upper()
    st.session_state.ticker = ticker_input

    chart_range = st.radio(
        "Range", ["3mo", "6mo", "1y", "2y"], index=2, horizontal=True, label_visibility="collapsed"
    )

    quote = quote_block(ticker_input)

    if not quote["ok"]:
        st.error(
            f"No data returned for **{ticker_input or '—'}**. Check the symbol "
            "(Yahoo Finance format, e.g. `ASML.AS` for Amsterdam or `AAPL` for NASDAQ)."
        )
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(quote["ticker"], f"${quote['price']:,.2f}", f"{quote['change_pct']:+.2f}%" if quote['change_pct'] is not None else None)
        m2.metric("ATR(14)", f"${quote['atr']:,.2f}")
        m3.metric("ATR % of price", f"{quote['atr_pct']:.2f}%")
        m4.metric("Company", quote["name"][:22])

        df = cached_ohlc(ticker_input, chart_range).copy()
        if df.empty:
            st.warning("History unavailable for this range.")
        else:
            df["ATR14"] = atr_series(df, int(atr_period))
            fig = go.Figure()
            fig.add_trace(
                go.Candlestick(
                    x=df.index, open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
                    name="Price", increasing_line_color="#22c55e", decreasing_line_color="#ef4444",
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=df.index, y=df["Close"].rolling(20).mean(), name="SMA 20",
                    line=dict(color="#60a5fa", width=1.2),
                )
            )
            # ATR bands around the close — the trading envelope the exits are derived from.
            fig.add_trace(
                go.Scatter(
                    x=df.index, y=df["Close"] + df["ATR14"], name="+1 ATR",
                    line=dict(color="rgba(148,163,184,.45)", width=1, dash="dot"),
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=df.index, y=df["Close"] - df["ATR14"], name="−1 ATR",
                    line=dict(color="rgba(148,163,184,.45)", width=1, dash="dot"),
                    fill="tonexty", fillcolor="rgba(148,163,184,.07)",
                )
            )

            # Overlay open positions for this ticker.
            for pos in store.positions:
                if pos["ticker"] != ticker_input:
                    continue
                fig.add_hline(y=pos["entry_price"], line=dict(color="#38bdf8", width=1.2, dash="dash"),
                              annotation_text=f"Entry {pos['entry_price']:.2f}", annotation_position="right")
                fig.add_hline(y=pos["take_profit"], line=dict(color="#22c55e", width=1.4),
                              annotation_text=f"TP {pos['take_profit']:.2f}", annotation_position="right")
                trail_on = bool(pos.get("trail_active"))
                fig.add_hline(
                    y=pos["stop_loss"],
                    line=dict(color="#f59e0b" if trail_on else "#ef4444", width=1.6,
                              dash="dot" if trail_on else "solid"),
                    annotation_text=(
                        f"Trail SL {pos['stop_loss']:.2f}" if trail_on
                        else f"SL {pos['stop_loss']:.2f}"
                    ),
                    annotation_position="right",
                )
                if trail_on:
                    fig.add_hline(y=pos.get("peak_price", pos["entry_price"]),
                                  line=dict(color="rgba(245,158,11,.35)", width=1, dash="dash"),
                                  annotation_text=f"Peak {pos.get('peak_price', 0):.2f}",
                                  annotation_position="right")

            fig.update_layout(
                height=520, margin=dict(l=10, r=90, t=30, b=10),
                template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(13,20,32,.6)",
                xaxis_rangeslider_visible=False,
                legend=dict(orientation="h", y=1.02, x=0),
                hovermode="x unified",
            )
            st.plotly_chart(fig, use_container_width=True)
            st.caption(
                "Dotted bands = ±1 ATR around the close (the stock's normal daily reach). "
                "Your take-profit and stop-loss are multiples of that band; once the trade "
                "is in profit the stop turns amber and trails the peak."
            )

with right:
    st.markdown("#### 🎫 Order ticket")
    if not quote["ok"]:
        st.info("Enter a valid ticker to build an order.")
    else:
        price = quote["price"]
        atr = quote["atr"]
        if not atr:
            st.warning("Not enough history to compute ATR for this symbol.")
        else:
            tp_mult = st.slider("Take-profit (× ATR)", 0.5, 6.0, float(tp_atr_default), 0.25, key="tp_mult")
            sl_mult = st.slider("Stop-loss (× ATR)", 0.3, 4.0, float(sl_atr_default), 0.1, key="sl_mult")

            levels = build_levels(price, atr, tp_mult, sl_mult)
            suggested_qty = suggest_quantity(account_size, risk_pct, price, levels["stop_loss"])

            # Preview of the trail: it arms at this price and its first floor sits here.
            trail_preview = compute_trailing_stop(
                entry=price,
                extreme=price + trail_act_default * atr,
                current_stop=levels["stop_loss"],
                atr=atr,
                k=float(trail_k_default),
                activation_atr=float(trail_act_default),
            )

            qty = st.number_input(
                "Quantity (shares)", min_value=1, max_value=100_000, value=int(suggested_qty), step=1
            )

            notional = price * qty
            risk_amt = levels["risk_per_share"] * qty
            reward_amt = levels["reward_per_share"] * qty

            st.markdown(
                f"""
                <div class="card">
                  <div style="color:#94a3b8;font-size:.74rem;letter-spacing:.6px">ENTRY (LIVE PRICE)</div>
                  <div style="font-size:1.5rem;font-weight:700;color:#f8fafc">${levels['entry']:,.2f}</div>
                  <div style="display:flex;gap:18px;margin-top:8px;font-size:.85rem">
                    <div><span style="color:#4ade80">▲ TP ${levels['take_profit']:,.2f}</span>
                         <div style="color:#64748b">{levels['tp_pct']:+.2f}% · {tp_mult:g}× ATR</div></div>
                    <div><span style="color:#f87171">▼ SL ${levels['stop_loss']:,.2f}</span>
                         <div style="color:#64748b">{levels['sl_pct']:+.2f}% · {sl_mult:g}× ATR</div></div>
                  </div>
                  <div style="margin-top:10px;color:#94a3b8;font-size:.8rem">
                    ATR ${atr:,.2f} ({quote['atr_pct']:.2f}%) · R:R {levels['rr']:.2f} · Notional ${notional:,.0f}
                  </div>
                  <div style="margin-top:8px;padding-top:8px;border-top:1px solid #24303f;
                              color:#fbbf24;font-size:.8rem">
                    🪜 Trailing arms at ${trail_preview['activation_level']:,.2f} 
                    (+{trail_act_default:g}× ATR) · floor then ${trail_preview['candidate']:,.2f} 
                    ({trail_k_default:g}× ATR behind peak) and only ever rises
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            st.caption(
                f"Risk if stopped out: **${risk_amt:,.2f}** ({risk_amt / account_size * 100:.2f}% of account) · "
                f"Reward if target hit: **${reward_amt:,.2f}** · suggested size {suggested_qty} shares "
                f"at {risk_pct:g}% account risk."
            )

            if st.button("🟢 Buy — open position", type="primary", use_container_width=True):
                store.open_position(
                    ticker=ticker_input,
                    qty=int(qty),
                    entry_price=price,
                    atr=atr,
                    tp_atr=float(tp_mult),
                    sl_atr=float(sl_mult),
                    atr_pct=float(quote["atr_pct"] or 0.0),
                    trail_k=float(trail_k_default),
                    trail_activation_atr=float(trail_act_default),
                )
                st.success(
                    f"Opened {qty} × {ticker_input} @ ${price:,.2f} · TP ${levels['take_profit']:,.2f} · "
                    f"SL ${levels['stop_loss']:,.2f} · trailing {trail_k_default:g}× ATR behind the peak "
                    f"once +{trail_act_default:g}× ATR in profit. It closes automatically on a breach."
                )
                st.rerun()

            st.caption(
                f"Order size is auto-suggested so a stop-out costs ~{risk_pct:g}% of the "
                f"${account_size:,.0f} account. Change it in the sidebar."
            )

# --------------------------------------------------------------------------- #
# Open positions
# --------------------------------------------------------------------------- #
st.divider()
st.markdown("#### 📂 Open positions")

if not store.positions:
    st.info("No open positions. Pick a ticker, set your ATR multiples and press **Buy**.")
else:
    hdr = st.columns([1.0, 0.6, 1.0, 1.2, 1.05, 1.35, 1.15, 0.7])
    for col, label in zip(
        hdr,
        ["Ticker", "Qty", "Entry", "Last / P&L", "TP", "Stop (trailing)", "Peak / locked-in", "Action"],
    ):
        col.markdown(f"<span style='color:#94a3b8;font-size:.76rem;letter-spacing:.5px'>{label.upper()}</span>",
                     unsafe_allow_html=True)

    for pos in list(store.positions):
        last = pos.get("last_price") or pos["entry_price"]
        pnl = (last - pos["entry_price"]) * pos["qty"]
        pnl_pct = (last / pos["entry_price"] - 1.0) * 100.0
        to_tp = (pos["take_profit"] / last - 1.0) * 100.0
        to_sl = (pos["stop_loss"] / last - 1.0) * 100.0

        trail_on = bool(pos.get("trail_active"))
        peak = pos.get("peak_price", pos["entry_price"])
        lock_in = pos.get("trail_lock_in", 0.0)
        trail_k = pos.get("trail_k", DEFAULT_TRAIL_K)
        stop_color = "#fbbf24" if trail_on else "#f87171"
        stop_tag = "🪜 trailing" if trail_on else "fixed"
        pnl_color = "#4ade80" if pnl >= 0 else "#f87171"
        lock_color = "#4ade80" if lock_in > 0 else "#64748b"
        lock_txt = "lock +$" + f"{lock_in:,.2f}" + "/share"

        r = st.columns([1.0, 0.6, 1.0, 1.2, 1.05, 1.35, 1.15, 0.7])
        r[0].markdown(f"**{pos['ticker']}**<br><span style='color:#64748b;font-size:.72rem'>{pos['id']}</span>",
                      unsafe_allow_html=True)
        r[1].write(f"{pos['qty']}")
        r[2].write(f"${pos['entry_price']:,.2f}")
        r[3].markdown(
            f"${last:,.2f}<br><span style='color:{pnl_color};font-size:.8rem'>"
            f"{pnl:+,.2f} ({pnl_pct:+.2f}%)</span>",
            unsafe_allow_html=True,
        )
        r[4].markdown(
            f"${pos['take_profit']:,.2f}<br><span style='color:#64748b;font-size:.74rem'>"
            f"{to_tp:+.2f}% away</span>",
            unsafe_allow_html=True,
        )
        r[5].markdown(
            f"<span style='color:{stop_color}'>${pos['stop_loss']:,.2f}</span> "
            f"<span style='color:#64748b;font-size:.7rem'>{stop_tag}</span><br>"
            f"<span style='color:#64748b;font-size:.74rem'>{to_sl:+.2f}% away · "
            f"{trail_k:g}× ATR</span>",
            unsafe_allow_html=True,
        )
        r[6].markdown(
            f"<span style='color:#e2e8f0'>${peak:,.2f}</span><br>"
            f"<span style='color:{lock_color};font-size:.74rem'>{lock_txt}</span>",
            unsafe_allow_html=True,
        )
        if r[7].button("Close", key=f"close_{pos['id']}", use_container_width=True):
            store.close_position(pos["id"], last, "Manual close")
            st.rerun()

    if st.button("🔄 Refresh prices, trail the stops & run auto-close now", use_container_width=False):
        closed = refresh_positions(quiet=True)
        st.cache_data.clear()
        if closed:
            st.success(f"{len(closed)} position(s) auto-closed on this check.")
        else:
            st.info("No exit level was touched — all positions remain open and the stops were trailed up.")
        st.rerun()

# --------------------------------------------------------------------------- #
# Trade history
# --------------------------------------------------------------------------- #
st.divider()
st.markdown("#### 🧾 Trade history")

if not store.history:
    st.info("Closed trades will appear here.")
else:
    hist = pd.DataFrame(store.history)
    hist["entry_time"] = pd.to_datetime(hist["entry_time"], errors="coerce", utc=True)
    hist["exit_time"] = pd.to_datetime(hist["exit_time"], errors="coerce", utc=True)
    hist["hold_h"] = (hist["exit_time"] - hist["entry_time"]).dt.total_seconds() / 3600.0
    hist = hist.sort_values("exit_time", ascending=False)

    show = hist[
        [
            "ticker", "qty", "entry_price", "exit_price", "exit_reason",
            "pnl", "pnl_pct", "atr_at_entry", "atr_pct_at_entry", "hold_h", "exit_time",
        ]
    ].rename(
        columns={
            "ticker": "Ticker", "qty": "Qty", "entry_price": "Entry", "exit_price": "Exit",
            "exit_reason": "Exit reason", "pnl": "P&L ($)", "pnl_pct": "P&L (%)",
            "atr_at_entry": "ATR at entry", "atr_pct_at_entry": "ATR %", "hold_h": "Held (h)",
            "exit_time": "Closed at (UTC)",
        }
    )
    st.dataframe(
        show.style.format(
            {
                "Entry": "${:,.2f}", "Exit": "${:,.2f}", "P&L ($)": "{:+,.2f}",
                "P&L (%)": "{:+.2f}%", "ATR at entry": "${:,.2f}", "ATR %": "{:.2f}%",
                "Held (h)": "{:.1f}",
            }
        ),
        use_container_width=True,
        hide_index=True,
        height=min(420, 60 + 35 * len(show)),
    )

    a, b = st.columns([1.3, 1])
    with a:
        cum = hist.sort_values("exit_time")["pnl"].cumsum()
        fig2 = go.Figure(
            go.Scatter(
                x=hist.sort_values("exit_time")["exit_time"], y=cum, mode="lines+markers",
                line=dict(color="#38bdf8", width=2), fill="tozeroy",
                fillcolor="rgba(56,189,248,.14)", name="Cumulative P&L",
            )
        )
        fig2.update_layout(
            title="Cumulative realised P&L ($)", height=280, margin=dict(l=10, r=10, t=40, b=10),
            template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(13,20,32,.55)",
        )
        st.plotly_chart(fig2, use_container_width=True)
    with b:
        st.markdown("**Exit mix**")
        mix = hist["exit_reason"].value_counts()
        st.write(
            {
                "Take profit": int(mix.get("Take profit", 0)),
                "Trailing stop": int(mix.get("Trailing stop", 0)),
                "Stop loss": int(mix.get("Stop loss", 0)),
                "Manual close": int(mix.get("Manual close", 0)),
            }
        )
        st.markdown("**Trade statistics**")
        st.write(
            {
                "Closed trades": stats["trades"],
                "Avg win": f"${stats['avg_win']:,.2f}",
                "Avg loss": f"${stats['avg_loss']:,.2f}",
                "Best trade": f"${stats['best']:,.2f}",
                "Worst trade": f"${stats['worst']:,.2f}",
                "Expectancy / trade": f"${stats['expectancy']:,.2f}",
                "Average ATR % at entry": f"{hist['atr_pct_at_entry'].mean():.2f}%",
                "Trailing-stop exits": stats["trail_exits"],
            }
        )

    st.download_button(
        "⬇️ Export trades as CSV",
        hist.to_csv(index=False).encode("utf-8"),
        file_name=f"atr_trades_{datetime.now():%Y%m%d_%H%M}.csv",
        mime="text/csv",
    )

st.divider()
st.caption(
    "Data: Yahoo Finance via `yfinance` (delayed, real market prices). Execution is simulated "
    "on a local JSON book — this is not investment advice and no real orders are placed. "
    "ATR is Wilder's Average True Range over the selected period, computed from daily OHLC. "
    "The trailing stop is a chandelier exit: it sits k×ATR behind the best price since entry "
    "and only ever ratchets towards profit."
)
