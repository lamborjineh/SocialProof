"""
retrieval/live_search.py — Live Evidence Retrieval v3.3
Social Proof v3.4

v3.1 Changes:
  - Domain reputation scores applied: results below REPUTATION_THRESHOLD filtered.
  - Numeric/statistical query detection → boosts Tier 1 and Tier 2 sources.
  - Expanded fact-check query patterns.
  - 30–60 min TTL query result cache.
  - Live search results merged and ranked using the hybrid formula.

v3.2 Changes:
  - Removed all local duplicates of shared helpers. The following are now
    imported from retrieval/utils.py:
      recency_boost, trust_normalised, hybrid_score,
      is_numeric_query, split_sentences
  - W_SEMANTIC / W_TRUST / W_RECENCY are defined once in utils.py.
  - The embedding model is passed in from the API (api/main.py passes
    retriever.model), so no second model instance is loaded.

v3.3 Bug Fixes (BUG 8):
  - _parse_html_to_sentences(): expanded container selector chain to include
    div.field-item, div.press-release, section.content and other gov-site
    structures used by PSA, BSP, NEDA, DOH. Deep fallback to all <p> tags
    when container detection yields < 100 chars.
  - _fetch_with_requests(): User-Agent now rotated per request from a pool
    of 4 real browser UAs. .gov.ph domains get verify=False to tolerate
    common SSL cert issues on Philippine government servers.

ROOT BUG FIX (v2.1, kept):
  Google News RSS returns redirect URLs like news.google.com/rss/articles/...
  We extract source_domain from <source url="..."> and use THAT for domain checks.

FALLBACK CHAIN (v2.2, kept):
  1. requests  — fast plain HTTP
  2. Playwright — headless Chromium (bypasses Cloudflare / JS walls)
  3. ScraperAPI — paid rotating proxy (last resort, 1000 free credits/month)
  4. RSS description — always available, shortest content

SETUP:
  pip install playwright scraperapi-sdk
  playwright install chromium
  Set SCRAPER_API_KEY in .env (optional)
"""

import re
import os
import time
import random
import hashlib
import requests
import concurrent.futures
from bs4 import BeautifulSoup
from typing import List, Dict, Set, Optional
from urllib.parse import quote_plus
from pathlib import Path
from datetime import datetime, timedelta
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

SCRAPER_API_KEY: str          = os.getenv("SCRAPER_API_KEY", "")
GOOGLE_FACTCHECK_API_KEY: str = os.getenv("GOOGLE_FACTCHECK_API_KEY", "")

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ── Source registry imports ───────────────────────────────────────────────────
from corpus.source_registry import (
    ALL_DOMAINS, TIER1_DOMAINS, TIER2_DOMAINS, STATS_DOMAINS,
    FACTCHECK_DOMAINS, NEWS_DOMAINS, REPUTATION,
    get_reputation, REPUTATION_THRESHOLD,
)

# ── Shared retrieval utilities ────────────────────────────────────────────────
# These were previously duplicated here. Single source of truth is utils.py.
from retrieval.utils import (
    recency_boost, trust_normalised, hybrid_score,
    is_numeric_query, split_sentences,
)

# ── Domain sets ───────────────────────────────────────────────────────────────
TRUSTED_DOMAINS: Set[str]       = TIER1_DOMAINS | TIER2_DOMAINS | NEWS_DOMAINS
ALL_CREDIBLE_DOMAINS: Set[str]  = ALL_DOMAINS
FACT_CHECK_DOMAINS: Set[str]    = FACTCHECK_DOMAINS | {
    "snopes.com", "factcheck.org", "politifact.com", "fullfact.org",
    "africacheck.org", "reuters.com", "apnews.com",
}

_EXTRA_OPEN_WEB: Set[str] = {
    "bloomberg.com", "ft.com", "wsj.com",
    "sciencedirect.com", "pubmed.ncbi.nlm.nih.gov",
    "factcheck.org", "politifact.com", "fullfact.org", "africacheck.org",
    "nytimes.com", "washingtonpost.com", "time.com",
    "theatlantic.com", "vox.com", "wired.com",
    "en.wikipedia.org",
    "cdc.gov", "nih.gov", "nature.com",
}
ALL_CREDIBLE_DOMAINS = ALL_CREDIBLE_DOMAINS | _EXTRA_OPEN_WEB

# ── Config ────────────────────────────────────────────────────────────────────
LIVE_FETCH_LIMIT    = 20
MIN_TRUSTED_RESULTS = 3
LIVE_TOP_K          = 7
LIVE_THRESHOLD      = 0.35
MIN_SENT_LEN        = 30
MAX_SENT_LEN        = 600
MAX_WORKERS         = 5
try:
    from config import TIMEOUT_LIVE_SEARCH as ARTICLE_TIMEOUT
except ImportError:
    ARTICLE_TIMEOUT = 12  # fallback if config unavailable
PLAYWRIGHT_TIMEOUT  = 15000  # ms

CACHE_TTL_MIN = 30
CACHE_TTL_MAX = 60

# BUG 8 FIX: Rotate User-Agents so gov sites (PSA, BSP, WHO) don't 403 on a
# single static string. The original single UA was flagged as a bot by .gov.ph.
_USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/122.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/121.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) "
     "Gecko/20100101 Firefox/123.0"),
    ("Mozilla/5.0 (X11; Linux x86_64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/120.0.0.0 Safari/537.36"),
]

HEADERS = {
    "User-Agent": _USER_AGENTS[0],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-PH,en;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

RSS_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


# ── In-process query cache ────────────────────────────────────────────────────
_query_cache: dict = {}


def _cache_key(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()[:16]


def _cache_get(query: str) -> Optional[List[Dict]]:
    key = _cache_key(query)
    entry = _query_cache.get(key)
    if entry:
        expiry, results = entry
        if datetime.now() < expiry:
            return results
        del _query_cache[key]
    return None


def _cache_set(query: str, results: List[Dict]) -> None:
    ttl_minutes = random.randint(CACHE_TTL_MIN, CACHE_TTL_MAX)
    expiry = datetime.now() + timedelta(minutes=ttl_minutes)
    _query_cache[_cache_key(query)] = (expiry, results)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\x20-\x7E]', ' ', text)
    return text.strip()


def _get_domain(url: str) -> str:
    m = re.search(r'https?://(?:www\.)?([^/]+)', url)
    return m.group(1) if m else url[:50]


def _parse_html_to_sentences(html: str, domain: str, url: str,
                              source_type: str = "live") -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "nav", "footer",
                               "header", "aside", "form", "iframe"]):
        tag.decompose()

    # BUG 8 FIX: Expanded container selector chain.
    # Standard news: article > div.article-body > main
    # Gov/PSA/BSP: div.field-item, div.press-release, section.content,
    #              div.entry, div.post, div.node-content, table (stat pages)
    container = (
        soup.find("article") or
        soup.find("div", class_=re.compile(
            r"article.?body|article.?content|entry.?content|"
            r"post.?content|story.?body|news.?content|"
            r"article.?text|post.?body|content.?body", re.I)) or
        soup.find("main") or
        soup.find("div", attrs={"id": re.compile(r"article|content|story|main", re.I)}) or
        # Gov site patterns (PSA, BSP, NEDA, DOH use these structures)
        soup.find("div", class_=re.compile(
            r"field.?item|press.?release|node.?content|"
            r"view.?content|region.?content|page.?content|"
            r"entry|post|release|statistic", re.I)) or
        soup.find("section", class_=re.compile(r"content|main|body|article", re.I)) or
        soup.find("div", class_=re.compile(r"panel.?body|tab.?content|accordion", re.I))
    )

    paragraphs = container.find_all("p") if container else soup.find_all("p")

    # BUG 8 FIX: If no container found OR container yields very little text,
    # fall back to ALL <p> tags on the page filtered by minimum length.
    # This catches gov sites that use tables + loose <p> tags without containers.
    if not container or len(" ".join(p.get_text() for p in paragraphs)) < 100:
        paragraphs = soup.find_all("p")

    content = " ".join(
        _clean(p.get_text()) for p in paragraphs
        if len(_clean(p.get_text())) > 40
    )

    if len(content) < 100:
        return []

    pipeline = ("stats"     if domain in STATS_DOMAINS     else
                "factcheck" if domain in FACT_CHECK_DOMAINS else
                "news")

    # Use utils.split_sentences with live_search thresholds
    sents = split_sentences(content, min_len=MIN_SENT_LEN, max_len=MAX_SENT_LEN)
    return [
        {"text": s, "domain": domain, "url": url,
         "source_type": source_type, "pipeline_type": pipeline}
        for s in sents
    ]


# ── Domain trust checks ───────────────────────────────────────────────────────

def _is_trusted_item(item: Dict) -> bool:
    src = item.get("source_domain", "")
    dom = _get_domain(item["url"])
    return (src in TRUSTED_DOMAINS) or (dom in TRUSTED_DOMAINS)


def _is_credible_item(item: Dict) -> bool:
    src = item.get("source_domain", "")
    dom = _get_domain(item["url"])
    return (src in ALL_CREDIBLE_DOMAINS) or (dom in ALL_CREDIBLE_DOMAINS)


def _is_stats_item(item: Dict) -> bool:
    src = item.get("source_domain", "")
    dom = _get_domain(item["url"])
    return (src in STATS_DOMAINS) or (dom in STATS_DOMAINS)


def _query_google_factcheck_api(
    claim_text: str,
    lang_code: str = "en",
    page_size: int = 5,
) -> List[Dict]:
    """
    Query the Google Fact Check Tools API.

    Returns up to `page_size` IFCN-verified fact-check results for the claim.
    This single call covers ALL connected IFCN partners simultaneously:
    Vera Files (PH), AFP Fact Check PH, Rappler, Reuters, AP, Snopes, etc.

    lang_code options:
      "en"  — international English results
      "fil" — Filipino-language results (covers Vera Files / Rappler PH)
      ""    — no language filter (broadest, mixes both)

    Returns [] silently on any failure so the pipeline degrades gracefully.
    Requires GOOGLE_FACTCHECK_API_KEY in .env (free key, 10k req/day).
    """
    if not GOOGLE_FACTCHECK_API_KEY:
        return []

    try:
        params: Dict = {
            "query":        claim_text,
            "key":          GOOGLE_FACTCHECK_API_KEY,
            "pageSize":     page_size,
        }
        if lang_code:
            params["languageCode"] = lang_code

        resp = requests.get(
            "https://factchecktools.googleapis.com/v1alpha1/claims:search",
            params=params,
            timeout=5,
        )
        if resp.status_code != 200:
            return []

        data    = resp.json()
        results = []
        for item in data.get("claims", []):
            review      = (item.get("claimReview") or [{}])[0]
            publisher   = review.get("publisher", {})
            source_url  = review.get("url", "")
            source_name = publisher.get("name", "Google Fact Check")
            domain      = _get_domain(source_url) if source_url else "factchecktools.googleapis.com"
            results.append({
                "text":          item.get("text", ""),
                "url":           source_url,
                "source_domain": domain,
                "source_label":  source_name,
                "rating":        review.get("textualRating", ""),
                "source_type":   "gfct_api",
                # Pre-set reputation to 0.90 — all GFCT entries are IFCN-certified
                "_reputation":   0.90,
            })
        return results

    except Exception as e:
        logger.warning(f"[LiveSearch] Google Fact Check API error: {e}")
        return []


def _is_fact_check_item(item: Dict) -> bool:
    src = item.get("source_domain", "")
    dom = _get_domain(item["url"])
    return (src in FACT_CHECK_DOMAINS) or (dom in FACT_CHECK_DOMAINS)


def _passes_reputation(item: Dict) -> bool:
    src = item.get("source_domain", "") or _get_domain(item["url"])
    return get_reputation(src) >= REPUTATION_THRESHOLD


# ── Query extraction ──────────────────────────────────────────────────────────

def _extract_query_terms(claim: str, max_terms: int = 10) -> str:
    stopwords = {
        "the", "a", "an", "is", "are", "was", "were", "in", "of", "to",
        "for", "that", "this", "and", "or", "but", "on", "at", "by", "from",
        "with", "has", "have", "had", "be", "been", "being", "will", "would",
        "could", "should", "may", "might", "do", "does", "did", "its", "it",
        "which", "who", "what", "when", "where", "how", "said", "says",
        "also", "than", "then", "their", "they", "them", "these", "those",
        "not", "no", "as", "up", "about", "into", "through", "during",
        "only", "just", "very", "so", "if", "can", "all", "more", "some",
    }
    tokens = re.findall(r'[\d]+(?:[.,]\d+)?%?|[A-Za-z]+', claim)
    priority, secondary = [], []
    for tok in tokens:
        lower = tok.lower()
        if lower in stopwords or len(tok) < 3:
            continue
        if re.match(r'^\d', tok) or tok[0].isupper():
            priority.append(tok)
        else:
            secondary.append(tok)
    combined = (priority + secondary)[:max_terms]
    return " ".join(combined) if combined else claim[:80]


# ── Google News URL resolver ──────────────────────────────────────────────────

def _resolve_gnews_url(url: str, timeout: int = 8) -> str:
    """
    Resolve a Google News RSS redirect URL to the real article URL.

    Google News RSS returns URLs like:
      https://news.google.com/rss/articles/CBMi...
    These are redirect pages. _fetch_with_requests() already follows redirects
    via allow_redirects=True, but _fetch_with_playwright() calls page.goto()
    directly on the redirect URL, which causes:
      "Unable to retrieve content because the page is navigating"

    Fix: follow the redirect chain with a plain HEAD request before handing
    the URL to Playwright. _fetch_with_requests() is unchanged (already safe).
    Falls back to the original URL on any error.
    """
    if "news.google.com" not in url:
        return url

    try:
        resp = requests.head(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            allow_redirects=True,
            timeout=timeout,
        )
        final = resp.url
        if "news.google.com" in final or "google.com/sorry" in final:
            resp2 = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                allow_redirects=True,
                timeout=timeout,
            )
            final = resp2.url

        if final and "news.google.com" not in final:
            return final

    except Exception:
        pass

    return url


# ── Fetchers ──────────────────────────────────────────────────────────────────

def _fetch_with_requests(url: str, domain: str) -> Optional[List[Dict]]:
    try:
        import urllib3
        # BUG 8 FIX: Rotate User-Agent per request so gov sites don't fingerprint us.
        # Also use verify=False for .gov.ph sites that have SSL cert issues — suppress
        # the urllib3 InsecureRequestWarning to keep logs clean.
        ua      = random.choice(_USER_AGENTS)
        headers = {**HEADERS, "User-Agent": ua}
        is_gov  = domain.endswith(".gov.ph") or ".gov.ph" in url
        if is_gov:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        time.sleep(random.uniform(0.5, 1.5))
        resp = requests.get(url, headers=headers, timeout=ARTICLE_TIMEOUT,
                            allow_redirects=True, verify=(not is_gov))
        if resp.status_code in (403, 429):
            return None
        resp.raise_for_status()
        final_url = resp.url
        if "consent.google.com" in final_url:
            return None
        try:
            html = resp.content.decode("utf-8", errors="replace")
        except Exception:
            html = resp.content.decode("latin-1", errors="replace")
        sentences = _parse_html_to_sentences(html, domain, final_url, "live")
        return sentences if sentences else None
    except Exception:
        return None


def _fetch_with_playwright(url: str, domain: str) -> Optional[List[Dict]]:
    if not PLAYWRIGHT_AVAILABLE:
        return None
    try:
        # Resolve Google News redirect URL before handing to Playwright.
        # Without this, page.goto() fires on a redirect page and crashes
        # with "Unable to retrieve content because the page is navigating".
        url = _resolve_gnews_url(url)

        print(f"  [LiveSearch] Playwright: trying → {domain}")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-PH")
            page    = context.new_page()
            page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,css}",
                       lambda route: route.abort())
            page.goto(url, timeout=PLAYWRIGHT_TIMEOUT, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            final_url = page.url
            html      = page.content()
            browser.close()
        if "consent.google.com" in final_url:
            return None
        sentences = _parse_html_to_sentences(html, domain, final_url, "live_playwright")
        return sentences if sentences else None
    except Exception as e:
        print(f"  [LiveSearch] Playwright: failed → {domain} ({type(e).__name__})")
        return None


def _fetch_with_scraperapi(url: str, domain: str) -> Optional[List[Dict]]:
    if not SCRAPER_API_KEY:
        return None
    try:
        print(f"  [LiveSearch] ScraperAPI: trying → {domain}")
        api_url = (f"http://api.scraperapi.com?api_key={SCRAPER_API_KEY}"
                   f"&url={quote_plus(url)}&country_code=ph")
        resp = requests.get(api_url, timeout=30)
        if resp.status_code != 200:
            return None
        html = resp.content.decode("utf-8", errors="replace")
        sentences = _parse_html_to_sentences(html, domain, url, "live_scraperapi")
        return sentences if sentences else None
    except Exception:
        return None


def _rss_description_fallback(item: Dict, domain: str) -> List[Dict]:
    description = item.get("description", "")
    if not description or len(description) < MIN_SENT_LEN:
        return []
    sents = split_sentences(description, min_len=MIN_SENT_LEN, max_len=MAX_SENT_LEN)
    if not sents and len(description) >= MIN_SENT_LEN:
        sents = [description[:MAX_SENT_LEN]]
    pipeline = ("stats"     if domain in STATS_DOMAINS     else
                "factcheck" if domain in FACT_CHECK_DOMAINS else
                "news")
    return [
        {"text": s, "domain": domain, "url": item["url"],
         "source_type": "live_rss_desc", "pipeline_type": pipeline}
        for s in sents
    ]


def _fetch_article_sentences(item: Dict) -> List[Dict]:
    url    = item["url"]
    domain = item.get("source_domain", "") or _get_domain(url)

    result = _fetch_with_requests(url, domain)
    if result is not None:
        return result
    result = _fetch_with_playwright(url, domain)
    if result is not None:
        return result
    result = _fetch_with_scraperapi(url, domain)
    if result is not None:
        return result
    return _rss_description_fallback(item, domain)


def _fetch_articles_concurrent(articles: List[Dict]) -> List[Dict]:
    """
    Concurrent article fetch with partial-results logic.
    If a source times out or errors, it is skipped and logged — whatever
    other sources responded is still returned. Live search never fails entirely.
    """
    all_sentences = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_article_sentences, art): art for art in articles}
        for future in concurrent.futures.as_completed(futures):
            art = futures[future]
            try:
                sentences = future.result(timeout=ARTICLE_TIMEOUT + 5)
                all_sentences.extend(sentences)
            except concurrent.futures.TimeoutError:
                print(f"[LiveSearch] Timeout: {art.get('url','?')[:70]} — skipping (partial results)")
            except Exception as e:
                print(f"[LiveSearch] Error: {art.get('url','?')[:70]}: {type(e).__name__} — skipping")
    return all_sentences


# ── Google News RSS ───────────────────────────────────────────────────────────

def _google_news_rss(query: str, limit: int, lang: str = "en-PH",
                     gl: str = "PH") -> List[Dict]:
    encoded = quote_plus(query)
    rss_url = (f"https://news.google.com/rss/search"
               f"?q={encoded}&hl={lang}&gl={gl}&ceid={gl}:{lang[:2]}")
    try:
        resp = requests.get(rss_url, headers=RSS_HEADERS, timeout=10)
        resp.raise_for_status()
        try:
            soup = BeautifulSoup(resp.content, "xml")
        except Exception:
            soup = BeautifulSoup(resp.content, "html.parser")

        results = []
        for item in soup.find_all("item")[:limit]:
            url = ""
            link_tag = item.find("link")
            if link_tag:
                url = link_tag.get_text(strip=True)
                if not url:
                    sib = link_tag.next_sibling
                    if sib and isinstance(sib, str):
                        url = sib.strip()
                if not url:
                    url = link_tag.get("href", "")
            if not url or not url.startswith("http"):
                guid = item.find("guid")
                if guid:
                    url = guid.get_text(strip=True)
            if not url or not url.startswith("http"):
                continue

            title_tag = item.find("title")
            title = title_tag.get_text(strip=True) if title_tag else ""

            source_domain = ""
            source_tag = item.find("source")
            if source_tag:
                source_url = source_tag.get("url", "")
                if source_url:
                    source_domain = _get_domain(source_url)

            description = ""
            desc_tag = item.find("description")
            if desc_tag:
                raw_desc = desc_tag.get_text(strip=True)
                raw_desc = re.sub(r'<[^>]+>', ' ', raw_desc)
                raw_desc = re.sub(r'\s*-\s*\w[\w\s]{0,30}$', '', raw_desc).strip()
                if len(raw_desc) >= MIN_SENT_LEN:
                    description = raw_desc

            results.append({
                "url":           url,
                "title":         title,
                "source_domain": source_domain,
                "description":   description,
            })

        print(f"  [LiveSearch] RSS({gl}): {len(results)} items for '{query[:40]}'")
        return results

    except Exception as e:
        print(f"  [LiveSearch] RSS query failed: {e}")
        return []


# ── Encoding ──────────────────────────────────────────────────────────────────

def _encode_texts(model, texts: list):
    import numpy as np
    try:
        output = model.encode(
            texts,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
            batch_size=64,
        )
        vecs = np.array(output["dense_vecs"], dtype="float32")
    except (TypeError, KeyError):
        vecs = model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            batch_size=64,
            show_progress_bar=False,
        ).astype("float32")
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return vecs / norms


# ── Ranking ───────────────────────────────────────────────────────────────────

def _rank_and_filter(sentences: List[Dict], claim_emb, model,
                     threshold: float, top_k: int,
                     max_per_domain: int = 3,
                     numeric_boost: bool = False) -> List[Dict]:
    import numpy as np
    if not sentences:
        return []

    texts     = [s["text"] for s in sentences]
    sent_embs = _encode_texts(model, texts)
    semantic_sims = np.dot(sent_embs, claim_emb)

    scored = []
    for idx, sem_sim in enumerate(semantic_sims):
        sent   = sentences[idx]
        domain = sent["domain"]

        if get_reputation(domain) < REPUTATION_THRESHOLD:
            continue

        # hybrid_score from utils — includes numeric boost
        final = hybrid_score(float(sem_sim), domain, sent["url"],
                             numeric_boost=numeric_boost)
        scored.append((idx, final))

    scored.sort(key=lambda x: x[1], reverse=True)

    results, seen, domain_counts = [], set(), {}
    for idx, score in scored:
        if score < threshold:
            break
        sent   = sentences[idx]
        domain = sent["domain"]
        short  = sent["text"][:80]
        if short in seen:
            continue
        seen.add(short)
        if domain_counts.get(domain, 0) >= max_per_domain:
            continue
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        results.append({**sent, "similarity": round(score, 4)})
        if len(results) >= top_k:
            break
    return results


# ── Main public function ──────────────────────────────────────────────────────

def live_search(claim: str, model, k: int = LIVE_TOP_K) -> List[Dict]:
    """
    Live evidence retrieval — three pipelines merged with hybrid ranking.
    Results cached 30–60 min per query to reduce repeated scraping.

    The model parameter is the embedding model from Retriever — passed in
    by api/main.py as retriever.model, so no second model instance is loaded.

    Pipeline A — News:      Phase 1 (trusted PH) + Phase 2 (open web)
    Pipeline B — Stats:     extra search targeting Tier 1/2 stats sources
    Pipeline C — Fact-check: targeted fact-check query patterns
    """
    import numpy as np

    query       = _extract_query_terms(claim)
    numeric_q   = is_numeric_query(claim)   # from utils
    cache_key_q = f"{query}|{numeric_q}"

    cached = _cache_get(cache_key_q)
    if cached is not None:
        print(f"  [LiveSearch] Cache hit for '{query[:40]}'")
        return cached

    print(f"  [LiveSearch] Query: '{query}' (numeric={numeric_q})")
    print(f"  [LiveSearch] Playwright: {'available' if PLAYWRIGHT_AVAILABLE else 'NOT installed'}")
    print(f"  [LiveSearch] ScraperAPI: {'configured' if SCRAPER_API_KEY else 'NOT configured (optional)'}")

    claim_emb = _encode_texts(model, [claim])[0]

    # ── Phase 1 / Pipeline A: Trusted Philippine + general news ──────────────
    raw_results      = _google_news_rss(query, limit=LIVE_FETCH_LIMIT)
    trusted_articles = [r for r in raw_results
                        if _is_trusted_item(r) and _passes_reputation(r)]
    print(f"  [LiveSearch] Phase 1 — {len(trusted_articles)} trusted articles")

    trusted_sentences = _fetch_articles_concurrent(trusted_articles) if trusted_articles else []
    trusted_results   = _rank_and_filter(
        trusted_sentences, claim_emb, model,
        threshold=LIVE_THRESHOLD, top_k=k, max_per_domain=3,
        numeric_boost=numeric_q,
    )
    print(f"  [LiveSearch] Phase 1 results: {len(trusted_results)}")

    # ── Phase 2 / Pipeline A cont.: Open web fallback ─────────────────────────
    open_articles, open_results = [], []
    if len(trusted_results) < MIN_TRUSTED_RESULTS:
        raw_ph     = _google_news_rss(query, limit=LIVE_FETCH_LIMIT, lang="en-PH", gl="PH")
        raw_global = _google_news_rss(query, limit=LIVE_FETCH_LIMIT, lang="en-US", gl="US")
        seen_urls  = {a["url"] for a in trusted_articles}
        for r in raw_ph + raw_global:
            if (r["url"] not in seen_urls
                    and _is_credible_item(r)
                    and _passes_reputation(r)):
                open_articles.append(r)
                seen_urls.add(r["url"])
        print(f"  [LiveSearch] Phase 2 — {len(open_articles)} open web articles")
        if open_articles:
            open_sentences = _fetch_articles_concurrent(open_articles[:10])
            open_results   = _rank_and_filter(
                open_sentences, claim_emb, model,
                threshold=LIVE_THRESHOLD,
                top_k=k - len(trusted_results),
                max_per_domain=2,
                numeric_boost=numeric_q,
            )
            for r in open_results:
                r["source_type"] = r.get("source_type", "live_open")
        print(f"  [LiveSearch] Phase 2 results: {len(open_results)}")

    # ── Phase B / Pipeline B: Stats sources (numeric query) ──────────────────
    stats_results = []
    if numeric_q:
        stats_query    = f"{query} statistics data"
        raw_stats_ph   = _google_news_rss(stats_query, limit=15, lang="en-PH", gl="PH")
        raw_stats_us   = _google_news_rss(stats_query, limit=10, lang="en-US", gl="US")
        seen_stats     = {a["url"] for a in trusted_articles} | {a["url"] for a in open_articles}
        stats_articles = [
            r for r in raw_stats_ph + raw_stats_us
            if _is_stats_item(r) and r["url"] not in seen_stats and _passes_reputation(r)
        ]
        if stats_articles:
            stats_sentences = _fetch_articles_concurrent(stats_articles[:8])
            stats_results   = _rank_and_filter(
                stats_sentences, claim_emb, model,
                threshold=LIVE_THRESHOLD * 0.9, top_k=4, max_per_domain=2,
                numeric_boost=True,
            )
            for r in stats_results:
                r["source_type"] = r.get("source_type", "live_stats")
        print(f"  [LiveSearch] Phase B (stats) results: {len(stats_results)}")

    # ── Phase 3 / Pipeline C: Fact-check targeted search ─────────────────────
    fact_results  = []
    seen_urls_all = (
        {a["url"] for a in trusted_articles}
        | {a["url"] for a in open_articles}
    )

    # ── Step 3a: Google Fact Check Tools API (primary, fastest) ──────────────
    # Covers all IFCN-certified partners in one call (Vera Files, AFP PH,
    # Rappler, Reuters, AP, Snopes, etc.). Falls back silently if key missing.
    gfct_en  = _query_google_factcheck_api(claim, lang_code="en",  page_size=5)
    gfct_fil = _query_google_factcheck_api(claim, lang_code="fil", page_size=5)
    gfct_all = []
    for r in gfct_en + gfct_fil:
        if r["url"] and r["url"] not in seen_urls_all and r.get("text"):
            gfct_all.append(r)
            seen_urls_all.add(r["url"])

    if gfct_all:
        # Convert GFCT results directly to the evidence format
        # (no article fetch needed — the claim text IS the evidence)
        for r in gfct_all:
            fact_results.append({
                "text":          r["text"],
                "url":           r["url"],
                "source_domain": r["source_domain"],
                "source_label":  r["source_label"],
                "source_type":   "gfct_api",
                "similarity":    LIVE_THRESHOLD + 0.05,  # above threshold by default
                "trust":         r.get("_reputation", 0.90),
                "rating":        r.get("rating", ""),
            })
        print(f"  [LiveSearch] Phase 3a (GFCT API): {len(fact_results)} results "
              f"(en={len(gfct_en)}, fil={len(gfct_fil)})")
    else:
        if GOOGLE_FACTCHECK_API_KEY:
            print("  [LiveSearch] Phase 3a (GFCT API): 0 results")
        else:
            print("  [LiveSearch] Phase 3a (GFCT API): skipped — "
                  "GOOGLE_FACTCHECK_API_KEY not set in .env")

    # ── Step 3b: Google News RSS fallback (runs regardless of GFCT result) ───
    fc_queries = [
        f"{query} fact check",
        f"{query} debunked",
        f"{query} true or false",
        f"{query} misleading",
    ]
    fact_articles = []
    for fc_q in fc_queries:
        raw_fc = _google_news_rss(fc_q, limit=5, lang="en-US", gl="US")
        for r in raw_fc:
            if _is_fact_check_item(r) and r["url"] not in seen_urls_all and _passes_reputation(r):
                fact_articles.append(r)
                seen_urls_all.add(r["url"])

    if fact_articles:
        fact_sentences = _fetch_articles_concurrent(fact_articles[:5])
        rss_fact_results = _rank_and_filter(
            fact_sentences, claim_emb, model,
            threshold=LIVE_THRESHOLD * 0.8, top_k=3, max_per_domain=2,
        )
        for r in rss_fact_results:
            r["source_type"] = r.get("source_type", "fact_check_rss")
        fact_results.extend(rss_fact_results)
    print(f"  [LiveSearch] Phase 3 (fact-check total) results: {len(fact_results)}")

    # ── Merge, dedup, return ──────────────────────────────────────────────────
    all_results = fact_results + stats_results + trusted_results + open_results
    all_results.sort(key=lambda x: x["similarity"], reverse=True)

    seen, final = set(), []
    for r in all_results:
        short = r["text"][:80]
        if short not in seen:
            seen.add(short)
            final.append(r)
        if len(final) >= k:
            break

    summary = (
        f"{len(final)} total "
        f"({len(trusted_results)} trusted, {len(open_results)} open, "
        f"{len(stats_results)} stats, {len(fact_results)} fact-check)"
    )
    print(f"  [LiveSearch] Done: {summary}")

    if final:
        final[0]["_live_search_summary"]  = summary
        final[0]["_live_trusted_count"]   = len(trusted_results)
        final[0]["_live_open_count"]      = len(open_results)
        final[0]["_live_stats_count"]     = len(stats_results)
        final[0]["_live_fact_count"]      = len(fact_results)

    _cache_set(cache_key_q, final)
    return final
