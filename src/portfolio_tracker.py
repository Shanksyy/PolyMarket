"""
Tracks open and closed positions, calculates PnL, and persists state to disk.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
TRADES_FILE = DATA_DIR / "trades.json"


class PortfolioTracker:
    """Persistent portfolio tracker backed by JSON files."""

    def __init__(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        self.open_trades: dict[str, dict] = {}   # condition_id → trade
        self.closed_trades: list[dict] = []
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def record_open(
        self,
        order_result: dict,
        market: dict,
        signal: dict,
    ) -> None:
        """Record a newly opened position."""
        cid = market.get("condition_id", order_result.get("condition_id", ""))
        trade = {
            "condition_id": cid,
            "question": market.get("question", ""),
            "direction": order_result.get("direction", ""),
            "size_usd": order_result.get("size_usd", 0.0),
            "entry_price": order_result.get("fill_price") or (
                market.get("yes_price") if order_result.get("direction") == "YES"
                else market.get("no_price")
            ),
            "order_id": order_result.get("order_id", ""),
            "opened_at": _now(),
            "dry_run": order_result.get("dry_run", True),
            "signal_probability": signal.get("probability"),
            "signal_edge": signal.get("edge"),
            "signal_confidence": signal.get("confidence"),
            "reasoning": signal.get("reasoning", ""),
        }
        self.open_trades[cid] = trade
        self._save()
        logger.info("Portfolio: opened position in %s", market.get("question", "")[:60])

    def record_close(
        self,
        condition_id: str,
        exit_price: float,
        exit_reason: str = "manual",
    ) -> dict | None:
        """Mark a position as closed and calculate PnL."""
        trade = self.open_trades.pop(condition_id, None)
        if not trade:
            return None

        pnl = self._calc_pnl(trade, exit_price)
        closed = {
            **trade,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "closed_at": _now(),
            "pnl_usd": pnl,
            "pnl_pct": (pnl / trade["size_usd"] * 100) if trade["size_usd"] else 0.0,
        }
        self.closed_trades.append(closed)
        self._save()
        return closed

    # ── Statistics ────────────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """Return a performance summary dict."""
        total_pnl = sum(t["pnl_usd"] for t in self.closed_trades)
        wins = [t for t in self.closed_trades if t["pnl_usd"] > 0]
        losses = [t for t in self.closed_trades if t["pnl_usd"] <= 0]
        open_exposure = sum(t["size_usd"] for t in self.open_trades.values())
        total_invested = sum(t["size_usd"] for t in self.closed_trades)

        return {
            "open_positions": len(self.open_trades),
            "open_exposure_usd": open_exposure,
            "closed_positions": len(self.closed_trades),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": len(wins) / len(self.closed_trades) if self.closed_trades else 0.0,
            "total_pnl_usd": total_pnl,
            "total_invested_usd": total_invested,
            "roi_pct": (total_pnl / total_invested * 100) if total_invested else 0.0,
            "avg_win_usd": (sum(t["pnl_usd"] for t in wins) / len(wins)) if wins else 0.0,
            "avg_loss_usd": (sum(t["pnl_usd"] for t in losses) / len(losses)) if losses else 0.0,
        }

    def print_summary(self) -> None:
        """Print a formatted performance summary to the logger."""
        s = self.summary()
        logger.info(
            "\n══════════════════ Portfolio Summary ══════════════════\n"
            "  Open positions  : %d  ($%.2f exposure)\n"
            "  Closed trades   : %d  (wins: %d | losses: %d)\n"
            "  Win rate        : %.1f%%\n"
            "  Total PnL       : $%.2f\n"
            "  ROI             : %.2f%%\n"
            "══════════════════════════════════════════════════════",
            s["open_positions"], s["open_exposure_usd"],
            s["closed_positions"], s["win_count"], s["loss_count"],
            s["win_rate"] * 100,
            s["total_pnl_usd"],
            s["roi_pct"],
        )

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            state = {
                "open_trades": self.open_trades,
                "closed_trades": self.closed_trades,
                "saved_at": _now(),
            }
            PORTFOLIO_FILE.write_text(json.dumps(state, indent=2))
        except Exception as exc:
            logger.warning("Could not save portfolio: %s", exc)

    def _load(self) -> None:
        if not PORTFOLIO_FILE.exists():
            return
        try:
            state = json.loads(PORTFOLIO_FILE.read_text())
            self.open_trades = state.get("open_trades", {})
            self.closed_trades = state.get("closed_trades", [])
            logger.info(
                "Loaded portfolio: %d open, %d closed positions",
                len(self.open_trades),
                len(self.closed_trades),
            )
        except Exception as exc:
            logger.warning("Could not load portfolio file: %s", exc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _calc_pnl(trade: dict, exit_price: float) -> float:
        """
        For prediction markets:
          - If we bought YES at entry_price and exit_price=1.0 → we won full size
          - If exit_price=0.0 → we lost full size
          - Partial resolution → proportional
        """
        size = trade.get("size_usd", 0.0)
        entry = trade.get("entry_price", 0.5) or 0.5
        direction = trade.get("direction", "YES")

        if direction == "YES":
            # Shares bought = size / entry_price
            shares = size / entry
            pnl = shares * exit_price - size
        else:
            # Bought NO at (1 - market_yes_price)
            no_entry = 1.0 - entry
            shares = size / max(no_entry, 0.01)
            # NO resolves to 1 if YES resolves to 0
            no_exit = 1.0 - exit_price
            pnl = shares * no_exit - size

        return round(pnl, 4)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
