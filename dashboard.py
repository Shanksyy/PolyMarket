"""
PolyMarket Bot Dashboard
Run with: streamlit run dashboard.py
"""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

import pandas as pd
import psutil
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
PORTFOLIO_FILE = BASE_DIR / "data" / "portfolio.json"
PID_FILE = BASE_DIR / "data" / "bot.pid"
MAIN_PY = BASE_DIR / "main.py"
LOG_DIR = BASE_DIR / "logs"

# ── Page config (must be first Streamlit call) ────────────────────────────────
st.set_page_config(
    page_title="PolyMarket Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Auto-refresh every 30 seconds
st_autorefresh(interval=30_000, key="auto_refresh")


# ── Data helpers ──────────────────────────────────────────────────────────────

def load_portfolio() -> dict:
    if not PORTFOLIO_FILE.exists():
        return {"open_trades": {}, "closed_trades": [], "saved_at": None}
    try:
        return json.loads(PORTFOLIO_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"open_trades": {}, "closed_trades": [], "saved_at": None}


def compute_summary(portfolio: dict) -> dict:
    open_trades = portfolio.get("open_trades", {})
    closed_trades = portfolio.get("closed_trades", [])
    wins = [t for t in closed_trades if t.get("pnl_usd", 0) > 0]
    losses = [t for t in closed_trades if t.get("pnl_usd", 0) <= 0]
    total_pnl = sum(t.get("pnl_usd", 0) for t in closed_trades)
    open_exposure = sum(t.get("size_usd", 0) for t in open_trades.values())
    total_invested = sum(t.get("size_usd", 0) for t in closed_trades)
    return {
        "open_positions": len(open_trades),
        "open_exposure_usd": open_exposure,
        "closed_positions": len(closed_trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": len(wins) / len(closed_trades) if closed_trades else 0.0,
        "total_pnl_usd": total_pnl,
        "total_invested_usd": total_invested,
        "roi_pct": (total_pnl / total_invested * 100) if total_invested else 0.0,
        "avg_win_usd": sum(t.get("pnl_usd", 0) for t in wins) / len(wins) if wins else 0.0,
        "avg_loss_usd": sum(t.get("pnl_usd", 0) for t in losses) / len(losses) if losses else 0.0,
    }


# ── Bot process management ────────────────────────────────────────────────────

def get_bot_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        if psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            cmdline = " ".join(proc.cmdline())
            if "main.py" in cmdline:
                return pid
        PID_FILE.unlink(missing_ok=True)
    except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        PID_FILE.unlink(missing_ok=True)
    return None


def start_bot() -> None:
    import subprocess
    LOG_DIR.mkdir(exist_ok=True)
    stdout_log = open(LOG_DIR / "bot_stdout.log", "a")
    stderr_log = open(LOG_DIR / "bot_stderr.log", "a")
    proc = subprocess.Popen(
        [sys.executable, str(MAIN_PY)],
        cwd=str(BASE_DIR),
        stdout=stdout_log,
        stderr=stderr_log,
    )
    PID_FILE.write_text(str(proc.pid))
    st.session_state["bot_proc"] = proc


def stop_bot(pid: int) -> None:
    try:
        psutil.Process(pid).send_signal(signal.SIGTERM)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    finally:
        PID_FILE.unlink(missing_ok=True)


# ── Load data ─────────────────────────────────────────────────────────────────

portfolio = load_portfolio()
summary = compute_summary(portfolio)
pid = get_bot_pid()
bot_running = pid is not None

# ── Header ────────────────────────────────────────────────────────────────────

col_title, col_status, col_btn = st.columns([5, 2, 2])

with col_title:
    st.title("📈 PolyMarket Bot Dashboard")
    if portfolio.get("saved_at"):
        st.caption(f"Last updated: {portfolio['saved_at'][:19].replace('T', ' ')} UTC")

with col_status:
    st.write("")  # vertical spacing
    if bot_running:
        st.success(f"🟢 Bot RUNNING  (PID {pid})")
    else:
        st.error("🔴 Bot STOPPED")

with col_btn:
    st.write("")
    if bot_running:
        if st.button("⏹ Stop Bot", type="primary", use_container_width=True):
            stop_bot(pid)
            st.rerun()
    else:
        if st.button("▶ Start Bot", type="primary", use_container_width=True):
            start_bot()
            st.rerun()

st.divider()

# ── Key metrics ───────────────────────────────────────────────────────────────

m1, m2, m3, m4, m5, m6 = st.columns(6)

m1.metric("Open Positions", summary["open_positions"])
m2.metric("Open Exposure", f"${summary['open_exposure_usd']:.2f}")
m3.metric(
    "Total PnL",
    f"${summary['total_pnl_usd']:.2f}",
    delta=f"{summary['total_pnl_usd']:+.2f}",
)
m4.metric("Win Rate", f"{summary['win_rate']:.1%}")
m5.metric(
    "ROI",
    f"{summary['roi_pct']:.1f}%",
    delta=f"{summary['roi_pct']:+.1f}%",
)
m6.metric("Closed Trades", summary["closed_positions"])

st.divider()

# ── Open positions ────────────────────────────────────────────────────────────

st.subheader("Open Positions")
open_trades = portfolio.get("open_trades", {})

if not open_trades:
    st.info("No open positions yet. Start the bot to begin scanning markets.")
else:
    rows = []
    for t in open_trades.values():
        rows.append({
            "Question": t.get("question", "")[:80],
            "Direction": t.get("direction", ""),
            "Size ($)": t.get("size_usd", 0),
            "Entry Price": t.get("entry_price", 0),
            "Probability": t.get("signal_probability") or 0,
            "Edge": t.get("signal_edge") or 0,
            "Confidence": t.get("signal_confidence", ""),
            "Opened": t.get("opened_at", "")[:19].replace("T", " "),
            "Mode": "DRY RUN" if t.get("dry_run") else "LIVE",
        })
    df_open = pd.DataFrame(rows)
    st.dataframe(
        df_open.style.format({
            "Size ($)": "${:.2f}",
            "Entry Price": "{:.1%}",
            "Probability": "{:.1%}",
            "Edge": "{:+.1%}",
        }),
        use_container_width=True,
        hide_index=True,
    )

st.divider()

# ── Closed trades ─────────────────────────────────────────────────────────────

st.subheader("Closed Trades")
closed_trades = portfolio.get("closed_trades", [])

if not closed_trades:
    st.info("No closed trades yet.")
else:
    rows = []
    for t in sorted(closed_trades, key=lambda x: x.get("closed_at", ""), reverse=True):
        rows.append({
            "Question": t.get("question", "")[:80],
            "Direction": t.get("direction", ""),
            "Size ($)": t.get("size_usd", 0),
            "Entry": t.get("entry_price", 0),
            "Exit": t.get("exit_price", 0),
            "PnL ($)": t.get("pnl_usd", 0),
            "PnL (%)": t.get("pnl_pct", 0),
            "Closed": t.get("closed_at", "")[:19].replace("T", " "),
            "Mode": "DRY RUN" if t.get("dry_run") else "LIVE",
        })
    df_closed = pd.DataFrame(rows)

    def _color_pnl(val):
        if isinstance(val, (int, float)):
            if val > 0:
                return "color: #2ecc71; font-weight: bold"
            if val < 0:
                return "color: #e74c3c; font-weight: bold"
        return ""

    st.dataframe(
        df_closed.style
            .format({
                "Size ($)": "${:.2f}",
                "Entry": "{:.1%}",
                "Exit": "{:.1%}",
                "PnL ($)": "${:+.2f}",
                "PnL (%)": "{:+.1f}%",
            })
            .map(_color_pnl, subset=["PnL ($)", "PnL (%)"]),
        use_container_width=True,
        hide_index=True,
    )

st.divider()

# ── PnL chart ─────────────────────────────────────────────────────────────────

st.subheader("Cumulative PnL Over Time")

if len(closed_trades) < 2:
    st.info("Need at least 2 closed trades to draw a chart.")
else:
    chart_rows = []
    for t in sorted(closed_trades, key=lambda x: x.get("closed_at", "")):
        if t.get("closed_at"):
            chart_rows.append({
                "closed_at": pd.to_datetime(t["closed_at"]),
                "pnl_usd": t.get("pnl_usd", 0),
            })
    df_chart = pd.DataFrame(chart_rows)
    df_chart["Cumulative PnL ($)"] = df_chart["pnl_usd"].cumsum()
    df_chart = df_chart.set_index("closed_at")
    st.line_chart(df_chart["Cumulative PnL ($)"], use_container_width=True, height=300)

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ Bot Settings")

    try:
        from dotenv import dotenv_values
        env = dotenv_values(BASE_DIR / ".env")
        dry_run = env.get("DRY_RUN", "true").lower() == "true"
        st.markdown(f"**Mode:** {'🧪 DRY RUN (safe)' if dry_run else '💰 LIVE TRADING'}")
        st.markdown(f"**Max position:** ${env.get('MAX_POSITION_SIZE_USD', '50')}")
        st.markdown(f"**Max exposure:** ${env.get('MAX_TOTAL_EXPOSURE_USD', '500')}")
        st.markdown(f"**Min edge:** {float(env.get('MIN_EDGE_THRESHOLD', '0.07')):.0%}")
        st.markdown(f"**Scan interval:** {env.get('SCAN_INTERVAL_MINUTES', '15')} minutes")
        st.markdown(f"**Min confidence:** {env.get('MIN_CONFIDENCE', 'medium')}")
    except Exception:
        st.warning("Could not read .env settings.")

    st.divider()

    st.subheader("📋 Bot Log")
    stdout_log = LOG_DIR / "bot_stdout.log"
    if stdout_log.exists():
        lines = stdout_log.read_text().splitlines()
        st.code("\n".join(lines[-30:]), language=None)
    else:
        st.caption("Log will appear here once the bot starts.")

    st.divider()
    if st.button("🔄 Refresh Now", use_container_width=True):
        st.rerun()
    st.caption("Auto-refreshes every 30 seconds")
