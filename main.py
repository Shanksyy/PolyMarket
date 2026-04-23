#!/usr/bin/env python3
"""
PolyMarket Trading Bot
══════════════════════════════════════════════════════════════════════════════
Scans Polymarket for mispriced prediction markets, gathers news from Twitter/X,
Telegram, Reddit, RSS feeds and major news APIs, then uses Claude claude-opus-4-7 with
adaptive thinking to identify genuine trading edges and place orders.

Usage:
    cp .env.example .env        # then fill in your API keys
    pip install -r requirements.txt
    python main.py              # starts in DRY_RUN mode by default
══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from config import settings
from src.market_scanner import MarketScanner
from src.news_aggregator import NewsAggregator
from src.analysis_engine import AnalysisEngine
from src.trade_executor import TradeExecutor
from src.risk_manager import RiskManager
from src.portfolio_tracker import PortfolioTracker

# ── Logging setup ─────────────────────────────────────────────────────────────
try:
    import colorlog

    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "red,bg_white",
        },
    ))
    logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL, logging.INFO), handlers=[handler])
except ImportError:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

logger = logging.getLogger(__name__)
console = Console()


# ── Bot ───────────────────────────────────────────────────────────────────────


class PolyMarketBot:
    """Main bot orchestrator."""

    def __init__(self) -> None:
        self._validate_config()
        self.scanner = MarketScanner()
        self.news = NewsAggregator()
        self.engine = AnalysisEngine()
        self.executor = TradeExecutor()
        self.risk = RiskManager()
        self.portfolio = PortfolioTracker()
        self._running = False

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the main trading loop."""
        self._print_banner()

        self._running = True
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        cycle = 0
        while self._running:
            cycle += 1
            logger.info("═══ Scan cycle #%d ═══", cycle)

            try:
                asyncio.run(self._scan_and_trade())
            except KeyboardInterrupt:
                break
            except Exception as exc:
                logger.exception("Unhandled error in scan cycle: %s", exc)

            self.portfolio.print_summary()

            if not self._running:
                break

            wait = settings.SCAN_INTERVAL_MINUTES * 60
            logger.info(
                "Next scan in %d minutes (%s)",
                settings.SCAN_INTERVAL_MINUTES,
                datetime.now(timezone.utc).strftime("%H:%M UTC"),
            )
            # Interruptible sleep
            for _ in range(wait):
                if not self._running:
                    break
                time.sleep(1)

        logger.info("Bot shut down gracefully.")

    async def _scan_and_trade(self) -> None:
        """One full scan + analysis + trade cycle."""

        # 1. Fetch and filter markets
        logger.info("Fetching Polymarket markets…")
        raw_markets = self.scanner.get_tradeable_markets()
        if not raw_markets:
            logger.warning("No tradeable markets found — check connectivity")
            return

        # Normalise into our standard shape
        markets = [MarketScanner.enrich_market(m) for m in raw_markets]
        logger.info("Found %d candidate markets", len(markets))

        # 2. Quick-scan triage (Claude identifies most promising markets)
        if len(markets) > settings.TOP_MARKETS_TO_ANALYSE:
            markets = self.engine.quick_scan(markets)

        # 3. Deep analyse top N markets in parallel
        top = markets[: settings.TOP_MARKETS_TO_ANALYSE]
        await self._analyse_and_trade_batch(top)

    async def _analyse_and_trade_batch(self, markets: list[dict]) -> None:
        """Analyse markets concurrently and trade on signals."""

        async def process(market: dict) -> None:
            try:
                # Gather news in parallel with analysis setup
                articles = await self.news.gather(market)

                # Deep Claude analysis (blocking — run in thread pool)
                loop = asyncio.get_event_loop()
                signal_data = await loop.run_in_executor(
                    None, self.engine.analyse, market, articles
                )

                if not signal_data:
                    return

                self._print_signal(market, signal_data)

                # Skip trades
                if signal_data.get("recommended_action") not in ("BUY_YES", "BUY_NO"):
                    return

                # Risk approval and sizing
                portfolio_value = self.executor.get_portfolio_value()
                approved, size_usd, reason = self.risk.approve(
                    market, signal_data, portfolio_value
                )

                if not approved:
                    logger.info("Trade DECLINED: %s", reason)
                    return

                direction = signal_data["direction"]
                logger.info(
                    "🎯 TRADING: %s %s $%.2f (%s)",
                    direction,
                    market.get("question", "")[:50],
                    size_usd,
                    reason,
                )

                # Execute
                result = self.executor.place_order(market, direction, size_usd, signal_data)

                if result.get("status") in ("filled", "simulated"):
                    self.risk.record_open(
                        market.get("condition_id", ""),
                        direction,
                        size_usd,
                        result.get("fill_price") or 0.5,
                    )
                    self.portfolio.record_open(result, market, signal_data)

            except Exception as exc:
                logger.exception("Error processing market %s: %s", market.get("question", "")[:40], exc)

        # Process all markets concurrently, but limit parallelism to avoid
        # rate-limiting the Anthropic API
        sem = asyncio.Semaphore(3)

        async def bounded(m: dict) -> None:
            async with sem:
                await process(m)

        await asyncio.gather(*[bounded(m) for m in markets])

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        mode = "[red]LIVE TRADING[/red]" if not settings.DRY_RUN else "[yellow]DRY RUN[/yellow]"
        console.print(Panel(
            f"""[bold cyan]PolyMarket AI Trading Bot[/bold cyan]

Model      : Claude claude-opus-4-7 (adaptive thinking)
Mode       : {mode}
Edge limit : >{settings.MIN_EDGE_THRESHOLD:.0%}
Max size   : ${settings.MAX_POSITION_SIZE_USD:.0f}/position
Exposure   : max ${settings.MAX_TOTAL_EXPOSURE_USD:.0f} total
Scan every : {settings.SCAN_INTERVAL_MINUTES} minutes""",
            title="⚡ Bot Starting",
            border_style="cyan",
        ))

    def _print_signal(self, market: dict, signal: dict) -> None:
        direction = signal.get("direction", "NONE")
        action = signal.get("recommended_action", "SKIP")
        color = {"BUY_YES": "green", "BUY_NO": "red"}.get(action, "dim")

        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        table.add_column("Key", style="bold")
        table.add_column("Value")

        table.add_row("Market", market.get("question", "")[:80])
        table.add_row("YES price", f"{market.get('yes_price', 0):.1%}")
        table.add_row("Claude prob", f"{signal.get('probability', 0):.1%}")
        table.add_row("Edge", f"{signal.get('edge', 0):+.1%}")
        table.add_row("Confidence", signal.get("confidence", "?"))
        table.add_row("Action", f"[{color}]{action}[/{color}]")
        table.add_row("Reason", signal.get("reasoning", "")[:120])

        console.print(Panel(table, border_style=color))

    @staticmethod
    def _validate_config() -> None:
        if not settings.ANTHROPIC_API_KEY:
            logger.critical("ANTHROPIC_API_KEY is not set — cannot run analysis engine")
            sys.exit(1)

        if not settings.DRY_RUN and not settings.POLYGON_PRIVATE_KEY:
            logger.critical(
                "DRY_RUN=false but POLYGON_PRIVATE_KEY is not set — "
                "set DRY_RUN=true or provide a private key"
            )
            sys.exit(1)

        logger.info(
            "Config OK | DRY_RUN=%s | min_edge=%.0f%% | max_pos=$%.0f",
            settings.DRY_RUN,
            settings.MIN_EDGE_THRESHOLD * 100,
            settings.MAX_POSITION_SIZE_USD,
        )

    def _handle_shutdown(self, *_) -> None:
        logger.info("Shutdown signal received — finishing current cycle…")
        self._running = False


# ── Entry point ───────────────────────────────────────────────────────────────


if __name__ == "__main__":
    bot = PolyMarketBot()
    bot.run()
