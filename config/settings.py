import os
from dotenv import load_dotenv

load_dotenv()

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

# ── Polymarket ────────────────────────────────────────────────────────────────
POLYGON_PRIVATE_KEY: str = os.getenv("POLYGON_PRIVATE_KEY", "")
POLYMARKET_API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET: str = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE: str = os.getenv("POLYMARKET_API_PASSPHRASE", "")
POLYMARKET_HOST: str = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
POLYMARKET_GAMMA_HOST: str = "https://gamma-api.polymarket.com"
CHAIN_ID: int = 137  # Polygon mainnet

# ── Twitter / X ───────────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN: str = os.getenv("TWITTER_BEARER_TOKEN", "")

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_API_ID: str = os.getenv("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH: str = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_CHANNELS: list[str] = [
    c.strip()
    for c in os.getenv("TELEGRAM_CHANNELS", "polymarket,predictionmarkets").split(",")
    if c.strip()
]

# ── News ──────────────────────────────────────────────────────────────────────
NEWS_API_KEY: str = os.getenv("NEWS_API_KEY", "")
CRYPTOPANIC_API_KEY: str = os.getenv("CRYPTOPANIC_API_KEY", "")

# ── Risk Management ───────────────────────────────────────────────────────────
MAX_POSITION_SIZE_USD: float = float(os.getenv("MAX_POSITION_SIZE_USD", "50"))
MAX_TOTAL_EXPOSURE_USD: float = float(os.getenv("MAX_TOTAL_EXPOSURE_USD", "500"))
MIN_EDGE_THRESHOLD: float = float(os.getenv("MIN_EDGE_THRESHOLD", "0.07"))
MIN_LIQUIDITY_USD: float = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MAX_POSITIONS: int = int(os.getenv("MAX_POSITIONS", "10"))
# Fraction of Kelly criterion to use (0.25 = quarter Kelly, safer)
KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.25"))

# ── Bot Behaviour ─────────────────────────────────────────────────────────────
SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
MIN_CONFIDENCE: str = os.getenv("MIN_CONFIDENCE", "medium")  # low | medium | high
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# Markets closing within this range (days) are prime targets
MIN_DAYS_TO_EXPIRY: int = 1
MAX_DAYS_TO_EXPIRY: int = 45

# How many markets to deeply analyse per scan cycle
TOP_MARKETS_TO_ANALYSE: int = 8

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
