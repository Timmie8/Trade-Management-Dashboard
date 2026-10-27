import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from trade_manager import load_trades, update_trades, add_trade, close_trade
from utils import get_price_history
import time

# Page config
st.set_page_config(
    page_title="Trade Management Dashboard",
    page_icon="📊",
    layout="wide"
)

# Custom CSS for better styling
st.markdown("""
<style>
    .main {
        padding: 2rem;
    }
    .stAlert {
        padding: 1rem;
        border-radius: 10px;
    }
</style>
""", unsafe_allow_html=True)

# Title
st.title("📊 Trade Management Dashboard")
st.markdown("---")

# Sidebar - Add new trade
with st.sidebar:
    st.header("➕ Add New Trade")
    
    ticker = st.text_input("Ticker Symbol (e.g., AAPL, MSFT, TSLA)", value="AAPL")
    entry_price = st.number_input("Entry Price ($)", min_value=0.0, step=0.01, value=150.0)
    stop_loss_pct = st.number_input("Stop Loss %", value=2.0, step=0.1, help="Maximum loss before forced exit") / 100
    trail_multiplier = st.number_input("Trailing Stop Multiplier (x ATR)", value=2.5, step=0.1, help="Distance from highest price")
    
    if st.button("🚀 Start Trade", type="primary", use_container_width=True):
        if ticker and entry_price > 0:
            add_trade(ticker, entry_price, stop_loss_pct, trail_multiplier)
            st.success(f"Trade started for {ticker.upper()}!")
            st.rerun()
        else:
            st.error("Please enter valid ticker and price")
    
    st.markdown("---")
    st.markdown("### 📖 How it works")
    st.markdown("""
    1. **Add your trade** with entry price
    2. **Green tiles** = Trade running normally
    3. **Red tiles** = SELL SIGNAL - Take action!
    4. **Gray tiles** = Closed trades (archived)
    """)

# Update all trades with live data
with st.spinner("Updating live prices..."):
    trades = update_trades()

# Separate trades by status
signal_trades = [t for t in trades if t["status"] == "SELL_SIGNAL"]
active_trades = [t for t in trades if t["status"] == "ACTIVE"]
closed_trades = [t for t in trades if t["status"] == "CLOSED"]

# === RED SECTION: SELL SIGNALS ===
if signal_trades:
    st.error("🚨 SELL SIGNALS - ACTION REQUIRED!")
    
    cols = st.columns(len(signal_trades))
    for i, trade in enumerate(signal_trades):
        with cols[i]:
            st.markdown(f"""
            <div style="background-color:#ff4b4b; padding:20px; border-radius:10px; color:white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <h2 style="margin:0;">{trade['ticker']}</h2>
                <p style="font-size:24px; margin:10px 0;"><b>${trade['current_price']:.2f}</b></p>
                <hr style="border-color:rgba(255,255,255,0.3);">
                <p><b>Entry:</b> ${trade['entry_price']:.2f}</p>
                <p><b>P&L:</b> {trade['pnl_pct']:.2f}%</p>
                <p><b>Stop Loss:</b> ${trade['trailing_stop']:.2f}</p>
            </div>
            """, unsafe_allow_html=True)
            
            if st.button(f"✅ Mark as Sold", key=f"close_{trade['id']}", use_container_width=True):
                close_trade(trade['id'])
                st.rerun()

# === GREEN SECTION: ACTIVE TRADES ===
if active_trades:
    st.success(f"🟢 Active Trades ({len(active_trades)})")
    
    cols = st.columns(min(3, len(active_trades)))
    for i, trade in enumerate(active_trades):
        with cols[i % len(cols)]:
            # Calculate distance to trailing stop
            distance_to_stop = ((trade['current_price'] - trade['trailing_stop']) / trade['current_price']) * 100
            warning = "⚠️" if distance_to_stop < 5 else ""
            
            st.markdown(f"""
            <div style="background-color:#0e8a3e; padding:15px; border-radius:10px; color:white;">
                <h3 style="margin:0;">{trade['ticker']} {warning}</h3>
                <p style="font-size:20px; margin:5px 0;"><b>${trade['current_price']:.2f}</b></p>
                <p style="margin:5px 0;">P&L: {trade['pnl_pct']:.2f}%</p>
                <p style="font-size:12px; margin:5px 0; opacity:0.8;">
                    Stop: ${trade['trailing_stop']:.2f} ({distance_to_stop:.1f}% away)
                </p>
            </div>
            """, unsafe_allow_html=True)

# === CHART SECTION ===
if active_trades or signal_trades:
    st.markdown("---")
    st.subheader("📈 Price Charts")
    
    selected_ticker = st.selectbox(
        "Select trade to view chart",
        [t['ticker'] for t in active_trades + signal_trades]
    )
    
    if selected_ticker:
        # Get price history
        hist = get_price_history(selected_ticker, period="3mo")
        
        # Find the trade
        trade = next((t for t in trades if t['ticker'] == selected_ticker), None)
        
        if trade is not None:
            # Create chart
            fig = go.Figure()
            
            # Price line
            fig.add_trace(go.Scatter(
                x=hist.index,
                y=hist['Close'],
                mode='lines',
                name='Price',
                line=dict(color='#00d4aa', width=2)
            ))
            
            # Entry price line
            fig.add_trace(go.Scatter(
                x=[hist.index[0], hist.index[-1]],
                y=[trade['entry_price'], trade['entry_price']],
                mode='lines',
                name='Entry Price',
                line=dict(color='blue', dash='dash')
            ))
            
            # Trailing stop line
            fig.add_trace(go.Scatter(
                x=[hist.index[0], hist.index[-1]],
                y=[trade['trailing_stop'], trade['trailing_stop']],
                mode='lines',
                name='Trailing Stop',
                line=dict(color='red', dash='dot')
            ))
            
            fig.update_layout(
                title=f"{selected_ticker} - Price Action",
                xaxis_title="Date",
                yaxis_title="Price ($)",
                template="plotly_dark",
                height=500
            )
            
            st.plotly_chart(fig, use_container_width=True)

# === GRAY SECTION: CLOSED TRADES ===
if closed_trades:
    with st.expander(f"⚪ Archived Trades ({len(closed_trades)})", expanded=False):
        cols = st.columns(4)
        for i, trade in enumerate(closed_trades):
            with cols[i % len(cols)]:
                st.markdown(f"""
                <div style="background-color:#d3d3d3; padding:10px; border-radius:8px; color:#666; opacity:0.6;">
                    <small><b>{trade['ticker']}</b></small><br>
                    <small>Entry: ${trade['entry_price']:.2f}</small><br>
                    <small>Exit: ${trade['current_price']:.2f}</small><br>
                    <small>P&L: {trade['pnl_pct']:.2f}%</small>
                </div>
                """, unsafe_allow_html=True)

# No trades message
if not trades:
    st.info("👈 Add your first trade using the sidebar!")

# Footer
st.markdown("---")
st.markdown(
    """
    <div style="text-align: center; color: #666; font-size: 12px;">
        <p>Trade Management Dashboard | Live data from Yahoo Finance | Built with Streamlit</p>
    </div>
    """,
    unsafe_allow_html=True
)

# Auto-refresh every 30 seconds
time.sleep(30)
st.rerun()
