"""
SocialProof — URL Fetcher
Fetches and extracts clean text from a URL for analysis.
Called by the orchestrator when input_type == 'url'.

Handles:
  - HTTP/HTTPS pages (news articles, gov sites)
  - Fallback chain: body text → og:title/og:description → meta description → <title>
  - Smarter low-quality text detection (JS-heavy pages, too-short output)
  - Smart truncation at sentence boundary, not hard cut
  - Rejects non-text content types (PDF, images, etc.)
  - Hard-blocks social media URLs with a clear user-facing message
"""
import re
import urllib.request
import urllib.error
from urllib.parse import urlparse
from typing import Optional
from config import logger


# ── Social media domains — blocked (login walls, no useful text) ──────────────
_SOCIAL_DOMAINS = {
    "facebook.com", "fb.com",
    "instagram.com",
    "twitter.com", "x.com",
    "tiktok.com",
    "threads.net",
    "linkedin.com",
}


class URLFetcher:
    TIMEOUT    = 12        # seconds
    MAX_BYTES  = 500_000   # 500KB max
    USER_AGENT = (
        "Mozilla/5.0 (compatible; SocialProof/2.0; "
        "+https://github.com/socialproof)"
    )

    # Tags whose content we keep
    CONTENT_TAGS = re.compile(
        r"<(p|h[1-6]|li|blockquote|td|th|figcaption)[^>]*>(.*?)</\1>",
        re.IGNORECASE | re.DOTALL,
    )
    # Tags to strip entirely with their content
    STRIP_TAGS = re.compile(
        r"<(script|style|nav|footer|header|aside|form|iframe|noscript)"
        r"[^>]*>.*?</\1>",
        re.IGNORECASE | re.DOTALL,
    )
    # Any remaining HTML tags
    ALL_TAGS   = re.compile(r"<[^>]+>")

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _is_social_url(url: str) -> bool:
        """Return True if the URL belongs to a known social platform."""
        try:
            netloc = urlparse(url).netloc.lower().lstrip("www.")
            return any(netloc == d or netloc.endswith("." + d) for d in _SOCIAL_DOMAINS)
        except Exception:
            return False

    @staticmethod
    def _is_low_quality(text: str) -> bool:
        """
        True when extracted body text is unlikely to be useful.
        Catches: too short, too few words, JS-gated pages.
        """
        t = text.lower()
        return (
            len(text) < 100
            or len(text.split()) < 20
            or "enable javascript" in t
            or "please enable js" in t
        )

    @staticmethod
    def _smart_truncate(text: str, max_chars: int = 8000) -> str:
        """
        Truncate at a sentence boundary instead of a hard character cut.
        Falls back to the hard limit only if no usable period is found.
        """
        if len(text) <= max_chars:
            return text
        truncated   = text[:max_chars]
        last_period = truncated.rfind(".")
        if last_period > max_chars * 0.7:   # don't cut too aggressively
            return truncated[:last_period + 1]
        return truncated

    @classmethod
    def _extract_og(cls, raw: str) -> str:
        """Extract OpenGraph title + description (available even behind login walls)."""
        og_title = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
            raw, re.IGNORECASE,
        )
        og_desc = re.search(
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
            raw, re.IGNORECASE,
        )
        parts = [
            og_title.group(1).strip() if og_title else "",
            og_desc.group(1).strip()  if og_desc  else "",
        ]
        return " ".join(p for p in parts if p)

    # ── Main fetch ────────────────────────────────────────────────────────────

    @classmethod
    def fetch(cls, url: str) -> dict:
        """
        Returns:
            {
                "text":    str,      # extracted plain text (may be empty)
                "title":   str,      # page <title> if found
                "url":     str,      # final URL after redirects
                "error":   str|None, # error message if fetch failed
            }
        """
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # Hard block social media — login walls make scraping useless
        if cls._is_social_url(url):
            logger.info(f"[URLFetcher] Social domain blocked: {url}")
            return {
                "text":  "",
                "title": "",
                "url":   url,
                "error": (
                    "Social media URL detected. Platforms like Facebook and Instagram "
                    "restrict content access — please paste the post text directly instead."
                ),
            }

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent":      cls.USER_AGENT,
                "Accept":          "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=cls.TIMEOUT) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if "text" not in content_type and "html" not in content_type:
                    return {
                        "text":  "",
                        "title": "",
                        "url":   url,
                        "error": f"Non-text content type: {content_type}",
                    }
                raw       = resp.read(cls.MAX_BYTES).decode("utf-8", errors="replace")
                final_url = resp.url
        except urllib.error.HTTPError as e:
            return {"text": "", "title": "", "url": url,
                    "error": f"HTTP {e.code}: {e.reason}"}
        except urllib.error.URLError as e:
            return {"text": "", "title": "", "url": url,
                    "error": f"URL error: {e.reason}"}
        except Exception as e:
            return {"text": "", "title": "", "url": url,
                    "error": f"Fetch error: {type(e).__name__}: {e}"}

        # ── Extract metadata (used as fallback chain) ─────────────────────────
        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
        title       = cls.ALL_TAGS.sub("", title_match.group(1)).strip() if title_match else ""

        meta_match = re.search(
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
            raw, re.IGNORECASE,
        )
        meta_desc = meta_match.group(1).strip() if meta_match else ""
        og_text   = cls._extract_og(raw)

        # ── Strip noise and extract body text ─────────────────────────────────
        cleaned = cls.STRIP_TAGS.sub(" ", raw)

        parts = cls.CONTENT_TAGS.findall(cleaned)
        if parts:
            text_parts = [cls.ALL_TAGS.sub("", p[1]).strip() for p in parts]
            text = " ".join(t for t in text_parts if len(t) > 20)
        else:
            body_match = re.search(r"<body[^>]*>(.*?)</body>", cleaned,
                                   re.IGNORECASE | re.DOTALL)
            body = body_match.group(1) if body_match else cleaned
            text = cls.ALL_TAGS.sub(" ", body)

        # ── Normalize whitespace early (before quality checks) ────────────────
        text = re.sub(r"\s+", " ", text).strip()

        # ── HTML entity decode ────────────────────────────────────────────────
        text = (text
                .replace("&amp;",  "&")
                .replace("&lt;",   "<")
                .replace("&gt;",   ">")
                .replace("&quot;", '"')
                .replace("&#39;",  "'")
                .replace("&nbsp;", " "))

        # ── Fallback chain if body text is low quality ────────────────────────
        # Priority: body text → OG tags → meta description → <title>
        if cls._is_low_quality(text):
            fallback = " ".join(filter(None, [og_text, meta_desc, title]))
            if fallback:
                logger.info(
                    f"[URLFetcher] Body text low quality ({len(text)} chars) — "
                    f"using metadata fallback ({len(fallback)} chars)"
                )
                text = fallback

        logger.info(f"[URLFetcher] {final_url} → {len(text)} chars extracted")

        return {
            "text":  cls._smart_truncate(text),
            "title": title,
            "url":   final_url,
            "error": None,
        }
