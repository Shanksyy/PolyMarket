"""
Executes trades on Polymarket via the py-clob-client.
Falls back to DRY_RUN mode (no real money) when configured or when the
private key is absent.
"""
from __future__ import annotations

import logging
from typing import Any

from config import settings

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Wraps py-clob-client and handles order placement with retries."""

    def __init__(self) -> None:
        self._client = None
        self._dry_run = settings.DRY_RUN or not settings.POLYGON_PRIVATE_KEY
        if self._dry_run:
            logger.warning("⚠️  DRY RUN mode — no real trades will be placed")
        else:
            self._init_client()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_portfolio_value(self) -> float:
        """Return estimated total portfolio value (USDC balance + positions)."""
        if self._dry_run or not self._client:
            return 1_000.0  # simulated starting capital
        try:
            # py-clob-client does not expose a direct balance method;
            # we query the subgraph or derive from positions.
            positions = self._client.get_positions()
            total = sum(float(p.get("currentValue", 0)) for p in positions)
            return max(total, 10.0)
        except Exception as exc:
            logger.warning("Could not fetch portfolio value: %s", exc)
            return 500.0

    def place_order(
        self,
        market: dict,
        direction: str,
        size_usd: float,
        signal: dict,
    ) -> dict[str, Any]:
        """
        Place a market order.

        Args:
            market: Enriched market dict from MarketScanner
            direction: "YES" or "NO"
            size_usd: Amount in USD to spend
            signal: Full analysis signal (used for logging)

        Returns:
            Order result dict with status, order_id, fill details.
        """
        token_id = (
            market.get("yes_token_id") if direction == "YES" else market.get("no_token_id")
        )
        price = market.get("yes_price") if direction == "YES" else market.get("no_price")

        if self._dry_run:
            return self._simulate_order(market, direction, size_usd, price, token_id)

        return self._execute_live_order(market, direction, size_usd, price, token_id)

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID."""
        if self._dry_run:
            logger.info("[DRY RUN] Would cancel order %s", order_id)
            return True
        try:
            self._client.cancel(order_id)  # type: ignore[union-attr]
            return True
        except Exception as exc:
            logger.error("Failed to cancel order %s: %s", order_id, exc)
            return False

    def get_open_positions(self) -> list[dict]:
        """Return raw positions from Polymarket."""
        if self._dry_run or not self._client:
            return []
        try:
            return self._client.get_positions() or []  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("Could not fetch positions: %s", exc)
            return []

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _init_client(self) -> None:
        """Initialise py-clob-client with the user's Polygon private key."""
        try:
            from py_clob_client.client import ClobClient  # noqa: PLC0415
            from py_clob_client.clob_types import ApiCreds  # noqa: PLC0415

            client = ClobClient(
                host=settings.POLYMARKET_HOST,
                key=settings.POLYGON_PRIVATE_KEY,
                chain_id=settings.CHAIN_ID,
            )

            # Use saved API creds if available, otherwise derive new ones
            if all([settings.POLYMARKET_API_KEY,
                    settings.POLYMARKET_API_SECRET,
                    settings.POLYMARKET_API_PASSPHRASE]):
                creds = ApiCreds(
                    api_key=settings.POLYMARKET_API_KEY,
                    api_secret=settings.POLYMARKET_API_SECRET,
                    api_passphrase=settings.POLYMARKET_API_PASSPHRASE,
                )
            else:
                logger.info("Deriving Polymarket API credentials from private key…")
                creds = client.create_or_derive_api_creds()
                logger.info(
                    "\n📋 Save these to your .env:\n"
                    "POLYMARKET_API_KEY=%s\n"
                    "POLYMARKET_API_SECRET=%s\n"
                    "POLYMARKET_API_PASSPHRASE=%s",
                    creds.api_key,
                    creds.api_secret,
                    creds.api_passphrase,
                )

            client.set_api_creds(creds)
            self._client = client
            logger.info("✅ Polymarket client initialised (live trading)")

        except ImportError:
            logger.error(
                "py-clob-client not installed. Run: pip install py-clob-client"
            )
            self._dry_run = True
        except Exception as exc:
            logger.error("Failed to initialise Polymarket client: %s", exc)
            self._dry_run = True

    def _execute_live_order(
        self,
        market: dict,
        direction: str,
        size_usd: float,
        price: float | None,
        token_id: str,
    ) -> dict[str, Any]:
        """Place a real order on Polymarket."""
        from py_clob_client.clob_types import MarketOrderArgs  # noqa: PLC0415

        try:
            args = MarketOrderArgs(
                token_id=token_id,
                amount=size_usd,
            )
            result = self._client.create_market_order(args)  # type: ignore[union-attr]
            logger.info(
                "✅ ORDER PLACED: %s %s $%.2f | id=%s",
                direction, market.get("question", "")[:40], size_usd,
                result.get("orderID", "?"),
            )
            return {
                "status": "filled",
                "order_id": result.get("orderID", ""),
                "direction": direction,
                "size_usd": size_usd,
                "fill_price": price,
                "market_question": market.get("question", ""),
                "condition_id": market.get("condition_id", ""),
                "dry_run": False,
            }
        except Exception as exc:
            logger.error("Order failed: %s", exc)
            return {
                "status": "error",
                "error": str(exc),
                "direction": direction,
                "size_usd": size_usd,
                "dry_run": False,
            }

    @staticmethod
    def _simulate_order(
        market: dict,
        direction: str,
        size_usd: float,
        price: float | None,
        token_id: str,
    ) -> dict[str, Any]:
        """Return a fake successful order without touching real funds."""
        import uuid

        fake_id = str(uuid.uuid4())[:8]
        logger.info(
            "🧪 [DRY RUN] %s %s $%.2f @ %.1f%% | id=%s",
            direction,
            market.get("question", "")[:50],
            size_usd,
            (price or 0.5) * 100,
            fake_id,
        )
        return {
            "status": "simulated",
            "order_id": f"dry_{fake_id}",
            "direction": direction,
            "size_usd": size_usd,
            "fill_price": price,
            "market_question": market.get("question", ""),
            "condition_id": market.get("condition_id", ""),
            "token_id": token_id,
            "dry_run": True,
        }
