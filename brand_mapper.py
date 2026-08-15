#!/usr/bin/env python3
"""
brand_mapper.py -- Chemical/active-ingredient naam (jaise "Trichoderma
viride") se REAL MARKET BRAND NAME dhundhta hai.

FLOW (single layer: web scrape)
--------------------------------
1. Cache check (brand_names.db) -- agar pehle scrape ho chuka hai to
   wahi cached result use hota hai, koi naya HTTP request nahi jaata.
2. Live scrape (BigHaat.com) -- cache miss hone par ddgs se BigHaat.com
   pe chemical search hota hai, matching product pages fetch/parse
   hoti hain, aur brand/company naam extract karke cache mein save
   ho jaata hai.

NOTE: CIBRC government-registry cross-check layer hata di gayi hai
(cibrc_registry.json delete ho chuka tha, isliye woh layer hamesha
khaali result deta tha -- ab pure web-scrape flow hi chalta hai).

Requirements (apne local machine pe):
    pip install requests beautifulsoup4 ddgs
"""

import re
import sqlite3
import time

import requests
from bs4 import BeautifulSoup

TRUSTED_STORES = {
    "bighaat.com": "BigHaat (verified agri e-commerce)",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BrandMapperBot/1.0)"}

DB_PATH = "brand_names.db"


# ---------------------------------------------------------------------------
# Brand-name web scrape
# ---------------------------------------------------------------------------
def _extract_brand_company(title, page_text):
    brand, company = None, None

    m = re.search(r"(?:Buy|Shop)\s+(.+?)\s+Fungicide\s+by\s+([\w\s]+?)\s*[|\-]", title, re.I)
    if m:
        brand, company = m.group(1).strip(), m.group(2).strip()
        return brand, company

    m = re.search(r"(?:Buy|Shop)\s+(.+?)\s+Fungicide", title, re.I)
    if m:
        brand = m.group(1).strip()

    m2 = re.search(r"is\s+an?\s+([\w\s\.]+?)\s+product\s+containing", page_text, re.I)
    if m2:
        company = m2.group(1).strip()

    return brand, company


def search_brand(chemical, max_results=3, pause=1.5):
    try:
        from ddgs import DDGS
    except ImportError:
        raise ImportError("ddgs missing. Install kar: pip install ddgs")

    results = []
    with DDGS() as ddgs:
        for domain, store_name in TRUSTED_STORES.items():
            query = f"site:{domain} {chemical} fungicide"
            try:
                hits = list(ddgs.text(query, max_results=max_results))
            except Exception as e:
                print(f"[warn] '{domain}' search fail hui: {e}")
                hits = []

            for h in hits:
                url = h.get("href")
                if not url:
                    continue
                try:
                    resp = requests.get(url, headers=HEADERS, timeout=10)
                    resp.raise_for_status()
                    soup = BeautifulSoup(resp.text, "html.parser")
                    title = soup.title.string if soup.title else h.get("title", "")
                    for tag in soup(["script", "style", "nav", "footer"]):
                        tag.decompose()
                    page_text = " ".join(soup.get_text(separator=" ").split())
                except Exception:
                    title, page_text = h.get("title", ""), h.get("body", "")

                brand, company = _extract_brand_company(title or "", page_text or "")
                results.append({
                    "store": store_name,
                    "domain": domain,
                    "chemical": chemical,
                    "brand_name": brand,
                    "company": company,
                    "product_url": url,
                    "matched_text": (page_text or "")[:300],
                })
            time.sleep(pause)

    return results


# ---------------------------------------------------------------------------
# Cache (unchanged from before)
# ---------------------------------------------------------------------------
def _ensure_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS chemical_brand_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chemical TEXT,
            brand_name TEXT,
            company TEXT,
            product_url TEXT,
            matched_text TEXT,
            fetched_at TEXT DEFAULT (datetime('now'))
        )
    """)


# ---------------------------------------------------------------------------
# MAIN ENTRY POINT
# ---------------------------------------------------------------------------
def get_brand(chemical, db_path=DB_PATH, use_cache=True, force_refresh=False, allow_scrape=True):
    """
    Returns a dict with:
      - "layer": which layer actually produced the result ("web_scrape" or
                 "none")
      - "brand_matches": list of scraped brand results (may be empty)
      - "cibrc_matches": always [] (CIBRC layer removed -- kept as a key
        for backward compatibility with callers like fertilizermodule.py)
      - "did_network_scrape": True only if an actual HTTP request went out
        (used by callers to throttle live scraping)

    allow_scrape=False skips the live scrape entirely (used once a
    caller's live-scrape budget is exhausted) -- cache is still checked.
    """
    con = sqlite3.connect(db_path)
    _ensure_table(con)

    # CIBRC layer removed -- kept as empty list so downstream code
    # (fertilizermodule.py's result.get("cibrc_matches", [])) still works.
    cibrc_matches = []

    # Cache check (brand-name scrape results only)
    brand_matches = []
    if use_cache and not force_refresh:
        cur = con.execute(
            "SELECT brand_name, company, product_url, matched_text "
            "FROM chemical_brand_map WHERE chemical = ? AND brand_name IS NOT NULL",
            (chemical,),
        )
        cached = cur.fetchall()
        if cached:
            brand_matches = [
                {"chemical": chemical, "brand_name": r[0], "company": r[1],
                 "product_url": r[2], "matched_text": r[3], "source": "cache"}
                for r in cached
            ]

    # LAYER B: web scrape (only if not already cached, allowed, AND ddgs installed)
    did_network_scrape = False
    if not brand_matches and allow_scrape:
        try:
            scraped = search_brand(chemical)
            did_network_scrape = True  # a real HTTP round-trip actually happened
            for r in scraped:
                con.execute(
                    """INSERT INTO chemical_brand_map
                       (chemical, brand_name, company, product_url, matched_text)
                       VALUES (?,?,?,?,?)""",
                    (chemical, r.get("brand_name"), r.get("company"),
                     r.get("product_url"), r.get("matched_text")),
                )
            con.commit()
            brand_matches = scraped
        except ImportError as e:
            # ddgs not installed -- this is an instant local failure, NOT a
            # network scrape, so did_network_scrape stays False and this
            # never eats into the caller's scrape budget.
            print(f"[warn] Layer B (web scrape) skipped: {e}")

    con.close()

    layer = "web_scrape" if brand_matches else "none"

    return {
        "chemical": chemical,
        "layer": layer,
        "brand_matches": brand_matches,
        "cibrc_matches": cibrc_matches,
        "did_network_scrape": did_network_scrape,
    }


if __name__ == "__main__":
    import sys

    chem = sys.argv[1] if len(sys.argv) > 1 else "Trichoderma viride"
    result = get_brand(chem)

    print(f"\nChemical queried : {result['chemical']}")
    print(f"Resolved via     : {result['layer']}")

    if result["brand_matches"]:
        print(f"\nRetail brand matches found ({len(result['brand_matches'])}):")
        for m in result["brand_matches"]:
            print(f"  - Brand: {m.get('brand_name')} | Company: {m.get('company')} | {m.get('product_url')}")
    else:
        print(f"\n'{chem}' -- koi match nahi mila web scrape se.")
