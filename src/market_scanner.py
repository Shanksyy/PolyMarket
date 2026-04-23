"""
Scans Polymarket for active, liquid markets and scores them for trading opportunity.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

from config import settings

logger = logging.getLogger(__name__)


class MarketScanner:
    """Fetches and filters Polymarket markets via the Gamma REST API."""

    GAMMA_MARKETS = f"{settings.POLYMARKET_GAMMA_HOST}/markets"
    CLOB_MARKETS = f"{settings.POLYMARKET_HOST}/markets"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    # ── Public API ────────────────────────────────────────────────────────────

    def get_tradeable_markets(self) -> list[dict[str, Any]]:
        """
        Return markets filtered by liquidity, time-to-expiry and active status,
        ranked by opportunity score (spread between extreme prices is largest).
        """
        raw = self._fetch_gamma_markets()
        filtered = [m for m in raw if self._is_tradeable(m)]
        scored = sorted(filtered, key=self._opportunity_score, reverse=True)
        logger.info("Found %d tradeable markets (from %d total)", len(scored), len(raw))
        return scored[: settings.TOP_MARKETS_TO_ANALYSE * 3]  # return pool for analysis

    def get_market_detail(self, condition_id: str) -> dict[str, Any] | None:
        """Fetch a single market's detail from the Gamma API."""
        try:
            resp = self.session.get(
                self.GAMMA_MARKETS,
                params={"condition_id": condition_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data:
                return data[0] if isinstance(data, list) else data
        except Exception as exc:
            logger.warning("Could not fetch market detail for %s: %s", condition_id, exc)
        return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _fetch_gamma_markets(self) -> list[dict[str, Any]]:
        """Fetch all active markets from Gamma API with pagination."""
        markets: list[dict] = []
        offset = 0
        limit = 100

        while True:
            try:
                resp = self.session.get(
                    self.GAMMA_MARKETS,
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": limit,
                        "offset": offset,
                        "order": "volume24hr",
                        "ascending": "false",
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                markets.extend(batch if isinstance(batch, list) else batch.get("markets", []))
                if len(batch) < limit:
                    break
                offset += limit
                time.sleep(0.2)  # be polite to the API
            except Exception as exc:
                logger.error("Error fetching markets at offset %d: %s", offset, exc)
                break

        return markets

    def _is_tradeable(self, market: dict) -> bool:
        """Filter predicate — returns True when we want to consider this market."""
        # Must have tokens (YES/NO outcomes)
        tokens = market.get("tokens") or market.get("outcomes", [])
        if not tokens or len(tokens) < 2:
            return False

        # Liquidity check
        liquidity = float(market.get("liquidity", 0) or 0)
        if liquidity < settings.MIN_LIQUIDITY_USD:
            return False

        # Time-to-expiry check
        days = self._days_to_expiry(market)
        if days is None:
            return False
        if not (settings.MIN_DAYS_TO_EXPIRY <= days <= settings.MAX_DAYS_TO_EXPIRY):
            return False

        # Skip markets that are already resolved/closed
        if market.get("closed") or market.get("resolved"):
            return False

        return True

    def _opportunity_score(self, market: dict) -> float:
        """
        Higher score = more interesting to analyse.
        We prefer: high volume, reasonable liquidity, mid-range prices (most uncertainty).
        """
        liquidity = float(market.get("liquidity", 0) or 0)
        volume_24h = float(market.get("volume24hr", 0) or market.get("volume", 0) or 0)

        # Get YES price — markets where YES ≈ 0.5 are hardest to price = most edge potential
        yes_price = self._get_yes_price(market)
        # Uncertainty score peaks at 0.5 (50/50) and goes to 0 at certainties
        uncertainty = 1.0 - abs(yes_price - 0.5) * 2 if yes_price is not None else 0.3

        score = (
            (volume_24h / 10_000) * 0.4
            + (liquidity / 100_000) * 0.3
            + uncertainty * 0.3
        )
        return score

    def _get_yes_price(self, market: dict) -> float | None:
        """Extract YES token price from various market formats."""
        # Gamma API format
        tokens = market.get("tokens", [])
        for token in tokens:
            if str(token.get("outcome", "")).upper() == "YES":
                price = token.get("price")
                if price is not None:
                    return float(price)

        # Alternative: outcomes list with prices
        outcomes = market.get("outcomePrices", [])
        if outcomes:
            try:
                return float(outcomes[0])
            except (ValueError, TypeError):
                pass

        return None

    def _days_to_expiry(self, market: dict) -> float | None:
        """Return days until market closes, or None if unparseable."""
        end_str = market.get("endDateIso") or market.get("end_date_iso")
        if not end_str:
            return None
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return (end_dt - now).total_seconds() / 86_400
        except Exception:
            return None

    @staticmethod
    def enrich_market(market: dict) -> dict:
        """
        Normalise a raw Gamma market dict into a clean, standard shape
        used by the rest of the bot.
        """
        tokens = market.get("tokens", [])
        yes_price = no_price = None
        yes_token_id = no_token_id = None

        for t in tokens:
            outcome = str(t.get("outcome", "")).upper()
            price = float(t.get("price", 0) or 0)
            token_id = t.get("token_id") or t.get("tokenId", "")
            if outcome == "YES":
                yes_price = price
                yes_token_id = token_id
            elif outcome == "NO":
                no_price = price
                no_token_id = token_id

        # Fallback from outcomePrices
        if yes_price is None:
            prices = market.get("outcomePrices", [])
            if len(prices) >= 2:
                yes_price = float(prices[0])
                no_price = float(prices[1])

        return {
            "condition_id": market.get("conditionId") or market.get("condition_id", ""),
            "question": market.get("question", "Unknown"),
            "description": market.get("description", ""),
            "category": market.get("category", ""),
            "tags": market.get("tags", []),
            "yes_price": yes_price or 0.5,
            "no_price": no_price or 0.5,
            "yes_token_id": yes_token_id or "",
            "no_token_id": no_token_id or "",
            "liquidity": float(market.get("liquidity", 0) or 0),
            "volume": float(market.get("volume24hr", 0) or market.get("volume", 0) or 0),
            "end_date_iso": market.get("endDateIso") or market.get("end_date_iso", ""),
            "url": market.get("url") or market.get("marketMakerAddress", ""),
            "_raw": market,
        }
