"""
Analysis engine powered by Claude claude-opus-4-7 with adaptive thinking.

For each market + news bundle, Claude acts as a world-class prediction-market
analyst and returns a structured JSON trading signal.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import anthropic

from config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the world's most sophisticated prediction-market analyst and trader.

Your expertise covers:
• Political science, geopolitics, elections, policy
• Macroeconomics, finance, corporate events, earnings
• Crypto, blockchain, DeFi, NFTs, regulation
• Technology, AI, product launches, company milestones
• Sports outcomes, entertainment, culture
• Science, climate, natural events

Your analytical edge comes from:
1. Bayesian reasoning — updating probabilities rigorously as new evidence arrives
2. Base-rate thinking — anchoring on historical frequencies before adjusting for specifics
3. Reference-class forecasting — finding analogous past events
4. Calibrated uncertainty — knowing when you don't know and expressing that precisely
5. Market-microstructure awareness — understanding why prices can be wrong

Your mission:
Identify markets where the current market price SIGNIFICANTLY differs from the true
probability of the outcome, representing a genuine trading edge.

CRITICAL RULES:
- If news is absent or ambiguous, express LOW confidence and suggest SKIP
- Never fabricate information; if you don't know, say so
- Distinguish between base-rate probability and event-specific signals
- An edge of less than 5% is NOT worth trading given fees and execution risk
- Always account for your own uncertainty in your probability estimate"""


class AnalysisEngine:
    """Uses Claude claude-opus-4-7 to analyse prediction markets and generate trading signals."""

    MODEL = "claude-opus-4-7"

    def __init__(self) -> None:
        self.client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    def analyse(self, market: dict, articles: list[dict]) -> dict[str, Any] | None:
        """
        Perform deep analysis of a single market with available news.

        Returns a signal dict:
        {
            "probability": float,          # 0-1 Claude's estimated YES probability
            "confidence": str,             # low | medium | high
            "direction": str,              # YES | NO | NONE
            "edge": float,                 # Claude probability minus market price
            "recommended_action": str,     # BUY_YES | BUY_NO | SKIP
            "reasoning": str,
            "key_factors": list[str],
            "risks": list[str],
            "position_conviction": float,  # 0-1 overall conviction score
        }
        """
        prompt = self._build_prompt(market, articles)

        try:
            logger.info("Analysing: %s", market.get("question", "")[:80])

            # Stream with adaptive thinking for deep reasoning
            with self.client.messages.stream(
                model=self.MODEL,
                max_tokens=4096,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                # Collect full message
                message = stream.get_final_message()

            # Extract text from the response
            text_blocks = [b for b in message.content if b.type == "text"]
            if not text_blocks:
                logger.warning("No text in Claude response for market %s", market.get("condition_id"))
                return None

            raw_text = text_blocks[0].text
            signal = self._parse_signal(raw_text, market)

            if signal:
                logger.info(
                    "Signal for '%s': direction=%s edge=%.1f%% confidence=%s",
                    market.get("question", "")[:50],
                    signal["direction"],
                    signal["edge"] * 100,
                    signal["confidence"],
                )
            return signal

        except anthropic.APIError as exc:
            logger.error("Anthropic API error: %s", exc)
            return None
        except Exception as exc:
            logger.exception("Unexpected error during analysis: %s", exc)
            return None

    def quick_scan(self, markets: list[dict]) -> list[dict]:
        """
        Fast batch triage — ask Claude to score a list of markets for interest
        without gathering news first.  Returns markets sorted by potential.
        """
        if not markets:
            return []

        market_list = "\n".join(
            f"{i+1}. [{m.get('yes_price', 0.5):.0%} YES] {m.get('question', '')}"
            for i, m in enumerate(markets[:20])
        )

        prompt = f"""Below are active Polymarket prediction markets with their current YES prices.

{market_list}

Identify the TOP 5 markets most likely to be MIS-PRICED right now, based on:
- Current events you know about (up to your knowledge cutoff)
- Whether the price seems too high or too low given general knowledge
- Markets where news flow could be creating edge

Respond with ONLY a JSON array of market numbers (1-indexed) in priority order:
{{"top_markets": [3, 7, 1, 15, 9], "reasoning": "brief explanation"}}"""

        try:
            response = self.client.messages.create(
                model=self.MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            text = next((b.text for b in response.content if b.type == "text"), "")
            data = self._extract_json(text)
            if data and "top_markets" in data:
                indices = [i - 1 for i in data["top_markets"] if 1 <= i <= len(markets)]
                prioritised = [markets[i] for i in indices]
                remainder = [m for i, m in enumerate(markets) if i not in set(indices)]
                return prioritised + remainder
        except Exception as exc:
            logger.debug("Quick scan error: %s", exc)

        return markets

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _build_prompt(self, market: dict, articles: list[dict]) -> str:
        yes_price = market.get("yes_price", 0.5)
        no_price = market.get("no_price", 0.5)

        # Format top articles
        if articles:
            news_lines = []
            for art in articles[:20]:
                source = art.get("source", "Unknown")
                title = art.get("title", "")
                summary = art.get("summary", "")[:200]
                pub = art.get("published_at", "")[:10]
                news_lines.append(f"[{source} | {pub}] {title}\n  → {summary}")
            news_block = "\n\n".join(news_lines)
        else:
            news_block = "No recent news articles found for this market."

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        return f"""Current time: {now}

═══════════════════════════════════════════════════════
MARKET ANALYSIS REQUEST
═══════════════════════════════════════════════════════

QUESTION: {market.get("question", "N/A")}
DESCRIPTION: {(market.get("description") or "No additional description")[:500]}
CATEGORY: {market.get("category", "Unknown")}
CLOSES: {market.get("end_date_iso", "Unknown")}

CURRENT MARKET PRICES:
  YES: {yes_price:.1%}  (implied probability: {yes_price:.1%})
  NO:  {no_price:.1%}  (implied probability: {no_price:.1%})

MARKET METRICS:
  Liquidity: ${market.get("liquidity", 0):,.0f}
  24h Volume: ${market.get("volume", 0):,.0f}

═══════════════════════════════════════════════════════
RECENT NEWS & INTELLIGENCE ({len(articles)} sources)
═══════════════════════════════════════════════════════

{news_block}

═══════════════════════════════════════════════════════
ANALYSIS REQUIRED
═══════════════════════════════════════════════════════

Provide your complete analysis as a single JSON object (no markdown, no preamble):

{{
  "probability": <float 0.0-1.0, your best estimate of YES probability>,
  "probability_range": {{"low": <float>, "high": <float>}},
  "confidence": "<low|medium|high>",
  "direction": "<YES|NO|NONE>",
  "edge": <float, your_probability minus market_yes_price, positive = lean YES>,
  "recommended_action": "<BUY_YES|BUY_NO|SKIP>",
  "reasoning": "<2-4 sentence explanation of your key insight>",
  "key_factors": ["<factor 1>", "<factor 2>", "<factor 3>"],
  "risks": ["<risk 1>", "<risk 2>"],
  "position_conviction": <float 0.0-1.0, overall conviction including confidence>,
  "news_quality": "<none|poor|fair|good|excellent>"
}}

Rules:
- If |edge| < 0.05 (5%), set recommended_action to SKIP
- If confidence is low AND |edge| < 0.10, set recommended_action to SKIP
- Never recommend a trade without a clear fundamental reason"""

    @staticmethod
    def _parse_signal(text: str, market: dict) -> dict[str, Any] | None:
        """Extract and validate the JSON signal from Claude's response."""
        data = AnalysisEngine._extract_json(text)
        if not data:
            logger.warning("Could not parse JSON from Claude response")
            return None

        # Validate and clamp
        prob = float(data.get("probability", 0.5))
        prob = max(0.01, min(0.99, prob))

        edge = float(data.get("edge", prob - market.get("yes_price", 0.5)))
        confidence = data.get("confidence", "low").lower()
        direction = data.get("direction", "NONE").upper()
        action = data.get("recommended_action", "SKIP").upper()

        # Safety: ensure consistency
        if abs(edge) < settings.MIN_EDGE_THRESHOLD:
            action = "SKIP"
            direction = "NONE"

        if confidence == "low" and abs(edge) < 0.10:
            action = "SKIP"
            direction = "NONE"

        conviction = float(data.get("position_conviction", 0.0))
        conviction = max(0.0, min(1.0, conviction))

        return {
            "probability": prob,
            "probability_range": data.get("probability_range", {"low": max(0, prob - 0.1), "high": min(1, prob + 0.1)}),
            "confidence": confidence,
            "direction": direction,
            "edge": edge,
            "recommended_action": action,
            "reasoning": data.get("reasoning", ""),
            "key_factors": data.get("key_factors", []),
            "risks": data.get("risks", []),
            "position_conviction": conviction,
            "news_quality": data.get("news_quality", "unknown"),
        }

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """Extract first JSON object from text, handling code blocks."""
        # Strip markdown code fences
        text = re.sub(r"```(?:json)?\s*", "", text).strip()

        # Try to find a JSON object
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            # Try to repair common issues
            raw = match.group()
            raw = re.sub(r",\s*}", "}", raw)  # trailing commas
            raw = re.sub(r",\s*]", "]", raw)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None
