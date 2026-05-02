"""
corpus/source_registry.py
Central source registry for SocialProof v3.3
  REPUTATION SCORING RUBRIC
  ─────────────────────────
  1.0  Tier-1 government statistical authority or UN agency:
       PSA, BSP, World Bank, IMF, WHO, UN, FAO.
       Criteria: primary data producer, methodology published, legally accountable.

  0.95 Tier-1 government policy agency (non-statistical):
       NEDA, PIDS, FNRI, DOH, DepEd, DOLE, ADB, UNICEF.
       Criteria: official mandate, regular public reporting.

  0.90 Tier-2 international wire service or major fact-checker:
       Reuters, AP, Vera Files, Tsek.ph, Snopes.
       Criteria: editorial standards published, correction policy visible.

  0.85 Tier-2 quality international press (BBC, Guardian, NPR, ScienceDaily).
       Criteria: editorial charter, ombudsman or standards editor.

  0.80 Tier-3 leading Philippine news (Rappler).
       Criteria: PCIJ membership or equivalent PH press council affiliation.

  0.75 Tier-3 standard Philippine news (Philstar, Inquirer, GMA, BusinessWorld).
       Criteria: regular publication, publicly identified editorial team.

  0.70 Tier-3 Philippine tabloids / community press (Manila Bulletin, mb.com.ph).
       Criteria: publication history ≥ 5 years, traceable ownership.

  < 0.65 Not included. Minimum threshold for inclusion is 0.65.

  REPUTATION_THRESHOLD (0.65) is the floor below which live-search results
  are filtered out entirely in live_search.py.

  To add a new source: set reputation to match the rubric above, document
  justification in the trusted_domains table (see corpus/db.py).
"""

from __future__ import annotations
from typing import Dict, Any, Set

SOURCES: Dict[str, Dict[str, Any]] = {

    # ══════════════════════════════════════════════════════════════════════════
    # TIER 1 — Philippine statistical / policy agencies
    # ══════════════════════════════════════════════════════════════════════════
    # ── NOTE on crawl_mode for PH gov agencies ────────────────────────────────
    # psa, doh, dti, dole, ched, comelec: Cloudflare bot protection blocks
    # Playwright on their section index pages (returns 30KB challenge page with
    # only 2 cloudflare.com links). crawl_mode changed from "deep" → "gnews".
    # The gnews RSS entity-name query reliably returns 100 links; the allinurl
    # query returns 0 (filtered by Cloudflare at the DNS/CDN layer).
    #
    # bsp, neda: ERR_NAME_NOT_RESOLVED from non-PH IPs — needs PH-based runtime.
    # Left as "deep" so they work correctly when run from a PH IP; the scraper
    # now does a DNS pre-check and exits gracefully if unreachable.
    # ─────────────────────────────────────────────────────────────────────────
    "psa": {
        "domain": "psa.gov.ph", "tier": 1, "reputation": 1.0,
        # CHANGED: deep → gnews. Cloudflare blocks section index pages.
        # The entity-name RSS query returns 100 links reliably.
        # allinurl query removed — returns 0 results (CDN-filtered).
        "pipeline": "stats", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": ["/statistics/", "/press-releases/", "/publications/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:365d+%22Philippine+Statistics+Authority%22&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "bsp": {
        "domain": "bsp.gov.ph", "tier": 1, "reputation": 1.0,
        # KEPT: deep. BSP fails with DNS error from non-PH IPs — needs PH runtime.
        # Scraper does DNS pre-check and exits cleanly if unreachable.
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/statistics/", "/monetary-policy/", "/publications/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:365d+%22Bangko+Sentral+ng+Pilipinas%22&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "doh": {
        "domain": "doh.gov.ph", "tier": 1, "reputation": 0.95,
        # CHANGED: deep → gnews. Cloudflare blocks section index pages.
        "pipeline": "stats", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": ["/statistics/", "/news/", "/advisories/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+%22Department+of+Health+Philippines%22&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "deped": {
        "domain": "deped.gov.ph", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/news/", "/press-releases/", "/resources/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+allinurl:deped.gov.ph&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "dole": {
        "domain": "dole.gov.ph", "tier": 1, "reputation": 0.95,
        # CHANGED: deep → gnews. Cloudflare blocks section index pages.
        "pipeline": "stats", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": ["/news/", "/releases/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:365d+%22Department+of+Labor+and+Employment%22+Philippines&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "neda": {
        "domain": "neda.gov.ph", "tier": 1, "reputation": 0.95,
        # KEPT: deep. NEDA fails with DNS timeout from non-PH IPs — needs PH runtime.
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/news/", "/press-releases/", "/data/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:365d+%22National+Economic+and+Development+Authority%22+Philippines&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "fnri": {
        "domain": "fnri.dost.gov.ph", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/news/", "/publications/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:365d+allinurl:fnri.dost.gov.ph&ceid=PH:en&hl=en-PH&gl=PH",
            "https://news.google.com/rss/search?q=when:365d+%22Food+and+Nutrition+Research+Institute%22+Philippines&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "dof": {
        "domain": "dof.gov.ph", "tier": 1, "reputation": 0.95,
        # crawl_mode: deep — DOF pages load without Cloudflare challenge (confirmed).
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/news/", "/press-releases/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:365d+%22Department+of+Finance%22+Philippines&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "comelec": {
        "domain": "comelec.gov.ph", "tier": 1, "reputation": 0.95,
        # CHANGED: deep → gnews. Section pages return connection errors.
        "pipeline": "stats", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": ["/precinct/", "/resolutions/", "/news/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:365d+%22Commission+on+Elections%22+Philippines&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "dti": {
        "domain": "dti.gov.ph", "tier": 1, "reputation": 0.90,
        # CHANGED: deep → gnews. Section pages return connection errors.
        "pipeline": "stats", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": ["/news/", "/press-releases/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:365d+%22Department+of+Trade+and+Industry%22+Philippines&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "ched": {
        "domain": "ched.gov.ph", "tier": 1, "reputation": 0.90,
        # CHANGED: deep → gnews. Section pages return connection errors.
        "pipeline": "stats", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": ["/news/", "/publications/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:365d+%22Commission+on+Higher+Education%22+Philippines&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "pids": {
        "domain": "pids.gov.ph", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "sitemap", "sitemap_url": None,
        "section_paths": ["/publications/", "/news/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:365d+allinurl:pids.gov.ph&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "pna": {
        "domain": "pna.gov.ph", "tier": 1, "reputation": 0.90,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://www.pna.gov.ph/articles/rss",
            "https://news.google.com/rss/search?q=when:30d+allinurl:pna.gov.ph&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },

    # ══════════════════════════════════════════════════════════════════════════
    # TIER 1 — International statistical / policy authorities
    # ══════════════════════════════════════════════════════════════════════════
    "worldbank": {
        "domain": "worldbank.org", "tier": 1, "reputation": 1.0,
        "pipeline": "stats", "crawl_mode": "sitemap", "sitemap_url": None,
        "section_paths": ["/en/news/", "/en/publication/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+allinurl:worldbank.org&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "imf": {
        "domain": "imf.org", "tier": 1, "reputation": 1.0,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/en/News/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+allinurl:imf.org&ceid=US:en&hl=en-US&gl=US",
            "https://news.google.com/rss/search?q=when:90d+%22International+Monetary+Fund%22&ceid=US:en&hl=en-US&gl=US",
        ],
    },
    "un_news": {
        "domain": "un.org", "tier": 1, "reputation": 1.0,
        "pipeline": "stats", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": ["https://news.un.org/feed/subscribe/en/news/all/rss.xml"],
    },
    "who": {
        "domain": "who.int", "tier": 1, "reputation": 1.0,
        "pipeline": "stats", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": ["https://www.who.int/rss-feeds/news-english.xml"],
    },
    "fao": {
        "domain": "fao.org", "tier": 1, "reputation": 1.0,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/news/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+allinurl:fao.org&ceid=US:en&hl=en-US&gl=US",
            "https://news.google.com/rss/search?q=when:90d+%22Food+and+Agriculture+Organization%22&ceid=US:en&hl=en-US&gl=US",
        ],
    },
    "oecd": {
        "domain": "oecd.org", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/newsroom/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+allinurl:oecd.org&ceid=US:en&hl=en-US&gl=US",
        ],
    },
    "unicef": {
        "domain": "unicef.org", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ['/press-releases/', '/reports/', '/stories/'],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+allinurl:unicef.org&ceid=US:en&hl=en-US&gl=US",
        ],
    },
    "unesco": {
        "domain": "unesco.org", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ['/news/', '/reports/', '/press-releases/'],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+allinurl:unesco.org&ceid=US:en&hl=en-US&gl=US",
        ],
    },
    "ilo": {
        "domain": "ilo.org", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ['/news/', '/publications/', '/press-releases/'],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+allinurl:ilo.org&ceid=US:en&hl=en-US&gl=US",
        ],
    },
    "adb": {
        "domain": "adb.org", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "sitemap", "sitemap_url": None,
        "section_paths": ["/news/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+allinurl:adb.org&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "eurostat": {
        "domain": "ec.europa.eu", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ['/statistics-explained/', '/news/'],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+eurostat&ceid=US:en&hl=en-US&gl=US",
        ],
    },
    "us_census": {
        "domain": "census.gov", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "sitemap", "sitemap_url": None,
        "section_paths": ["/newsroom/"],
        "rss_urls": ["https://www.census.gov/rss/www/rss_releases.xml"],
    },
    "uk_ons": {
        "domain": "ons.gov.uk", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "sitemap", "sitemap_url": None,
        "section_paths": ["/news/"],
        "rss_urls": ["https://www.ons.gov.uk/rss/news"],
    },

    # ── Statistical / Data Platforms ──────────────────────────────────────────
    "ourworldindata": {
        "domain": "ourworldindata.org", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/grapher/", "/articles/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:180d+allinurl:ourworldindata.org&ceid=US:en&hl=en-US&gl=US",
        ],
    },

    # ── Scientific / Medical Evidence ─────────────────────────────────────────
    "nih": {
        "domain": "nih.gov", "tier": 1, "reputation": 1.0,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/news-events/"],
        "rss_urls": [
            "https://www.nih.gov/news-events/news-releases/rss.xml",
            "https://news.google.com/rss/search?q=when:90d+allinurl:nih.gov&ceid=US:en&hl=en-US&gl=US",
        ],
    },
    "cdc": {
        # FIX: old config scraped /media/releases/ (product recall notices) and
        # a broken RSS feed (id 316422 = animal food recall feed).
        # New config targets vaccine safety, disease prevention, and health data
        # pages — the actual content useful for fact-checking health claims.
        "domain": "cdc.gov", "tier": 1, "reputation": 1.0,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": [
            "/vaccines/",
            "/flu/",
            "/covid-19/",
            "/healthyweight/",
            "/cancer/",
            "/diabetes/",
            "/heartdisease/",
        ],
        "rss_urls": [
            # Official CDC newsroom — press releases on disease, vaccines, public health
            "https://tools.cdc.gov/api/v2/resources/media/403372.rss",
            # Google News filtered to CDC health/disease topics only
            "https://news.google.com/rss/search?q=when:90d+allinurl:cdc.gov+(vaccine+OR+disease+OR+flu+OR+covid+OR+cancer+OR+diabetes)&ceid=US:en&hl=en-US&gl=US",
        ],
    },
    "nature": {
        "domain": "nature.com", "tier": 2, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ['/articles/', '/news/'],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:180d+allinurl:nature.com+(vaccine+OR+climate+OR+cancer+OR+pandemic+OR+nutrition+OR+health)&ceid=US:en&hl=en-US&gl=US",
            "https://news.google.com/rss/search?q=when:180d+\"published+in+Nature\"+OR+\"study+in+Nature\"&ceid=US:en&hl=en-US&gl=US",
        ],
    },
    "thelancet": {
        "domain": "thelancet.com", "tier": 2, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ['/journals/lancet/onlinefirst/', '/news/'],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+allinurl:thelancet.com&ceid=US:en&hl=en-US&gl=US",
        ],
    },
    "bmj": {
        "domain": "bmj.com", "tier": 2, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ['/latest-news/', '/news/'],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+allinurl:bmj.com&ceid=US:en&hl=en-US&gl=US",
        ],
    },

    # ══════════════════════════════════════════════════════════════════════════
    # TIER 2 — International wire services and major quality press
    # ══════════════════════════════════════════════════════════════════════════
    "reuters_via_gnews": {
        "domain": "reuters.com", "tier": 2, "reputation": 0.90,
        "pipeline": "news", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:7d+allinurl:reuters.com&ceid=US:en&hl=en-US&gl=US",
        ],
    },
    "apnews_via_gnews": {
        "domain": "apnews.com", "tier": 2, "reputation": 0.90,
        "pipeline": "news", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:7d+allinurl:apnews.com&ceid=US:en&hl=en-US&gl=US",
        ],
    },
    "bbc_via_gnews": {
        "domain": "bbc.com", "tier": 2, "reputation": 0.90,
        "pipeline": "news", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:7d+allinurl:bbc.com&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "bbc_direct": {
        "domain": "bbc.com", "tier": 2, "reputation": 0.90,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
            "https://feeds.bbci.co.uk/news/health/rss.xml",
            "https://feeds.bbci.co.uk/news/technology/rss.xml",
            "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
            "https://feeds.bbci.co.uk/news/rss.xml",
        ],
    },
    "guardian": {
        "domain": "theguardian.com", "tier": 2, "reputation": 0.85,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://www.theguardian.com/world/rss",
            "https://www.theguardian.com/science/rss",
            "https://www.theguardian.com/society/rss",
            "https://www.theguardian.com/technology/rss",
        ],
    },
    "npr": {
        "domain": "npr.org", "tier": 2, "reputation": 0.85,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://feeds.npr.org/1001/rss.xml",
            "https://feeds.npr.org/1128/rss.xml",
            "https://feeds.npr.org/1027/rss.xml",
        ],
    },
    "sciencedaily": {
        "domain": "sciencedaily.com", "tier": 2, "reputation": 0.85,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://www.sciencedaily.com/rss/all.xml",
            "https://www.sciencedaily.com/rss/health_medicine.xml",
            "https://www.sciencedaily.com/rss/mind_brain.xml",
            "https://www.sciencedaily.com/rss/science_society.xml",
        ],
    },

    # ══════════════════════════════════════════════════════════════════════════
    # TIER 3 — Philippine News
    # ══════════════════════════════════════════════════════════════════════════
    "rappler": {
        "domain": "rappler.com", "tier": 3, "reputation": 0.80,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": ["/nation/", "/world/", "/moveph/"],
        "rss_urls": [
            "https://www.rappler.com/nation/feed/",
            "https://www.rappler.com/world/feed/",
            "https://www.rappler.com/business/feed/",
            "https://www.rappler.com/science/feed/",
            # Rappler fact-check section
            "https://www.rappler.com/newsbreak/fact-check/feed/",
        ],
    },
    "philstar": {
        "domain": "philstar.com", "tier": 3, "reputation": 0.75,
        "pipeline": "news", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": ["/headlines/", "/nation/", "/business/"],
        "rss_urls": [
            "https://www.philstar.com/rss/headlines",
            "https://www.philstar.com/rss/nation",
            "https://www.philstar.com/rss/business",
            "https://news.google.com/rss/search?q=when:30d+allinurl:philstar.com&ceid=PH:en&hl=en-PH&gl=PH",
        ],
    },
    "businessworld": {
        "domain": "bworldonline.com", "tier": 3, "reputation": 0.75,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": ["https://www.bworldonline.com/feed/"],
    },
    "gmanews": {
        "domain": "gmanetwork.com", "tier": 3, "reputation": 0.75,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            # Updated 2025 — old /news/rss/news/feed/ paths returned 404.
            # Current canonical GMA RSS endpoints:
            "https://www.gmanetwork.com/news/rss/news.xml",
            "https://www.gmanetwork.com/news/rss/economy.xml",
            "https://www.gmanetwork.com/news/rss/publicaffairs.xml",
        ],
    },
    "inquirer": {
        "domain": "inquirer.net", "tier": 3, "reputation": 0.75,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://newsinfo.inquirer.net/feed/",
            "https://globalnation.inquirer.net/feed/",
            "https://business.inquirer.net/feed/",
        ],
    },
    "cnnphilippines": {
        "domain": "cnnphilippines.com", "tier": 3, "reputation": 0.75,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            # Switched from gnews (503-blocked) to native RSS feed
            "https://www.cnnphilippines.com/rss/rss.aspx",
        ],
    },
    "manilabulletin": {
        "domain": "manilabulletin.com.ph", "tier": 3, "reputation": 0.70,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://mb.com.ph/category/news/national/feed/",
            "https://mb.com.ph/category/news/feed/",
        ],
    },
    "mb_com_ph": {
        "domain": "mb.com.ph", "tier": 3, "reputation": 0.70,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            # Switched from gnews (503-blocked) to native RSS feeds
            "https://mb.com.ph/category/news/national/feed/",
            "https://mb.com.ph/category/news/feed/",
        ],
    },
    "aljazeera": {
        "domain": "aljazeera.com", "tier": 2, "reputation": 0.85,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://www.aljazeera.com/xml/rss/all.xml",
            "https://www.aljazeera.com/xml/rss/news.xml",
        ],
    },
    "abscbn": {
        "domain": "news.abs-cbn.com", "tier": 3, "reputation": 0.75,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://news.abs-cbn.com/rss/news",
            "https://news.abs-cbn.com/rss/nation",
        ],
    },
    "britannica": {
        "domain": "britannica.com", "tier": 2, "reputation": 0.80,
        "pipeline": "news", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": ["/topic/"],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:180d+allinurl:britannica.com&ceid=US:en&hl=en-US&gl=US",
        ],
    },

    # ══════════════════════════════════════════════════════════════════════════
    # TIER 2/3 — Fact-checkers
    # ══════════════════════════════════════════════════════════════════════════
    "verafiles": {
        "domain": "verafiles.org", "tier": 2, "reputation": 0.90,
        "pipeline": "factcheck", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://verafiles.org/feed/",
        ],
    },
    "tsekph_via_gnews": {
        "domain": "tsek.ph", "tier": 2, "reputation": 0.90,
        "pipeline": "factcheck", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://tsek.ph/feed/",
        ],
    },
    "snopes": {
        "domain": "snopes.com", "tier": 2, "reputation": 0.90,
        "pipeline": "factcheck", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": ["https://www.snopes.com/feed/"],
    },
    "wikipedia": {
        "domain": "en.wikipedia.org", "tier": 2, "reputation": 0.90,
        "pipeline": "factcheck", "crawl_mode": "api", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [],
    },
    "politifact": {
        "domain": "politifact.com", "tier": 2, "reputation": 0.90,
        "pipeline": "factcheck", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://www.politifact.com/rss/rulings/",
            "https://www.politifact.com/rss/all/",
        ],
    },
    "factcheck_org": {
        "domain": "factcheck.org", "tier": 2, "reputation": 0.90,
        "pipeline": "factcheck", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": ["https://www.factcheck.org/feed/"],
    },
    "fullfact": {
        "domain": "fullfact.org", "tier": 2, "reputation": 0.90,
        "pipeline": "factcheck", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": ["https://fullfact.org/feed/"],
    },
    "reuters_factcheck": {
        "domain": "reuters.com", "tier": 2, "reputation": 0.90,
        "pipeline": "factcheck", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": ["/fact-check/"],
        "rss_urls": [
            "https://feeds.reuters.com/reuters/CNfactcheck",
            "https://feeds.reuters.com/reuters/worldnews",
        ],
    },
    "ap_factcheck": {
        "domain": "apnews.com", "tier": 2, "reputation": 0.90,
        "pipeline": "factcheck", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": ["/ap-fact-check/"],
        "rss_urls": [
            "https://rsshub.app/apnews/topics/apf-topnews",
            "https://feeds.apnews.com/APTopHeadlines.rss",
        ],
    },
    "psa_via_news": {
        "domain": "psa.gov.ph", "tier": 1, "reputation": 1.0,
        "pipeline": "stats", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        # These queries match news articles quoting PSA data — from any outlet.
        # crawl_mode: rss (not gnews) so domain filter is NOT applied.
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+%22PSA%22+%22Philippine+Statistics%22+(inflation+OR+GDP+OR+unemployment+OR+poverty+OR+census)&ceid=PH:en&hl=en-PH&gl=PH",
        ],
        "_note": "Capture source: scrapes news articles quoting PSA data, not psa.gov.ph directly.",
    },
    "bsp_via_news": {
        "domain": "bsp.gov.ph", "tier": 1, "reputation": 1.0,
        "pipeline": "stats", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+%22Bangko+Sentral%22+(inflation+OR+%22interest+rate%22+OR+%22monetary+policy%22+OR+peso+OR+reserves)&ceid=PH:en&hl=en-PH&gl=PH",
        ],
        "_note": "Capture source: scrapes news articles quoting BSP monetary data.",
    },
    "doh_via_news": {
        "domain": "doh.gov.ph", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://news.google.com/rss/search?q=when:90d+%22Department+of+Health%22+Philippines+(vaccine+OR+dengue+OR+tuberculosis+OR+malnutrition+OR+mortality)&ceid=PH:en&hl=en-PH&gl=PH",
        ],
        "_note": "Capture source: scrapes news articles quoting DOH health data.",
    },
    "pagasa": {
        "domain": "pagasa.dost.gov.ph", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/en/climate/climate-data/", "/en/weather/weather-bulletin/", "/en/news/"],
        "rss_urls": [],
        # Highest-priority gov addition: typhoon/weather data is the #1
        # misinformation vector in PH disaster contexts.
    },
    "officialgazette": {
        "domain": "officialgazette.gov.ph", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": ["/section/laws/", "/section/executive-issuances/"],
        "rss_urls": [
            "https://www.officialgazette.gov.ph/feed/",
        ],
        # Definitively settles "did the president sign X" and law-existence claims.
    },
    "phivolcs": {
        "domain": "phivolcs.dost.gov.ph", "tier": 1, "reputation": 0.95,
        "pipeline": "stats", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/index.php/news/", "/index.php/volcano-bulletin/", "/index.php/earthquake/"],
        "rss_urls": [],
    },
    "senate_ph": {
        "domain": "senate.gov.ph", "tier": 2, "reputation": 0.85,
        "pipeline": "news", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/en/press-release/", "/en/legislative-documents/bills/"],
        "rss_urls": [],
    },
    "congress_ph": {
        "domain": "congress.gov.ph", "tier": 2, "reputation": 0.85,
        "pipeline": "news", "crawl_mode": "deep", "sitemap_url": None,
        "section_paths": ["/legisdocs/", "/press/"],
        "rss_urls": [],
    },
    "ptvnews": {
        "domain": "ptvnews.ph", "tier": 3, "reputation": 0.80,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://ptvnews.ph/feed/",
        ],
        # State-run broadcast — official government announcements.
    },

    # ── PH News (missing mainstream sources) ─────────────────────────────────
    "manilatimes": {
        "domain": "manilatimes.net", "tier": 3, "reputation": 0.70,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://www.manilatimes.net/feed/",
        ],
        # One of the oldest PH newspapers — notable gap in prior registry.
    },
    "sunstar": {
        "domain": "sunstar.com.ph", "tier": 3, "reputation": 0.70,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://www.sunstar.com.ph/feed/",
        ],
        # Regional chain — Cebu, Davao, Pampanga; covers Visayas/Mindanao claims.
    },
    "businessmirror": {
        "domain": "businessmirror.com.ph", "tier": 3, "reputation": 0.72,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://businessmirror.com.ph/feed/",
        ],
    },
    "onenews": {
        "domain": "one.news.ph", "tier": 3, "reputation": 0.72,
        "pipeline": "news", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": [],
        "rss_urls": [
            "https://one.news.ph/rss",
        ],
    },

    # ── Fact-checkers (missing high-value sources) ────────────────────────────
    "afp_factcheck": {
        "domain": "factcheck.afp.com", "tier": 1, "reputation": 0.90,
        "pipeline": "factcheck", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": ["/en/"],
        "rss_urls": [
            "https://factcheck.afp.com/en/rss.xml",
        ],
        # IFCN signatory, heavy PH coverage — among the most valuable additions.
    },
    "abscbn_factcheck": {
        "domain": "news.abs-cbn.com", "tier": 2, "reputation": 0.88,
        "pipeline": "factcheck", "crawl_mode": "rss", "sitemap_url": None,
        "section_paths": ["/factcheck/"],
        "rss_urls": [
            "https://news.abs-cbn.com/rss/factcheck",
        ],
        # ABS-CBN dedicated fact-check unit — separate entry from main news feed.
    },

    # ── PH Tabloids — for SourceCredibilityModule detection, NOT as evidence ──
    # These are registered so the system returns a scored credibility result
    # (e.g. 0.65) for content from these domains rather than treating them as
    # unknown sources (default 0.50 neutral). They are not scraped for corpus.
    # Panel defense answer: "Included for recognition and scoring, not as
    # evidence providers."
    "abante": {
        "domain": "abante.com.ph", "tier": 4, "reputation": 0.65,
        "pipeline": "news", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": [], "rss_urls": [],
    },
    "remate": {
        "domain": "remate.ph", "tier": 4, "reputation": 0.65,
        "pipeline": "news", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": [], "rss_urls": [],
    },
    "bulgar": {
        "domain": "bulgar.com.ph", "tier": 4, "reputation": 0.65,
        "pipeline": "news", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": [], "rss_urls": [],
    },
    "pilipino_star_ngayon": {
        "domain": "pilipino-star-ngayon.com", "tier": 4, "reputation": 0.65,
        "pipeline": "news", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": [], "rss_urls": [],
    },
    "abante_tonite": {
        "domain": "abante-tonite.com", "tier": 4, "reputation": 0.65,
        "pipeline": "news", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": [], "rss_urls": [],
    },
    "peoples_journal": {
        "domain": "peoplesjournal.net", "tier": 4, "reputation": 0.65,
        "pipeline": "news", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": [], "rss_urls": [],
    },
    "tempo_ph": {
        "domain": "tempo.com.ph", "tier": 4, "reputation": 0.67,
        "pipeline": "news", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": [], "rss_urls": [],
    },
    "kami_ph": {
        "domain": "kami.com.ph", "tier": 4, "reputation": 0.65,
        "pipeline": "news", "crawl_mode": "gnews", "sitemap_url": None,
        "section_paths": [], "rss_urls": [],
    },
}

# ── Quick-access derived sets ─────────────────────────────────────────────────

ALL_DOMAINS: Set[str] = {cfg["domain"] for cfg in SOURCES.values()}

TIER1_DOMAINS: Set[str] = {cfg["domain"] for cfg in SOURCES.values() if cfg["tier"] == 1}
TIER2_DOMAINS: Set[str] = {cfg["domain"] for cfg in SOURCES.values() if cfg["tier"] == 2}
TIER3_DOMAINS: Set[str] = {cfg["domain"] for cfg in SOURCES.values() if cfg["tier"] == 3}

STATS_DOMAINS:     Set[str] = {cfg["domain"] for cfg in SOURCES.values() if cfg["pipeline"] == "stats"}
FACTCHECK_DOMAINS: Set[str] = {cfg["domain"] for cfg in SOURCES.values() if cfg["pipeline"] == "factcheck"}
NEWS_DOMAINS:      Set[str] = {cfg["domain"] for cfg in SOURCES.values() if cfg["pipeline"] == "news"}

REPUTATION: Dict[str, float] = {cfg["domain"]: cfg["reputation"] for cfg in SOURCES.values()}

PIPELINE_MAP: Dict[str, str] = {cfg["domain"]: cfg["pipeline"] for cfg in SOURCES.values()}
TIER_MAP: Dict[str, int]     = {cfg["domain"]: cfg["tier"]     for cfg in SOURCES.values()}

SOURCE_GROUPS: Dict[str, list] = {
    "ph_news":   ["rappler", "philstar", "businessworld", "gmanews", "inquirer",
                  "cnnphilippines", "manilabulletin", "pna", "mb_com_ph",
                  "abscbn", "manilatimes", "sunstar", "businessmirror",
                  "onenews", "ptvnews"],
    "ph_fact":   ["verafiles", "tsekph_via_gnews", "afp_factcheck",
                  "abscbn_factcheck"],
    "ph_gov":    ["psa", "bsp", "doh", "deped", "neda", "dof", "comelec",
                  "dti", "dole", "ched", "fnri", "pids",
                  "pagasa", "officialgazette", "phivolcs",
                  "senate_ph", "congress_ph"],
    # ph_gov_capture: news articles quoting gov agencies — use when gov sites
    # are Cloudflare-blocked or DNS-unreachable. Run after ph_gov to fill gaps.
    "ph_gov_capture": ["psa_via_news", "bsp_via_news", "doh_via_news"],
    "intl_org":  ["worldbank", "un_news", "imf", "oecd", "unicef", "unesco",
                  "fao", "ilo", "adb", "eurostat", "us_census", "uk_ons",
                  "ourworldindata"],
    "intl_news": ["reuters_via_gnews", "apnews_via_gnews", "bbc_via_gnews",
                  "bbc_direct", "guardian", "npr", "aljazeera"],
    "science":   ["sciencedaily", "who", "nih", "cdc", "nature", "thelancet", "bmj"],
    "factcheck": ["snopes", "politifact", "factcheck_org", "fullfact",
                  "reuters_factcheck", "ap_factcheck",
                  "afp_factcheck",
                  "verafiles", "tsekph_via_gnews",
                  "wikipedia"],   # PATCH: Wikipedia added for structured factual grounding
    "derived":   ["britannica", "wikipedia"],
    # Tabloids group — for credibility scoring detection only, not scraped
    "ph_tabloids": ["abante", "remate", "bulgar", "pilipino_star_ngayon",
                    "abante_tonite", "peoples_journal", "tempo_ph", "kami_ph"],
}

PLAYWRIGHT_DOMAINS: Set[str] = {
    "gmanetwork.com", "inquirer.net", "cnnphilippines.com",
    "manilabulletin.com.ph", "philstar.com", "snopes.com", "verafiles.org",
    # tsek.ph returns 403 to plain requests — requires headless Chromium
    "tsek.ph",
    # PH gov agencies: JS-heavy pages, content only loads after JS execution
    "psa.gov.ph", "bsp.gov.ph", "doh.gov.ph", "neda.gov.ph",
}

# Domains where the Playwright gnews resolver never succeeds (Cloudflare or
# google.com/sorry interstitial blocks it). For these domains, _resolve_gnews_url
# skips the Playwright step and relies on requests only, failing fast to None.
GNEWS_PLAYWRIGHT_SKIP: Set[str] = {
    "psa.gov.ph", "doh.gov.ph", "dti.gov.ph", "dole.gov.ph",
    "ched.gov.ph", "comelec.gov.ph",
}

DOMAIN_DELAYS: Dict[str, float] = {
    "gmanetwork.com": 3.5, "inquirer.net": 3.5, "cnnphilippines.com": 3.5,
    "philstar.com": 3.0, "rappler.com": 2.5, "mb.com.ph": 2.5,
    "bworldonline.com": 2.0, "manilabulletin.com.ph": 2.0,
}
DEFAULT_DELAY: float        = 1.5
REPUTATION_THRESHOLD: float = 0.65  # minimum to include in live results

# ── Per-domain sentence caps ───────────────────────────────────────────────────
# Controls how many sentences the scraper and index builder will keep per domain.
# Without this, high-volume sources (e.g. nature.com) can flood the DB and index.
#
# Reasoning per entry:
#   nature.com   — scientific journal; useful for vaccine/climate claims but
#                  articles are very long; 200 sentences covers ~5-10 papers.
#   npr.org      — international news; 300 is enough for broad topic coverage.
#   Default 500  — generous ceiling for all other sources; prevents runaway
#                  scrapes while not artificially limiting PH gov sources which
#                  are already small by nature.
#
# To raise or lower a cap: edit the number here, then run:
#   python corpus/reset_db.py --confirm
#   python corpus/scraper.py --balanced
#   python retrieval/build_index.py --rebuild
SENTENCE_CAPS: Dict[str, int] = {}
DEFAULT_SENTENCE_CAP: int = 999_999  # no cap


# ── Per-domain Playwright timeouts (milliseconds) ─────────────────────────────
# PH government sites are JS-heavy and slow. Higher timeouts prevent premature
# failures on first load. Deep crawler uses these via get_playwright_timeout().
#
# Reasoning:
#   PH gov (.gov.ph) — Drupal/CMS sites, full JS render needed: 45s
#   Standard Playwright domains (snopes, GMA, etc.) — 30s is usually fine
#   Default — 30s matches the existing hardcoded value in scraper.py
PLAYWRIGHT_TIMEOUTS: Dict[str, int] = {
    "psa.gov.ph":          45_000,
    "bsp.gov.ph":          45_000,
    "doh.gov.ph":          45_000,
    "neda.gov.ph":         45_000,
    "deped.gov.ph":        45_000,
    "dole.gov.ph":         45_000,
    "fnri.dost.gov.ph":    45_000,
    "dof.gov.ph":          45_000,
    "dti.gov.ph":          45_000,
    "ched.gov.ph":         45_000,
    "comelec.gov.ph":      45_000,
    "pids.gov.ph":         45_000,
}
DEFAULT_PLAYWRIGHT_TIMEOUT: int = 30_000


def get_sentence_cap(domain: str) -> int:
    """Return the max sentences to store/index for a given domain."""
    return SENTENCE_CAPS.get(domain, DEFAULT_SENTENCE_CAP)


def get_delay(domain: str) -> float:
    return DOMAIN_DELAYS.get(domain, DEFAULT_DELAY)


def get_playwright_timeout(domain: str) -> int:
    """Return Playwright page.goto() timeout in milliseconds for a domain."""
    return PLAYWRIGHT_TIMEOUTS.get(domain, DEFAULT_PLAYWRIGHT_TIMEOUT)

def get_reputation(domain: str) -> float:
    """Return reputation score. Unknown domains get 0.5 (neutral)."""
    return REPUTATION.get(domain, 0.5)

def get_pipeline(domain: str) -> str:
    return PIPELINE_MAP.get(domain, "news")

def get_tier(domain: str) -> int:
    return TIER_MAP.get(domain, 3)