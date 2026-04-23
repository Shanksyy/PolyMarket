"""
Gathers news and social-media signals from multiple sources:
  - Google News RSS (free, no key needed)
  - NewsAPI (optional key)
  - CryptoPanic (optional key, best for crypto markets)
  - Reddit (free)
  - Twitter/X (optional bearer token)
  - Telegram public channels (optional Telegram account)
  - Direct web-scraping of Reuters, AP, BBC
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

import aiohttp
import feedparser

from config import settings

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _clean(text: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _excerpt(text: str, max_chars: int = 300) -> str:
    text = _clean(text)
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


# ── Main aggregator ───────────────────────────────────────────────────────────


class NewsAggregator:
    """Async news gatherer — collects from all configured sources in parallel."""

    def __init__(self) -> None:
        self._twitter_client = None
        self._telegram_client = None
        self._tg_started = False

    async def gather(self, market: dict) -> list[dict[str, Any]]:
        """
        Gather relevant news for a market.  Returns a list of article dicts:
          { source, title, summary, url, published_at, relevance_score }
        """
        question = market.get("question", "")
        keywords = self._extract_keywords(question)

        tasks = [
            self._google_news_rss(keywords),
            self._direct_rss_feeds(),
            self._newsapi(keywords),
            self._cryptopanic(keywords, market),
            self._reddit(keywords),
            self._twitter(keywords),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        articles: list[dict] = []
        for r in results:
            if isinstance(r, Exception):
                logger.debug("News source error: %s", r)
            elif r:
                articles.extend(r)

        # Deduplicate by title similarity
        articles = self._deduplicate(articles)

        # Score and sort by relevance
        for art in articles:
            art["relevance_score"] = self._relevance(art, keywords)
        articles.sort(key=lambda a: a["relevance_score"], reverse=True)

        logger.info("Gathered %d articles for: %s", len(articles), question[:60])
        return articles[:30]  # cap at 30 for context window

    # ── Source implementations ────────────────────────────────────────────────

    async def _google_news_rss(self, keywords: list[str]) -> list[dict]:
        """Free Google News RSS — no API key required."""
        query = quote_plus(" ".join(keywords[:4]))
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    text = await resp.text()
            feed = feedparser.parse(text)
            articles = []
            for entry in feed.entries[:15]:
                articles.append({
                    "source": "Google News",
                    "title": _clean(entry.get("title", "")),
                    "summary": _excerpt(entry.get("summary", entry.get("title", ""))),
                    "url": entry.get("link", ""),
                    "published_at": entry.get("published", ""),
                })
            return articles
        except Exception as exc:
            logger.debug("Google News RSS error: %s", exc)
            return []

    async def _direct_rss_feeds(self) -> list[dict]:
        """Scrape major news RSS feeds."""
        feeds = [
            ("Reuters", "https://feeds.reuters.com/reuters/topNews"),
            ("AP News", "https://feeds.apnews.com/rss/apf-topnews"),
            ("BBC", "http://feeds.bbci.co.uk/news/rss.xml"),
            ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
            ("NPR", "https://feeds.npr.org/1001/rss.xml"),
        ]
        articles = []
        async with aiohttp.ClientSession() as session:
            for name, url in feeds:
                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=8)
                    ) as resp:
                        text = await resp.text()
                    feed = feedparser.parse(text)
                    for entry in feed.entries[:8]:
                        articles.append({
                            "source": name,
                            "title": _clean(entry.get("title", "")),
                            "summary": _excerpt(entry.get("summary", "")),
                            "url": entry.get("link", ""),
                            "published_at": entry.get("published", ""),
                        })
                except Exception:
                    pass
        return articles

    async def _newsapi(self, keywords: list[str]) -> list[dict]:
        """NewsAPI.org — 100 req/day on free tier."""
        if not settings.NEWS_API_KEY:
            return []
        query = " OR ".join(keywords[:5])
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": query,
            "sortBy": "publishedAt",
            "pageSize": 20,
            "language": "en",
            "apiKey": settings.NEWS_API_KEY,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
            articles = []
            for art in data.get("articles", []):
                articles.append({
                    "source": art.get("source", {}).get("name", "NewsAPI"),
                    "title": _clean(art.get("title", "")),
                    "summary": _excerpt(art.get("description", "") or art.get("content", "")),
                    "url": art.get("url", ""),
                    "published_at": art.get("publishedAt", ""),
                })
            return articles
        except Exception as exc:
            logger.debug("NewsAPI error: %s", exc)
            return []

    async def _cryptopanic(self, keywords: list[str], market: dict) -> list[dict]:
        """CryptoPanic — only useful for crypto/finance markets."""
        if not settings.CRYPTOPANIC_API_KEY:
            return []
        category = market.get("category", "").lower()
        if not any(kw in category for kw in ["crypto", "bitcoin", "eth", "finance", "defi"]):
            return []

        query = " ".join(keywords[:3])
        url = "https://cryptopanic.com/api/v1/posts/"
        params = {
            "auth_token": settings.CRYPTOPANIC_API_KEY,
            "kind": "news",
            "filter": "hot",
            "public": "true",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
            articles = []
            for item in data.get("results", [])[:15]:
                articles.append({
                    "source": "CryptoPanic",
                    "title": _clean(item.get("title", "")),
                    "summary": _clean(item.get("title", "")),
                    "url": item.get("url", ""),
                    "published_at": item.get("published_at", ""),
                })
            return articles
        except Exception as exc:
            logger.debug("CryptoPanic error: %s", exc)
            return []

    async def _reddit(self, keywords: list[str]) -> list[dict]:
        """Reddit via public JSON API — free, no key needed."""
        subreddits = [
            "PredictionMarket", "worldnews", "news", "politics",
            "geopolitics", "investing", "CryptoCurrency",
        ]
        query = "+".join(keywords[:3])
        articles = []
        headers = {"User-Agent": "PolyMarketBot/1.0"}

        async with aiohttp.ClientSession(headers=headers) as session:
            for sub in subreddits[:4]:
                url = f"https://www.reddit.com/r/{sub}/search.json"
                params = {"q": query, "sort": "new", "limit": 10, "t": "week"}
                try:
                    async with session.get(
                        url, params=params, timeout=aiohttp.ClientTimeout(total=8)
                    ) as resp:
                        data = await resp.json()
                    for post in data.get("data", {}).get("children", []):
                        p = post.get("data", {})
                        articles.append({
                            "source": f"Reddit r/{sub}",
                            "title": _clean(p.get("title", "")),
                            "summary": _excerpt(p.get("selftext", p.get("title", ""))),
                            "url": "https://reddit.com" + p.get("permalink", ""),
                            "published_at": str(p.get("created_utc", "")),
                        })
                except Exception:
                    pass
                await asyncio.sleep(0.3)
        return articles

    async def _twitter(self, keywords: list[str]) -> list[dict]:
        """Twitter/X v2 API — requires bearer token."""
        if not settings.TWITTER_BEARER_TOKEN:
            return []
        try:
            import tweepy  # noqa: PLC0415

            client = tweepy.Client(
                bearer_token=settings.TWITTER_BEARER_TOKEN,
                wait_on_rate_limit=False,
            )
            query = " OR ".join(f'"{kw}"' for kw in keywords[:3])
            query += " -is:retweet lang:en"

            # Run blocking Tweepy call in a thread pool
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.search_recent_tweets(
                    query=query,
                    max_results=20,
                    tweet_fields=["text", "created_at", "public_metrics"],
                ),
            )
            articles = []
            if response and response.data:
                for tweet in response.data:
                    articles.append({
                        "source": "Twitter/X",
                        "title": tweet.text[:120],
                        "summary": _clean(tweet.text),
                        "url": f"https://twitter.com/i/web/status/{tweet.id}",
                        "published_at": str(tweet.created_at or ""),
                    })
            return articles
        except Exception as exc:
            logger.debug("Twitter error: %s", exc)
            return []

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_keywords(question: str) -> list[str]:
        """Extract meaningful search keywords from a market question."""
        # Remove common prediction-market boilerplate
        stop_words = {
            "will", "the", "a", "an", "of", "in", "on", "at", "to", "for",
            "is", "be", "by", "with", "that", "this", "and", "or", "but",
            "win", "lose", "happen", "occur", "before", "after", "during",
            "which", "who", "what", "when", "where", "how", "any", "yes", "no",
        }
        words = re.findall(r"\b[A-Za-z][A-Za-z0-9]{2,}\b", question)
        keywords = [w for w in words if w.lower() not in stop_words]

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique = []
        for kw in keywords:
            low = kw.lower()
            if low not in seen:
                seen.add(low)
                unique.append(kw)

        return unique[:8]

    @staticmethod
    def _deduplicate(articles: list[dict]) -> list[dict]:
        seen_titles: set[str] = set()
        unique = []
        for art in articles:
            key = art["title"].lower()[:60]
            if key and key not in seen_titles:
                seen_titles.add(key)
                unique.append(art)
        return unique

    @staticmethod
    def _relevance(article: dict, keywords: list[str]) -> float:
        """Simple keyword-overlap relevance score (0-1)."""
        text = (article.get("title", "") + " " + article.get("summary", "")).lower()
        matched = sum(1 for kw in keywords if kw.lower() in text)
        return matched / max(len(keywords), 1)
