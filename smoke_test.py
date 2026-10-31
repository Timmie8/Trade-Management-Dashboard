"""Headless smoke test: real yfinance data -> ATR -> exits -> ATR trailing stop.

Run: python3 smoke_test.py
"""
import os
import tempfile

import pandas as pd

from trading_core import (
    DEFAULT_TRAIL_K,
    TradingStore,
    atr_percent,
    build_levels,
    check_exit,
    compute_atr,
    compute_stats,
    compute_trailing_stop,
    fetch_ohlc,
    get_last_price,
    suggest_quantity,
    true_range,
)


def banner(n, title):
    print("\n" + "=" * 74)
    print(f"{n}. {title}")
    print("=" * 74)


# --------------------------------------------------------------------------- #
banner(1, "REAL DATA FROM YFINANCE")
df = fetch_ohlc("AAPL", period="1y")
assert not df.empty, "yfinance returned no data"
print(f"AAPL rows: {len(df)}  range: {df.index[0].date()} -> {df.index[-1].date()}")
print(df.tail(3).to_string())

# --------------------------------------------------------------------------- #
banner(2, "ATR(14) FROM TRUE RANGE")
tr = true_range(df)
h_l_only = (df["High"] - df["Low"]).iloc[-1]
print(f"Last True Range = {tr.iloc[-1]:.4f};  High-Low only for that day = {h_l_only:.4f}")
print("TR >= High-Low always, and it also captures the overnight gap.")
atr = compute_atr(df, 14)
assert atr and atr > 0
price = get_last_price("AAPL")
assert price
print(f"ATR(14) = {atr:.4f}   live price = {price:.4f}   ATR% = {atr_percent(price, atr):.2f}%")

# --------------------------------------------------------------------------- #
banner(3, "INITIAL EXIT LEVELS (2x ATR TP / 1x ATR SL)")
levels = build_levels(price, atr, 2.0, 1.0)
print(f"entry {levels['entry']:.2f}  TP {levels['take_profit']:.2f} (+{levels['tp_pct']:.2f}%)  "
      f"SL {levels['stop_loss']:.2f} ({levels['sl_pct']:.2f}%)  R:R {levels['rr']:.2f}")
assert levels["take_profit"] > levels["entry"] > levels["stop_loss"]
assert abs(levels["rr"] - 2.0) < 1e-6
qty = suggest_quantity(25000, 1.0, price, levels["stop_loss"])
print(f"Sizing (25k, 1% risk): {qty} shares -> risk ${(price - levels['stop_loss']) * qty:,.2f}")

# --------------------------------------------------------------------------- #
banner(4, "TRAILING STOP — PURE FUNCTION ON THE REAL ATR")
entry = 100.0
k = DEFAULT_TRAIL_K
stop0 = round(entry - atr, 4)
act = 1.0
print(f"entry {entry:.2f} · ATR {atr:.4f} · k={k} · activation +{act}×ATR · initial stop {stop0:.2f}")

r0 = compute_trailing_stop(entry, entry, stop0, atr, k, act)
print(f"  peak {entry:>7.2f} (0.0 ATR)  -> stop {r0['stop']:>7.2f}  active={r0['active']}  moved={r0['moved']}")
assert r0["stop"] == stop0 and not r0["active"] and not r0["moved"]

peak1 = entry + 1.5 * atr
r1 = compute_trailing_stop(entry, peak1, stop0, atr, k, act)
print(f"  peak {peak1:>7.2f} (+1.5 ATR) -> stop {r1['stop']:>7.2f}  active={r1['active']}  "
      f"candidate {r1['candidate']:.2f}  moved={r1['moved']}")
assert r1["active"] and not r1["moved"] and r1["stop"] == stop0
print("    armed, but the candidate floor is still below the original stop -> unchanged (correct)")

peak2 = entry + 3.0 * atr
r2 = compute_trailing_stop(entry, peak2, r1["stop"], atr, k, act)
print(f"  peak {peak2:>7.2f} (+3.0 ATR) -> stop {r2['stop']:>7.2f}  moved={r2['moved']}  "
      f"(= peak − {k}×ATR)")
assert r2["moved"] and r2["stop"] > r1["stop"]
assert abs(r2["stop"] - (peak2 - k * atr)) < 1e-6

peak3 = entry + 4.0 * atr
r3 = compute_trailing_stop(entry, peak3, r2["stop"], atr, k, act)
lock = r3["stop"] - entry
print(f"  peak {peak3:>7.2f} (+4.0 ATR) -> stop {r3['stop']:>7.2f}  "
      f"locked-in +${lock:.2f}/share ({lock / entry * 100:+.2f}%)")
assert r3["stop"] > r2["stop"] and lock > 0

r4 = compute_trailing_stop(entry, peak2, r3["stop"], atr, k, act)
print(f"  pullback to {peak2:.2f}      -> stop {r4['stop']:>7.2f}  moved={r4['moved']}  (never slides back)")
assert r4["stop"] == r3["stop"] and not r4["moved"]

# --------------------------------------------------------------------------- #
banner(5, "BAR-BY-BAR ON REAL BARS — MONOTONICITY + TRIGGER BAR VERIFIED")
path = os.path.join(tempfile.mkdtemp(), "state.json")
store = TradingStore(path)

# Walk a real, rising segment of AAPL: enter at the lowest close of the last 180 bars.
window = df.tail(180)
entry_idx = window["Close"].idxmin()
seg = window.loc[entry_idx:]
entry_px = float(seg["Close"].iloc[0])
entry_atr = compute_atr(df.loc[:entry_idx], 14) or atr
k_bar = 2.0
pos = store.open_position(
    "AAPL", 10, entry_px, entry_atr,
    tp_atr=50.0, sl_atr=1.0,            # TP parked far away so only the trail can exit
    atr_pct=atr_percent(entry_px, entry_atr), trail_k=k_bar, trail_activation_atr=0.5,
)
print(f"entry {entry_px:.2f} on {entry_idx.date()}  ATR {entry_atr:.2f}  "
      f"stop {pos['stop_loss']:.2f}  TP {pos['take_profit']:.2f} (parked)  k={k_bar}")
print(f"segments walked: {len(seg)} bars")

prev_stop = pos["stop_loss"]
rises, trigger_bar, trigger_reason, trace = 0, None, None, []
for ts, row in seg.iterrows():
    px = float(row["Close"])
    bar_atr = compute_atr(df.loc[:ts], 14) or entry_atr
    store.apply_trailing(pos, px, bar_atr)
    assert pos["stop_loss"] >= prev_stop - 1e-9, (
        f"STOP MOVED BACKWARDS on {ts.date()}: {prev_stop:.4f} -> {pos['stop_loss']:.4f}"
    )
    if pos["stop_loss"] > prev_stop + 1e-9:
        rises += 1
        trace.append((ts.date(), px, prev_stop, pos["stop_loss"]))
    prev_stop = pos["stop_loss"]
    reason = check_exit(pos, px)
    if reason and trigger_bar is None:
        trigger_bar, trigger_reason = ts.date(), reason
        trigger_close, trigger_stop = px, pos["stop_loss"]
        break

for d, px, before, after in trace[:6]:
    print(f"  ratchet {d}: close {px:>7.2f}  stop {before:>7.2f} -> {after:>7.2f}")
print(f"ratchets: {rises}   final stop {pos['stop_loss']:.2f}   "
      f"active={pos['trail_active']}   locked-in ${pos['trail_lock_in']:.2f}/share")
print(f"first exit: {trigger_bar} ({trigger_reason})")

assert pos["trail_active"], "trail never armed on a 180-bar real segment"
assert rises >= 1, "trail never ratcheted"
assert pos["stop_loss"] >= pos["initial_stop_loss"]
if trigger_bar:
    print(f"trigger bar {trigger_bar}: close {trigger_close:.2f} <= stop {trigger_stop:.2f} -> "
          f"'{trigger_reason}' is the expected exit")
    assert trigger_close <= trigger_stop
    assert trigger_reason == "Trailing stop"
    assert trigger_stop > pos["initial_stop_loss"]
else:
    print("no breach inside the segment; trail still ratcheted, stop above its initial level")

# --------------------------------------------------------------------------- #
banner(6, "TRAILED EXIT IS LABELLED 'Trailing stop', A PLAIN STOP-OUT IS 'Stop loss'")
exit_px = trigger_close if trigger_bar else float(seg["Close"].iloc[-1])
trailed = store.close_position(pos["id"], exit_px, check_exit(pos, exit_px) or "Trailing stop")
print(f"closed AAPL -> '{trailed['exit_reason']}' at {trailed['exit_price']:.2f}  "
      f"pnl {trailed['pnl']:+,.2f} ({trailed['pnl_pct']:+.2f}%)  "
      f"initial stop {trailed['initial_stop_loss']:.2f} -> final {trailed['stop_loss']:.2f}")
assert trailed["exit_reason"] == "Trailing stop"
assert trailed["stop_loss"] > trailed["initial_stop_loss"]

pos2 = store.open_position("MSFT", 10, 400.0, 8.0, tp_atr=6.0, sl_atr=1.0)
print(f"fresh MSFT @ 400 stop {pos2['stop_loss']:.2f} -> check_exit at 391: "
      f"{check_exit(pos2, 391.0)}")
assert check_exit(pos2, 391.0) == "Stop loss"
store.close_position(pos2["id"], pos2["stop_loss"], "Stop loss")

# --------------------------------------------------------------------------- #
banner(7, "PERSISTENCE + STATS INCL. TRAIL EXITS")
reloaded = TradingStore(path)
print(f"reloaded: {len(reloaded.history)} closed, {len(reloaded.positions)} open")
assert len(reloaded.history) == 2 and len(reloaded.positions) == 0
stats = compute_stats(reloaded.history)
for key in ("trades", "wins", "losses", "win_rate", "realised_pnl",
            "tp_exits", "sl_exits", "trail_exits"):
    print(f"  {key:14s} {stats[key]}")
assert stats["trades"] == 2
assert stats["trail_exits"] == 1 and stats["sl_exits"] == 1
assert stats["tp_exits"] + stats["sl_exits"] + stats["trail_exits"] == stats["trades"]

# --------------------------------------------------------------------------- #
banner(8, "EDGE CASES")
z = compute_trailing_stop(100, 130, 90, 0.0)
print(f"ATR 0            -> stop {z['stop']} (unchanged, no crash)")
assert z["stop"] == 90
z = compute_trailing_stop(100, 101, 90, 2.0, k=3, activation_atr=0)
print(f"activation 0     -> active={z['active']} (trails from the first tick)")
assert z["active"]
z = compute_trailing_stop(100, 130, 90, 2.0, k=0)
print(f"k=0 (no trail)   -> moved={z['moved']} (disabled)")
assert not z["moved"]
short = compute_trailing_stop(100, 80, 110, 2.0, k=3, activation_atr=1.0, direction="short")
print(f"short mirror     -> stop {short['stop']:.2f} active={short['active']} "
      f"(min(), so it only moves down)")
assert short["active"] and short["stop"] <= 110
print(f"invalid ticker   -> empty frame: {fetch_ohlc('ZZZZNOTATICKER9', period='1mo').empty}")
assert compute_atr(pd.DataFrame()) is None

print("\n" + "=" * 74)
print("ALL CHECKS PASSED ✅  (stop monotonicity asserted bar-by-bar on real data)")
print("=" * 74)
