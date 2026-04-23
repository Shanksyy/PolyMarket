"""
Bullpen CLI integration — pulls smart money signals, leaderboard data,
tracker trades, and real portfolio P&L via the Bullpen CLI tool.

Install Bullpen: npm install -g @bullpenfi/cli
Login:          bullpen login
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any

logger = logging.getLogger(__name__)


class BullpenTracker:
    """Wraps the Bullpen CLI via subprocess calls."""

    # On Windows, npm installs a .cmd shim
    _CMD = "bullpen.cmd" if sys.platform == "win32" else "bullpen"

    def is_available(self) -> bool:
        """Return True if the bullpen CLI is installed and reachable."""
        try:
            result = subprocess.run(
                [self._CMD, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    # ── Core data fetchers ────────────────────────────────────────────────────

    def get_smart_money(
        self,
        signal_type: str = "aggregated",
        category: str = "overall",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Return smart money signals — what top traders are buying/selling.
        signal_type: aggregated | top_traders | new_wallet
        category: overall | politics | sports | crypto | culture | economics | tech | finance
        """
        data = self._run(
            "polymarket", "data", "smart-money",
            "--type", signal_type,
            "--category", category,
            "--limit", str(limit),
        )
        if not data:
            return []
        return data.get("data", data.get("items", []))

    def get_leaderboard(self, period: str = "week", limit: int = 25) -> list[dict[str, Any]]:
        """
        Top Polymarket traders by P&L.
        period: day | week | month | all
        """
        data = self._run(
            "polymarket", "data", "leaderboard",
            "--period", period,
            "--limit", str(limit),
        )
        if not data:
            return []
        return data.get("traders", data.get("items", []))

    def get_tracker_trades(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent trades from wallets you follow in Bullpen."""
        data = self._run("tracker", "trades", "--limit", str(limit))
        if not data:
            return []
        return data.get("items", data.get("trades", []))

    def get_portfolio_pnl(self) -> dict[str, Any]:
        """Realised and unrealised P&L from your Polymarket wallet."""
        return self._run("portfolio", "pnl") or {}

    def get_portfolio_balances(self) -> dict[str, Any]:
        """Current balances across all chains."""
        return self._run("portfolio", "balances") or {}

    def get_following(self) -> list[dict[str, Any]]:
        """List of Polymarket addresses you follow."""
        data = self._run("tracker", "following")
        if not data:
            return []
        return data.get("items", data.get("following", []))

    def follow_address(self, address: str, nickname: str = "") -> bool:
        """Follow a Polymarket address for trade notifications."""
        args = ["tracker", "follow", address]
        if nickname:
            args += ["--notify-trades", "true"]
        result = self._run(*args)
        return bool(result and result.get("ok"))

    def get_trader_feed(self, address: str, limit: int = 20) -> list[dict[str, Any]]:
        """Get recent trades for a specific address."""
        data = self._run("tracker", "feed", "--address", address, "--limit", str(limit))
        if not data:
            return []
        return data.get("items", data.get("trades", []))

    # ── Signal conversion for analysis engine ─────────────────────────────────

    def smart_money_as_articles(self, market: dict) -> list[dict]:
        """
        Convert smart money data into article-style dicts so the
        analysis engine can incorporate them as news signals.
        """
        try:
            signals = self.get_smart_money()
            tracker_trades = self.get_tracker_trades()
        except Exception as exc:
            logger.debug("Bullpen smart money fetch failed: %s", exc)
            return []

        articles = []
        question = market.get("question", "").lower()

        for sig in signals:
            title = sig.get("market", sig.get("title", sig.get("name", "")))
            if not title:
                continue
            # Only include signals relevant to this market
            if not any(kw in str(sig).lower() for kw in question.split()[:3]):
                continue
            articles.append({
                "source": "Bullpen Smart Money",
                "title": f"Smart money signal: {title}",
                "summary": (
                    f"Type: {sig.get('type', 'aggregated')} | "
                    f"Direction: {sig.get('direction', sig.get('side', '?'))} | "
                    f"Size: ${sig.get('size', sig.get('amount', '?'))}"
                ),
                "url": "",
                "published_at": sig.get("timestamp", sig.get("created_at", "")),
            })

        for trade in tracker_trades:
            market_title = trade.get("market", trade.get("title", ""))
            if not market_title:
                continue
            if not any(kw in str(trade).lower() for kw in question.split()[:3]):
                continue
            side = trade.get("side", trade.get("action", ""))
            size = trade.get("size", trade.get("amount", "?"))
            price = trade.get("price", "?")
            articles.append({
                "source": "Bullpen Tracker",
                "title": f"Tracked trader {side} on: {market_title}",
                "summary": f"A followed smart-money trader placed a {side} trade of ${size} at {price}.",
                "url": "",
                "published_at": trade.get("timestamp", trade.get("created_at", "")),
            })

        return articles

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run(self, *args: str) -> dict | None:
        """Run a bullpen command and return parsed JSON output."""
        cmd = [self._CMD] + list(args) + ["--output", "json"]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                logger.debug("Bullpen command %s failed: %s", args, result.stderr[:200])
                return None
            return json.loads(result.stdout)
        except FileNotFoundError:
            logger.debug("Bullpen CLI not installed — skipping")
            return None
        except subprocess.TimeoutExpired:
            logger.debug("Bullpen command timed out: %s", args)
            return None
        except json.JSONDecodeError as exc:
            logger.debug("Bullpen JSON parse error: %s", exc)
            return None
        except Exception as exc:
            logger.debug("Bullpen error: %s", exc)
            return None
