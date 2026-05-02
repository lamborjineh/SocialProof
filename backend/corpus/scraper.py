"""
corpus/scraper.py
Scrapes articles from curated credible sources for the SocialProof corpus.

Scope: Philippine news, PH government agencies, international statistical
and policy authorities, global health/education bodies, and fact-checkers.

After scraping, always run:
  python retrieval/build_index.py --rebuild
  python scripts/diagnose.py
"""

import requests
import time
import re
import argparse
from datetime import datetime
from bs4 import BeautifulSoup
from pathlib import Path
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout


def _run_in_thread(fn, *args, timeout=60, **kwargs):
    """
    Run fn(*args, **kwargs) in a fresh thread that has no asyncio event loop.
    Playwright sync API crashes when called from a thread that has a running
    asyncio loop (common on Windows Python 3.10+). Running in a new thread
    bypasses this — the new thread starts with no event loop of its own.
    Returns the result or None on timeout/error.
    """
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            return None
        except Exception as e:
            print(f"  [Thread] Error: {e}")
            return None

sys.path.insert(0, str(Path(__file__).parent.parent))

from corpus.db import insert_article, insert_pipeline_sentences, init_db
from corpus.source_registry import (
    SOURCES, SOURCE_GROUPS, PLAYWRIGHT_DOMAINS, DOMAIN_DELAYS, DEFAULT_DELAY,
    STATS_DOMAINS, get_delay, get_pipeline, get_sentence_cap, get_playwright_timeout,
)
from corpus.simhash import SimHashStore
from corpus.stat_extractor import extract_stats, insert_stats

# ── Request headers ───────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

RSS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

MAX_RETRIES = 2

# ── Content patterns for government / org pages ───────────────────────────────
_GOV_ORG_CONTENT_PATTERNS = re.compile(
    r"press.?release|field.?body|node.?content|region.?content|page.?content|"
    r"entry.?content|post.?content|content.?area|main.?content|body.?content|"
    r"article.?body|article.?content|story.?body|news.?content|"
    r"article.?text|post.?body|content.?body",
    re.I,
)

# ── Boilerplate phrases ───────────────────────────────────────────────────────
_BOILERPLATE = [
    "Subscribe to our newsletter", "ADVERTISEMENT", "Read more:",
    "Also read:", "RELATED STORIES", "MORE FROM", "Click here to",
    "Follow us on", "Sign up for", "Get the latest", "Share this article",
    "Comments are closed", "Leave a Reply", "Your email address",
    "Press Release", "For media inquiries", "For more information contact",
    "Download PDF", "Download full report", "View full text",
    "Note to editors", "About the", "Media Contact",
    "This press release", "For immediate release",
    "Subscribe to Rappler", "READ:", "WATCH:", "LOOK:",
    "IN PHOTOS:", "DEVELOPING STORY", "JUST IN:",
    "Learn more at", "Visit our website", "Contact us",
    "All rights reserved",
]

# ── Numeric density helper ────────────────────────────────────────────────────
_HAS_DIGIT = re.compile(r'\d')


def _numeric_density(sentences: list) -> float:
    """Fraction of sentences that contain at least one digit."""
    if not sentences:
        return 0.0
    return sum(1 for s in sentences if _HAS_DIGIT.search(s)) / len(sentences)


# ── Text utilities ────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text)
    for phrase in _BOILERPLATE:
        text = text.replace(phrase, "")
    return text.strip()


def split_sentences(text: str) -> list:
    """
    Split text into sentences using spaCy (same model used by the NLP pipeline).
    Falls back to regex split if spaCy is unavailable.
    Filters: too short (<20 chars) or too long (>500 chars), min 4 words.

    spaCy handles edge cases the regex misses: abbreviations (U.S., Dr., P1.2B),
    quoted speech, Filipino sentence structures, and mid-sentence punctuation
    common in fact-check articles.
    """
    try:
        from core.model_registry import ModelRegistry
        doc = ModelRegistry.nlp()(text)
        raw = [sent.text.strip() for sent in doc.sents]
    except Exception:
        # Fallback: original regex split
        raw = re.split(r'(?<=[.!?])\s+(?=[A-Z0-9"\'])', text)

    return [
        s for s in raw
        if 20 <= len(s.strip()) <= 500
        and len(s.split()) >= 4
    ]


# ── Playwright article fetcher ────────────────────────────────────────────────

def _fetch_raw_html_playwright(url: str, timeout_ms: int = 30_000,
                               wait_ms: int = 2000) -> str | None:
    """
    Fetch raw HTML from a URL using Playwright with no content filtering.
    Runs in a fresh thread to avoid asyncio event loop conflicts on Windows.
    """
    return _run_in_thread(_fetch_raw_html_playwright_sync, url, timeout_ms, wait_ms,
                          timeout=timeout_ms // 1000 + 10)


def _fetch_raw_html_playwright_sync(url: str, timeout_ms: int = 30_000,
                                    wait_ms: int = 2000) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            page.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,css}",
                lambda route: route.abort(),
            )
            # Use networkidle for index pages: JS-heavy frameworks (Drupal, React,
            # Angular) render their links client-side. domcontentloaded fires before
            # JS has executed, leaving an empty HTML shell with zero <a> tags.
            # networkidle waits until all XHR/fetch calls settle — the point at
            # which the framework has finished mounting its component tree.
            page.goto(url, timeout=timeout_ms, wait_until="networkidle")

            # Belt-and-suspenders: wait up to 10s for at least one link to appear.
            # Catches frameworks that finish networkidle before rendering links.
            try:
                page.wait_for_selector("a[href]", timeout=10_000)
            except Exception:
                pass  # No links appeared — we'll still return whatever HTML we have
            html = page.content()
            html_kb = len(html) // 1024
            print(f"    [Deep] HTML fetched: {html_kb} KB, url={url[-55:]}")
            browser.close()
        return html if html and len(html) > 500 else None
    except Exception as e:
        msg = str(e)
        # "Target page, context or browser has been closed" — thread timed out
        # and the executor shut down the browser. Return None gracefully.
        if "has been closed" in msg or "Target page" in msg:
            print(f"  [Deep] Browser closed by thread timeout — skipping {url[:70]}")
        else:
            print(f"  [Deep] Playwright raw fetch failed {url[:70]}: {e}")
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        return None


def scrape_article_playwright(url: str, timeout_ms: int = 30_000,
                              wait_ms: int = 2000) -> dict | None:
    """
    Fetch and parse an article using headless Chromium (Playwright).
    Runs in a fresh thread to avoid asyncio event loop conflicts on Windows.
    """
    return _run_in_thread(_scrape_article_playwright_sync, url, timeout_ms, wait_ms,
                          timeout=timeout_ms // 1000 + 15)


def _scrape_article_playwright_sync(url: str, timeout_ms: int = 30_000,
                                    wait_ms: int = 2000) -> dict | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [Playwright] Not installed. Run: playwright install chromium")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            page.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,css}",
                lambda route: route.abort(),
            )
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(wait_ms)
            try:
                html = page.content()
            except Exception:
                # Google News JS-redirect pages may still be navigating after
                # domcontentloaded. Wait for networkidle and retry once.
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                    html = page.content()
                except Exception as nav_err:
                    browser.close()
                    raise nav_err
            final_url = page.url   # capture where we actually landed
            browser.close()

        # If the redirect never completed we're still on google.com — skip.
        # This is the root cause of 0-char and 140-char thin content on gnews URLs.
        if "google.com" in final_url and "google.com" in url:
            print(f"  [Playwright] Stuck on Google interstitial — skipping")
            return None

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["script", "style", "nav", "footer",
                                   "aside", "form", "iframe", "header", "noscript"]):
            tag.decompose()

        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = clean_text(og["content"])
        elif soup.find("h1"):
            title = clean_text(soup.find("h1").get_text())
        elif soup.find("title"):
            title = clean_text(soup.find("title").get_text())
        else:
            title = ""

        date_published = datetime.now().strftime("%Y-%m-%d")
        for attr in ["article:published_time", "datePublished", "pubdate", "date"]:
            m = (soup.find("meta", property=attr) or
                 soup.find("meta", attrs={"name": attr}) or
                 soup.find("meta", attrs={"itemprop": attr}))
            if m and m.get("content"):
                date_published = m["content"][:10]
                break
        if date_published == datetime.now().strftime("%Y-%m-%d"):
            tt = soup.find("time")
            if tt and tt.get("datetime"):
                date_published = tt["datetime"][:10]

        container = soup.find("article") or soup.find("main")
        paragraphs = container.find_all("p") if container else soup.find_all("p")
        content = " ".join(
            clean_text(p.get_text())
            for p in paragraphs
            if len(p.get_text().strip()) > 40
        )
        # Fallback: if <p> extraction thin, try common article div containers
        if len(content) < 50:
            for cls_hint in ["article-body", "article-content", "post-content",
                              "entry-content", "story-body", "field-body",
                              "node-content", "content-body", "region-content"]:
                div = soup.find(attrs={"class": re.compile(cls_hint, re.I)})
                if div:
                    div_text = " ".join(
                        clean_text(t)
                        for t in div.stripped_strings
                        if len(t.strip()) > 40
                    )
                    if len(div_text) > len(content):
                        content = div_text
                        break
        if len(content) < 50:
            print(f"  [Playwright] Thin content ({len(content)} chars): {url[:70]}")
            return None

        return {"title": title, "content": content,
                "date_published": date_published, "_html": html}

    except Exception as e:
        print(f"  [Playwright] Failed {url[:70]}: {e}")
        return None


# ── Article scraping ──────────────────────────────────────────────────────────

def scrape_article(url: str, domain: str = "") -> dict | None:
    """
    Fetch and parse an article page.
    Returns {title, content, date_published, _html} or None on failure.
    _html is the raw response HTML, used by stat_extractor for table parsing.
    """
    headers = dict(HEADERS)
    if domain:
        headers["Referer"] = f"https://www.{domain}/"

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=20)

            if resp.status_code == 403:
                print(f"  [Article] 403 blocked: {url[:70]} — skipping")
                return None

            resp.raise_for_status()

            try:
                html = resp.content.decode("utf-8", errors="replace")
            except Exception:
                html = resp.content.decode("latin-1", errors="replace")

            soup = BeautifulSoup(html, "html.parser")

            for tag in soup.find_all(["script", "style", "nav", "footer",
                                      "aside", "form", "iframe", "header",
                                      "noscript"]):
                tag.decompose()

            og = soup.find("meta", property="og:title")
            if og and og.get("content"):
                title = clean_text(og["content"])
            elif soup.find("h1"):
                title = clean_text(soup.find("h1").get_text())
            elif soup.find("title"):
                title = clean_text(soup.find("title").get_text())
            else:
                title = ""

            date_published = None
            for attr in ["article:published_time", "datePublished",
                         "pubdate", "date", "DC.date"]:
                m = (soup.find("meta", property=attr) or
                     soup.find("meta", attrs={"name": attr}) or
                     soup.find("meta", attrs={"itemprop": attr}))
                if m and m.get("content"):
                    date_published = m["content"][:10]
                    break
            if not date_published:
                tt = soup.find("time")
                if tt and tt.get("datetime"):
                    date_published = tt["datetime"][:10]
            if not date_published:
                date_published = datetime.now().strftime("%Y-%m-%d")

            container = (
                soup.find("article") or
                soup.find("div", class_=_GOV_ORG_CONTENT_PATTERNS) or
                soup.find("div", attrs={"id": re.compile(
                    r"article|content|story|main|body", re.I)}) or
                soup.find("main") or
                soup.find("div", class_=re.compile(r"container|wrapper", re.I))
            )
            paragraphs = container.find_all("p") if container else soup.find_all("p")
            content = " ".join(
                clean_text(p.get_text())
                for p in paragraphs
                if len(p.get_text().strip()) > 40
            )

            if len(content) < 150:
                return None

            return {"title": title, "content": content,
                    "date_published": date_published, "_html": html}

        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(get_delay(domain))
            else:
                print(f"  [Article] Timeout: {url[:70]}")
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(get_delay(domain))
            else:
                print(f"  [Article] Failed {url[:70]}: {e}")
    return None


# ── Google News URL resolver ──────────────────────────────────────────────────
# Note: persistent browser removed — Playwright sync API cannot be shared across
# threads and crashes inside asyncio loops. Each resolution now runs in its own
# fresh thread via _run_in_thread(), which starts with no asyncio event loop.

def _resolve_gnews_url_playwright_sync(url: str, timeout: int = 10) -> str:
    """Playwright gnews resolver — runs inside a fresh thread (no asyncio loop)."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx  = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = ctx.new_page()
            try:
                page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                time.sleep(1.5)
                final = page.url
            finally:
                page.close()
                ctx.close()
                browser.close()
            if final and "news.google.com" not in final and "google.com" not in final:
                return final
    except Exception:
        pass
    return url


def _resolve_gnews_url(url: str, timeout: int = 10) -> str | None:
    """
    Resolve a Google News RSS redirect URL to the real article URL.

    Returns the resolved URL string, or None if resolution failed so the
    caller can skip the article rather than trying to scrape google.com.

    Step 1: requests HEAD+GET (fast, ~0.2s).
    Step 2: Playwright JS redirect fallback — capped at 8s to prevent
            the 45s hangs seen on google.com/sorry interstitial pages.
            Skipped entirely for Cloudflare-protected gov sites (via
            _skip_playwright heuristic) where Playwright never succeeds.
    """
    if "news.google.com" not in url:
        return url

    # Step 1: requests (fast path)
    try:
        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        url = url.replace("news.google.com/rss/articles/", "news.google.com/articles/")
        resp = requests.head(url, headers=hdrs, allow_redirects=True, timeout=timeout)
        final = resp.url
        if "news.google.com" in final or "google.com/sorry" in final:
            resp2 = requests.get(url, headers=hdrs, allow_redirects=True, timeout=timeout)
            final = resp2.url
        if final and "news.google.com" not in final and "google.com" not in final:
            return final
    except Exception:
        pass

    # Step 2: Playwright JS redirect — hard cap at 8s, skip for Cloudflare-blocked
    # gov domains. google.com/sorry interstitials never resolve regardless of wait
    # time; 8s is sufficient for real JS redirects while stopping runaway hangs.
    _skip_playwright = any(
        kw in url for kw in ["psa.gov", "doh.gov", "dti.gov", "dole.gov",
                              "ched.gov", "comelec.gov"]
    )
    if not _skip_playwright:
        result = _run_in_thread(_resolve_gnews_url_playwright_sync, url, 8,
                                timeout=12)  # 8s page timeout + 4s thread overhead
        if result and result != url and "google.com" not in result:
            return result

    # Could not resolve — return None so caller skips instead of scraping google.com
    print(f"  [gnews] Could not resolve redirect — skipping: {url[:70]}")
    return None


# ── RSS fetching ──────────────────────────────────────────────────────────────

def fetch_rss_links(rss_url: str) -> list:
    """
    Fetch article URLs from an RSS/Atom feed.
    Skips immediately on 403 — retrying a blocked request makes it worse.
    """
    warnings.filterwarnings("ignore")
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(rss_url, headers=RSS_HEADERS, timeout=15)

            if resp.status_code == 403:
                print(f"  [RSS] 403 blocked: {rss_url[:70]} — skipping")
                return []

            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"  [RSS] 429 rate limited: {rss_url[:70]} — waiting {wait}s")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            content = resp.content

            soup = None
            for parser in ["xml", "lxml", "html.parser"]:
                try:
                    soup = BeautifulSoup(content, parser)
                    if soup:
                        break
                except Exception:
                    continue
            if not soup:
                return []

            links = []
            seen = set()

            def add(url):
                url = url.strip()
                if url and url.startswith("http") and url not in seen:
                    seen.add(url)
                    links.append(url)

            for item in soup.find_all("item"):
                link_tag = item.find("link")
                if link_tag:
                    txt = link_tag.get_text(strip=True)
                    if txt:
                        add(txt); continue
                    href = link_tag.get("href", "")
                    if href:
                        add(href); continue
                    sib = link_tag.next_sibling
                    if sib and str(sib).strip().startswith("http"):
                        add(str(sib).strip()); continue
                guid = item.find("guid")
                if guid:
                    txt = guid.get_text(strip=True)
                    if txt.startswith("http"):
                        add(txt)

            for entry in soup.find_all("entry"):
                link_tag = entry.find("link")
                if link_tag:
                    url = link_tag.get("href", "") or link_tag.get_text(strip=True)
                    add(url)

            return links

        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
            else:
                print(f"  [RSS] Timeout: {rss_url[:70]}")
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
            else:
                print(f"  [RSS] Failed {rss_url[:70]}: {e}")
    return []


# ── Source scraper ────────────────────────────────────────────────────────────

def _get_domain_sentence_count(domain: str) -> int:
    """Return how many sentences are already stored for this domain in the DB."""
    try:
        from corpus.db import get_connection
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM sentences WHERE source_domain=?", (domain,))
        count = c.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def _fetch_sitemap_urls(domain: str, section_paths: list,
                        limit: int = 200) -> list[str]:
    """
    Layer 1 — Sitemap crawler.

    Fetches sitemap.xml (and sitemap index files) from a domain, then filters
    URLs to only those containing one of the registered section_paths.
    This prevents crawling thousands of irrelevant project/archive pages.

    Returns up to `limit` article URLs sorted by path length descending
    (deeper paths = more specific articles, less likely to be index pages).

    Falls back to [] on any failure so the caller can try other layers.
    """
    sitemap_candidates = [
        f"https://www.{domain}/sitemap.xml",
        f"https://{domain}/sitemap.xml",
        f"https://www.{domain}/sitemap_index.xml",
        f"https://{domain}/sitemap_index.xml",
        f"https://www.{domain}/news-sitemap.xml",
    ]

    raw_urls: list[str] = []

    for sitemap_url in sitemap_candidates:
        try:
            resp = requests.get(sitemap_url, headers=HEADERS, timeout=15,
                                allow_redirects=True)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.content, "xml")

            # Sitemap index — contains <sitemap><loc> pointing to child sitemaps
            child_sitemaps = [loc.get_text(strip=True)
                              for loc in soup.find_all("sitemap")]
            if child_sitemaps:
                # Filter child sitemaps by section_paths before fetching
                relevant = [
                    u for u in child_sitemaps
                    if not section_paths or any(p in u for p in section_paths)
                ] or child_sitemaps[:5]  # fallback: first 5 if none match

                for child_url in relevant[:8]:  # max 8 child sitemaps
                    try:
                        cr = requests.get(child_url, headers=HEADERS, timeout=12)
                        if cr.status_code == 200:
                            cs = BeautifulSoup(cr.content, "xml")
                            raw_urls += [
                                loc.get_text(strip=True)
                                for loc in cs.find_all("loc")
                            ]
                        time.sleep(0.5)
                    except Exception:
                        continue
                break  # found and processed a sitemap index

            # Regular sitemap — contains <url><loc>
            locs = [loc.get_text(strip=True) for loc in soup.find_all("loc")]
            if locs:
                raw_urls += locs
                break

        except Exception:
            continue

    if not raw_urls:
        return []

    # Filter by section_paths
    if section_paths:
        filtered = [u for u in raw_urls
                    if any(p in u for p in section_paths)]
    else:
        filtered = raw_urls

    # Remove non-article URLs (pagination, category pages, media)
    _skip_patterns = re.compile(
        r"/page/\d+|/category/|/tag/|/author/|/feed/|"
        r"\.(pdf|doc|xls|xlsx|jpg|png|zip)$",
        re.I,
    )
    filtered = [u for u in filtered if not _skip_patterns.search(u)]

    # Sort: longer paths = more specific articles
    filtered.sort(key=len, reverse=True)

    print(f"  [Sitemap] {domain}: {len(raw_urls)} total → "
          f"{len(filtered)} after section_path filter")
    return filtered[:limit]


def _fetch_deep_links(domain: str, section_paths: list,
                      limit: int = 100) -> list[str]:
    """
    Layer 2 — Deep section-path crawler.

    For each section_path, fetches the index page directly and extracts
    all internal <a href> links that point deeper into the same section.
    Uses Playwright for JS-heavy domains (PH gov agencies).

    This is the fallback when sitemap is unavailable or returns nothing.
    Also used as the primary method for crawl_mode='deep' sources.

    Returns deduplicated article URLs, filtered to the source domain.
    """
    from urllib.parse import urljoin, urlparse

    timeout_ms = get_playwright_timeout(domain)
    # Slow PH gov sites need extra JS render time
    wait_ms    = 3000 if domain.endswith(".gov.ph") else 2000

    collected: list[str] = []
    seen: set[str]       = set()

    for section in section_paths:
        index_url = f"https://{domain}{section}"
        print(f"  [Deep] Fetching index: {index_url}")

        # Fetch the section index page — use raw HTML fetcher, NOT the article
        # scraper. Index/listing pages have thin paragraph content and would
        # always fail scrape_article_playwright()'s 150-char content filter.
        if domain in PLAYWRIGHT_DOMAINS:
            html_content = _fetch_raw_html_playwright(index_url,
                                                      timeout_ms=timeout_ms,
                                                      wait_ms=wait_ms)
        else:
            try:
                resp = requests.get(index_url, headers=HEADERS, timeout=20,
                                    allow_redirects=True)
                html_content = resp.text if resp.status_code == 200 else None
            except Exception:
                html_content = None

        if not html_content:
            print(f"  [Deep] Could not fetch: {index_url}")
            continue

        soup = BeautifulSoup(html_content, "html.parser")
        base = f"https://{domain}"

        # ── Diagnostic: inspect raw link inventory before filtering ───────────
        all_a_tags = soup.find_all("a")
        a_with_href = soup.find_all("a", href=True)
        sample_hrefs = [a.get("href", "")[:80] for a in a_with_href[:6]]
        print(f"    [Deep] Raw HTML: {len(html_content)//1024}KB, "
              f"<a> tags: {len(all_a_tags)}, with href: {len(a_with_href)}")
        if sample_hrefs:
            for h in sample_hrefs:
                print(f"      href: {h}")
        else:
            # No <a href> at all — show a snippet so we can see what's in the page
            body_snippet = soup.get_text()[:300].replace("\n", " ").strip()
            print(f"      No <a href> found. Page text snippet: {body_snippet[:200]}")
        # ─────────────────────────────────────────────────────────────────────

        # ── Cloudflare challenge detection ───────────────────────────────────────
        # If ALL hrefs point to cloudflare.com, the page is a Cloudflare challenge.
        # No amount of waiting will bypass this — abort deep crawl immediately.
        all_hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
        cf_hrefs  = [h for h in all_hrefs if "cloudflare.com" in h]
        if all_hrefs and len(cf_hrefs) == len(all_hrefs):
            print(f"  [Deep] Cloudflare challenge detected on {domain} — aborting deep crawl, will use gnews fallback")
            return []   # triggers gnews fallback in scrape_source()
        # ─────────────────────────────────────────────────────────────────────

        # Navigation/utility paths to exclude in fallback mode
        _nav_skip = re.compile(
            r"/(login|logout|register|search|help|about|contact|"
            r"privacy|terms|sitemap|tag|category|author|feed|page/\d+|"
            r"wp-content|wp-admin|#)(/|$)",
            re.I,
        )

        # Collect all candidate links from this index page
        all_internal: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "mailto:", "tel:")):
                continue
            full = urljoin(base, href)
            parsed = urlparse(full)
            if domain not in parsed.netloc:
                continue
            path = parsed.path.rstrip("/")
            if not path or path.count("/") < 2:   # too shallow = nav link
                continue
            if _nav_skip.search(path + "/"):
                continue
            clean = f"{parsed.scheme}://{parsed.netloc}{path}"
            all_internal.append(clean)

        # Strict pass: only links under the registered section_paths
        strict: list[str] = [
            u for u in all_internal
            if section_paths and any(
                urlparse(u).path.startswith(p) for p in section_paths
            )
        ]

        print(f"    [Deep] {index_url[-60:]} — "
              f"{len(all_internal)} internal links, "
              f"{len(strict)} match section_paths")

        # Fallback: if strict filter found nothing, use all internal links.
        # Many CMS systems (Drupal, WordPress) store articles at /content/slug
        # or /node/N rather than under the section path registered in the registry.
        candidates = strict if strict else all_internal

        for url_str in candidates:
            if url_str not in seen:
                seen.add(url_str)
                collected.append(url_str)

        time.sleep(1.0)  # polite between section index fetches

    print(f"  [Deep] {domain}: {len(collected)} article links found")
    return collected[:limit]


def _gnews_domain_filter(resolved_url: str, expected_domain: str) -> bool:
    """
    Layer 3 — gnews domain filter.

    After resolving a Google News redirect, check that the destination URL
    actually belongs to the expected source domain.

    Returns True (accept) if the resolved URL is from the expected domain.
    Returns False (reject) if it resolved to a third-party site.

    Example: PSA gnews query resolves to rappler.com → rejected.
    This prevents polluting the PSA stats pipeline with Rappler news articles.
    """
    from urllib.parse import urlparse
    resolved_domain = urlparse(resolved_url).netloc.lower().replace("www.", "")
    # Allow subdomains (e.g. data.worldbank.org for worldbank.org)
    return (resolved_domain == expected_domain or
            resolved_domain.endswith(f".{expected_domain}"))


def scrape_source(source_key: str, limit: int = 100) -> int:
    """
    Scrape one source. Returns number of new articles added.

    v4.0 — crawl_mode aware:
      rss    → existing RSS fetch (unchanged)
      gnews  → RSS fetch + domain filter (rejects cross-domain resolves)
      sitemap→ sitemap crawler (Layer 1) → gnews fallback
      deep   → section-path crawler (Layer 2) → gnews fallback
    """
    if source_key not in SOURCES:
        print(f"Unknown source '{source_key}'. Available: {list(SOURCES.keys())}")
        return 0

    source       = SOURCES[source_key]
    domain       = source["domain"]
    delay        = get_delay(domain)
    pipeline     = get_pipeline(domain)
    is_stats     = domain in STATS_DOMAINS
    crawl_mode   = source.get("crawl_mode", "rss")
    section_paths = source.get("section_paths", [])
    timeout_ms   = get_playwright_timeout(domain)

    print(f"\n[Scraper] {source_key} ({domain}) "
          f"— pipeline={pipeline} crawl_mode={crawl_mode} delay={delay}s")

    # ── DNS pre-check for .gov.ph domains ─────────────────────────────────────
    # BSP, NEDA, and others fail with ERR_NAME_NOT_RESOLVED from non-PH IPs.
    # A fast socket check avoids wasting Playwright timeouts on unreachable hosts.
    if domain.endswith(".gov.ph"):
        import socket as _socket
        try:
            _socket.getaddrinfo(domain, 443)
        except Exception as dns_err:
            print(f"  [DNS] {domain} unreachable ({dns_err}) — skipping source.")
            print(f"  [DNS] Run scraper from a PH-based IP to reach this source.")
            return 0

    # ── Collect links based on crawl_mode ─────────────────────────────────────
    unique_links: list[str] = []

    if crawl_mode == "sitemap":
        # Layer 1: sitemap crawler with section_path filter
        unique_links = _fetch_sitemap_urls(domain, section_paths, limit=limit)
        if not unique_links:
            print(f"  [Sitemap] No URLs found — falling back to gnews RSS")
            for rss_url in source["rss_urls"]:
                links = fetch_rss_links(rss_url)
                print(f"  Found {len(links)} links from {rss_url[:70]}")
                unique_links.extend(links)
                time.sleep(DEFAULT_DELAY)

    elif crawl_mode == "deep":
        # Layer 2: section-path deep crawler
        unique_links = _fetch_deep_links(domain, section_paths, limit=limit)
        if not unique_links:
            print(f"  [Deep] No links found — falling back to gnews RSS")
            for rss_url in source["rss_urls"]:
                links = fetch_rss_links(rss_url)
                print(f"  Found {len(links)} links from {rss_url[:70]}")
                unique_links.extend(links)
                time.sleep(DEFAULT_DELAY)

    else:
        # rss / gnews: existing RSS fetch
        for rss_url in source["rss_urls"]:
            links = fetch_rss_links(rss_url)
            print(f"  Found {len(links)} links from {rss_url[:70]}")
            unique_links.extend(links)
            time.sleep(DEFAULT_DELAY)

    # Deduplicate
    seen_urls: set = set()
    deduped: list[str] = []
    for lnk in unique_links:
        if lnk not in seen_urls:
            seen_urls.add(lnk)
            deduped.append(lnk)
    unique_links = deduped[:limit]
    print(f"  Unique articles to scrape: {len(unique_links)}")

    if not unique_links:
        print(f"  [Done] {domain}: 0 new articles")
        return 0

    # ── Scrape articles ────────────────────────────────────────────────────────
    success = skipped = 0
    sim_store = SimHashStore(threshold=3)

    for i, url in enumerate(unique_links, 1):
        # Resolve Google News redirects
        resolved_url = _resolve_gnews_url(url)

        # _resolve_gnews_url returns None when the redirect cannot be resolved
        # (google.com/sorry interstitial, timeout, etc.). Skip cleanly rather
        # than trying to scrape google.com as if it were an article.
        if resolved_url is None:
            skipped += 1
            continue

        if resolved_url != url:
            print(f"  [{i}/{len(unique_links)}] Resolved: {resolved_url[:80]}")
        else:
            print(f"  [{i}/{len(unique_links)}] {url[:80]}")

        # Layer 3: gnews domain filter — reject cross-domain resolves
        if crawl_mode == "gnews" and "news.google.com" in url:
            if not _gnews_domain_filter(resolved_url, domain):
                from urllib.parse import urlparse as _up
                got = _up(resolved_url).netloc
                print(f"    ✗ gnews resolved to wrong domain ({got}) — skipping")
                skipped += 1
                continue

        from urllib.parse import urlparse as _urlparse
        resolved_domain = _urlparse(resolved_url).netloc.lower().replace("www.", "")

        # Use per-domain Playwright timeout for JS-heavy sites
        if resolved_domain in PLAYWRIGHT_DOMAINS or domain in PLAYWRIGHT_DOMAINS:
            data = scrape_article_playwright(
                resolved_url,
                timeout_ms=timeout_ms,
                wait_ms=3000 if domain.endswith(".gov.ph") else 2000,
            )
        else:
            data = scrape_article(resolved_url, domain=resolved_domain or domain)

        if not data:
            skipped += 1
            time.sleep(delay)
            continue

        article_id = insert_article(
            source_domain=domain, url=url,
            title=data["title"], content=data["content"],
            date_published=data["date_published"],
        )
        if article_id is None:
            skipped += 1  # URL already exists
        else:
            sentences = split_sentences(data["content"])
            sentences = [s for s in sentences if sim_store.add_if_unique(s)]

            if sentences:
                domain_cap    = get_sentence_cap(domain)
                current_count = _get_domain_sentence_count(domain)
                if current_count >= domain_cap:
                    print(f"    ⚠ Sentence cap reached for {domain} "
                          f"({current_count}/{domain_cap}) — skipping remaining articles.")
                    break
                remaining = domain_cap - current_count
                sentences = sentences[:remaining]

                nd = _numeric_density(sentences)
                insert_pipeline_sentences(
                    article_id, domain, url, sentences,
                    pipeline=pipeline,
                    numeric_density=nd,
                )
                success += 1
                print(f"    ✓ {data['title'][:60]} "
                      f"({len(sentences)} sentences, pipeline={pipeline})")

                if is_stats:
                    stats = extract_stats(data["_html"], sentences, domain, url)
                    if stats:
                        n_inserted = insert_stats(stats)
                        if n_inserted:
                            print(f"    ↪ {n_inserted} structured stats extracted")
            else:
                skipped += 1
                print(f"    ↩ {data['title'][:60]} (all sentences duplicated)")

        time.sleep(delay)

    print(f"  [Done] {domain}: {success} new, {skipped} skipped")
    return success


def scrape_group(group_name: str, limit: int = 100) -> int:
    """Scrape all sources in a named tier group."""
    if group_name not in SOURCE_GROUPS:
        print(f"Unknown group '{group_name}'. Available: {list(SOURCE_GROUPS.keys())}")
        return 0
    keys = SOURCE_GROUPS[group_name]
    print(f"\n[Scraper] Group '{group_name}' — {len(keys)} sources")
    total = sum(scrape_source(k, limit=limit) for k in keys)
    print(f"\n[Scraper] Group '{group_name}' complete. New articles: {total}")
    return total


def scrape_all(limit: int = 100) -> int:
    """Scrape every source in SOURCES."""
    init_db()
    total = sum(scrape_source(k, limit=limit) for k in SOURCES)
    print(f"\n[Scraper] All sources complete. Total new articles: {total}")
    print("[Scraper] Run next: python retrieval/build_index.py --rebuild")
    print("[Scraper] Then:     python scripts/diagnose.py")
    return total


def scrape_balanced(news_limit: int = 60, stats_limit: int = 150,
                    factcheck_limit: int = 150) -> int:
    """
    Scrape with pipeline-aware per-source limits to fix corpus imbalance.

    News sources get a lower cap (60) — already over-represented.
    Stats + factcheck get a higher cap (150) — currently under-represented.
    Target: ~40% news / 35% stats / 25% factcheck in bridge_corpus export.
    """
    init_db()
    total = 0
    for key, cfg in SOURCES.items():
        pipeline = cfg.get("pipeline", "news")
        if pipeline == "stats":
            lim = stats_limit
        elif pipeline == "factcheck":
            lim = factcheck_limit
        else:
            lim = news_limit
        total += scrape_source(key, limit=lim)
    print(f"\n[Scraper] Balanced run complete. Total new articles: {total}")
    print("[Scraper] Run next: python retrieval/build_index.py --rebuild")
    return total


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SocialProof corpus scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python corpus/scraper.py                        # all sources, 100 each
  python corpus/scraper.py --limit 500            # large corpus run
  python corpus/scraper.py --source rappler       # single source
  python corpus/scraper.py --group ph_news        # one tier
  python corpus/scraper.py --group ph_gov --limit 200

Groups: """ + ", ".join(SOURCE_GROUPS.keys()),
    )
    parser.add_argument("--source",   type=str,  help="Scrape a single source by key")
    parser.add_argument("--group",    type=str,  help="Scrape a source tier group")
    parser.add_argument("--limit",    type=int,  default=100,
                        help="Max articles per source (default 100)")
    parser.add_argument("--balanced", action="store_true",
                        help="Pipeline-aware limits: 60 news / 150 stats / 150 factcheck per source")
    parser.add_argument("--reset-cap", type=str, metavar="DOMAIN",
                        help="Delete all sentences for DOMAIN so scraping can refill it. "
                             "Use for testing: --reset-cap psa.gov.ph")
    args = parser.parse_args()

    init_db()

    if args.reset_cap:
        from corpus.db import get_connection
        domain = args.reset_cap
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM sentences WHERE source_domain=?", (domain,))
        n = c.fetchone()[0]
        c.execute("DELETE FROM sentences WHERE source_domain=?", (domain,))
        conn.commit()
        conn.close()
        print(f"[reset-cap] Deleted {n} sentences for {domain}. Re-run scraper to refill.")
    elif args.balanced:
        scrape_balanced()
    elif args.source:
        scrape_source(args.source, limit=args.limit)
    elif args.group:
        scrape_group(args.group, limit=args.limit)
    else:
        scrape_all(limit=args.limit)