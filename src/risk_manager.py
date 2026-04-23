"""
Risk management: position sizing (Kelly criterion), exposure limits,
and trade approval gating.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

CONFIDENCE_MULTIPLIER = {"low": 0.3, "medium": 0.7, "high": 1.0}


class RiskManager:
    """Stateful risk checker.  Call approve() before every trade."""

    def __init__(self) -> None:
        # open_positions: condition_id -> {size_usd, direction, entry_price}
        self.open_positions: dict[str, dict] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def approve(
        self,
        market: dict,
        signal: dict,
        portfolio_value: float,
    ) -> tuple[bool, float, str]:
        """
        Decide whether to take a trade and how much to size it.

        Returns:
            (approved: bool, size_usd: float, reason: str)
        """
        cid = market.get("condition_id", "")

        # --- Gate 1: Already have a position ---
        if cid in self.open_positions:
            return False, 0.0, "Already have a position in this market"

        # --- Gate 2: Max open positions ---
        if len(self.open_positions) >= settings.MAX_POSITIONS:
            return False, 0.0, f"Max positions ({settings.MAX_POSITIONS}) already open"

        # --- Gate 3: Total exposure ---
        current_exposure = sum(p["size_usd"] for p in self.open_positions.values())
        if current_exposure >= settings.MAX_TOTAL_EXPOSURE_USD:
            return False, 0.0, (
                f"Total exposure ${current_exposure:.0f} >= limit "
                f"${settings.MAX_TOTAL_EXPOSURE_USD:.0f}"
            )

        # --- Gate 4: Minimum confidence ---
        confidence = signal.get("confidence", "low")
        min_conf = settings.CONFIDENCE_ORDER.get(settings.MIN_CONFIDENCE, 1)
        if settings.CONFIDENCE_ORDER.get(confidence, 0) < min_conf:
            return False, 0.0, f"Confidence '{confidence}' below minimum '{settings.MIN_CONFIDENCE}'"

        # --- Gate 5: Edge threshold ---
        edge = abs(signal.get("edge", 0.0))
        if edge < settings.MIN_EDGE_THRESHOLD:
            return False, 0.0, f"Edge {edge:.1%} below threshold {settings.MIN_EDGE_THRESHOLD:.1%}"

        # --- Gate 6: Signal must say BUY ---
        action = signal.get("recommended_action", "SKIP")
        if action not in ("BUY_YES", "BUY_NO"):
            return False, 0.0, f"Signal action is {action}"

        # --- Size calculation ---
        size = self._kelly_size(signal, portfolio_value, current_exposure)
        if size < 1.0:
            return False, 0.0, f"Kelly size ${size:.2f} too small to be worth trading"

        reason = (
            f"Approved: edge={edge:.1%} confidence={confidence} "
            f"size=${size:.2f}"
        )
        return True, size, reason

    def record_open(self, condition_id: str, direction: str, size_usd: float, entry_price: float) -> None:
        self.open_positions[condition_id] = {
            "direction": direction,
            "size_usd": size_usd,
            "entry_price": entry_price,
        }
        logger.info("Position opened: %s %s $%.2f @ %.2f", condition_id[:10], direction, size_usd, entry_price)

    def record_close(self, condition_id: str) -> None:
        if condition_id in self.open_positions:
            del self.open_positions[condition_id]
            logger.info("Position closed: %s", condition_id[:10])

    @property
    def total_exposure(self) -> float:
        return sum(p["size_usd"] for p in self.open_positions.values())

    @property
    def available_capital(self) -> float:
        return max(0.0, settings.MAX_TOTAL_EXPOSURE_USD - self.total_exposure)

    # ── Kelly criterion ───────────────────────────────────────────────────────

    def _kelly_size(
        self,
        signal: dict,
        portfolio_value: float,
        current_exposure: float,
    ) -> float:
        """
        Fractional Kelly bet size in USD.

        Kelly formula for binary bets:
            f = (b*p - q) / b
        where:
            p = probability of winning
            q = 1 - p = probability of losing
            b = net odds (payout - 1), for prediction markets ≈ (1/price - 1)
        """
        direction = signal.get("direction", "NONE")
        prob = signal.get("probability", 0.5)
        confidence = signal.get("confidence", "low")
        conviction = signal.get("position_conviction", 0.5)

        if direction == "YES":
            price = signal.get("probability", 0.5)  # market price we'll pay
            # Use market price as the cost
            market_price = max(0.01, min(0.99, prob - signal.get("edge", 0.05)))
            b = (1.0 / max(market_price, 0.01)) - 1.0
            p = prob
        elif direction == "NO":
            # Buying NO means we think YES is overpriced → p(NO wins) = 1 - prob
            p = 1.0 - prob
            no_price = max(0.01, 1.0 - (prob - signal.get("edge", 0.05)))
            b = (1.0 / no_price) - 1.0
        else:
            return 0.0

        q = 1.0 - p
        if b <= 0:
            return 0.0

        kelly_fraction = (b * p - q) / b
        if kelly_fraction <= 0:
            return 0.0

        # Apply fractional Kelly, confidence scaling, and conviction
        conf_mult = CONFIDENCE_MULTIPLIER.get(confidence, 0.3)
        adjusted_fraction = kelly_fraction * settings.KELLY_FRACTION * conf_mult * conviction

        # Size against available capital budget
        base = min(portfolio_value, settings.MAX_TOTAL_EXPOSURE_USD)
        raw_size = base * adjusted_fraction

        # Hard caps
        remaining_budget = settings.MAX_TOTAL_EXPOSURE_USD - current_exposure
        size = min(raw_size, settings.MAX_POSITION_SIZE_USD, remaining_budget)
        size = max(0.0, size)

        logger.debug(
            "Kelly: p=%.2f b=%.2f kelly_f=%.3f adj=%.3f raw=$%.2f final=$%.2f",
            p, b, kelly_fraction, adjusted_fraction, raw_size, size,
        )
        return round(size, 2)
