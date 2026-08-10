#!/usr/bin/env python3
"""
brand_mapper.py -- Chemical/active-ingredient naam (jaise "Trichoderma
viride") se REAL MARKET BRAND NAME dhundhta hai, ab ek naya trusted layer
ke saath: CIB&RC (Central Insecticides Board & Registration Committee) ki
apni official biopesticide registry (cibrc_registry.json, jo
parse_cibrc_registry.py se banti hai) -- yeh real, legally-verified
government data hai, web-scrape se pehle check hota hai.

NAYA FALLBACK ORDER (3 layers)
--------------------------------
Layer A -- CIBRC Registry (local, deterministic, government-verified):
    Fuzzy match chemical naam ko cibrc_registry.json ke 'pesticide' field
    se. Milta hai to REAL, LEGALLY-REGISTERED company list milti hai --
    lekin CIBRC registry sirf company/formulation/section batati hai,
    RETAIL BRAND NAME nahi (jaise "Bavistin"). Isliye:

Layer B -- Brand-name web scrape (BigHaat.com):
    Trusted e-commerce se retail brand name dhundo. Agar CIBRC se company
    list mil chuki hai, to scrape result ko us list se cross-verify karo --
    agar scraped product ki company CIBRC list me bhi hai, to confidence
    zyada high hoti hai (verified match), warna bhi result dikhta hai
    lekin "unverified_company" flag ke saath.

Layer C -- Raw CIBRC info as last resort:
    Agar Layer B kuch nahi deta (scrape fail, ya library missing), to bhi
    Layer A ka result khali mat chhodo -- kam se kam yeh batao ki chemical
    legally kis company/formulation ke naam se CIBRC-registered hai, taaki
    farmer/system ko pata ho ki yeh ek asli, registered product hai, sirf
    retail brand name nahi mila.

Agar Layer A bhi kuch nahi deta (chemical registry me hi nahi hai), tab
purane wale pure-scrape flow pe fall back hota hai -- kabhi bhi silently
empty result nahi deta bina kuch try kiye.

Requirements (apne local machine pe):
    pip install requests beautifulsoup4 ddgs
"""

import re
import sqlite3
import time
import json
import os
import difflib

import requests
from bs4 import BeautifulSoup

TRUSTED_STORES = {
    "bighaat.com": "BigHaat (verified agri e-commerce)",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BrandMapperBot/1.0)"}

DB_PATH = "brand_names.db"
CIBRC_REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "cibrc_registry.json")

_STOP_WORDS = {"var", "of", "the", "and"}


# ---------------------------------------------------------------------------
# LAYER A: CIBRC Registry lookup (real, government-verified, local, fast)
# ---------------------------------------------------------------------------
def _load_cibrc_registry() -> list[dict]:
    if not os.path.isfile(CIBRC_REGISTRY_PATH):
        return []
    with open(CIBRC_REGISTRY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _fuzzy_score(query: str, candidate: str) -> float:
    """Same word-overlap + sequence-ratio approach used elsewhere in this
    project's fuzzy matching (proven more reliable than raw SequenceMatcher
    alone for short technical names with word-order/spelling variance)."""
    query_words = {w for w in query.lower().split() if w not in _STOP_WORDS and len(w) > 2}
    candidate_words = {w for w in candidate.lower().split() if w not in _STOP_WORDS}

    if query_words:
        matched = sum(
            1 if (qw in candidate_words or any(qw in cw or cw in qw for cw in candidate_words))
            else (0.5 if difflib.get_close_matches(qw, candidate_words, n=1, cutoff=0.8) else 0)
            for qw in query_words
        )
        word_score = matched / len(query_words)
    else:
        word_score = 0.0

    seq_score = difflib.SequenceMatcher(None, query.lower(), candidate.lower()).ratio()
    return max(word_score, seq_score)


def lookup_cibrc_registry(chemical: str, min_score: float = 0.7) -> list[dict]:
    """Fuzzy-matches `chemical` against every registry entry's 'pesticide'
    field. Returns ALL matches above min_score, sorted best-first --
    a chemical is often registered under multiple companies."""
    registry = _load_cibrc_registry()
    if not registry:
        return []

    scored = []
    for entry in registry:
        if entry.get("status") != "parsed":
            continue
        score = _fuzzy_score(chemical, entry.get("pesticide", ""))
        if score >= min_score:
            scored.append({**entry, "match_score": round(score, 3)})

    scored.sort(key=lambda e: e["match_score"], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# LAYER B: Brand-name web scrape (unchanged core logic from before)
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


def _cross_verify(scraped_company: str, cibrc_matches: list[dict]) -> bool:
    """True if the scraped product's company plausibly matches one of the
    CIBRC-registered companies for this chemical -- boosts confidence."""
    if not scraped_company or not cibrc_matches:
        return False
    for entry in cibrc_matches:
        if _fuzzy_score(scraped_company, entry.get("company", "")) >= 0.5:
            return True
    return False


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
# MAIN ENTRY POINT: 3-layer fallback
# ---------------------------------------------------------------------------
def get_brand(chemical, db_path=DB_PATH, use_cache=True, force_refresh=False, allow_scrape=True):
    """
    Returns a dict with:
      - "layer": which layer actually produced the result ("cibrc_registry",
                 "web_scrape", "web_scrape_verified", or "cibrc_fallback")
      - "brand_matches": list of scraped brand results (may be empty)
      - "cibrc_matches": list of CIBRC-registered entries (may be empty)
      - "did_network_scrape": True only if an actual HTTP request went out
        (used by callers to throttle Layer B without also throttling the
        free/local CIBRC lookup)
    Never silently returns nothing if EITHER layer found something.

    allow_scrape=False skips Layer B entirely (used once a caller's live-
    scrape budget is exhausted) -- Layer A (CIBRC) always runs regardless,
    since it's local and costs nothing.
    """
    con = sqlite3.connect(db_path)
    _ensure_table(con)

    # LAYER A: CIBRC registry (fast, local, always tried first -- NEVER throttled)
    cibrc_matches = lookup_cibrc_registry(chemical)

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

    # Cross-verify scraped brands against CIBRC-registered companies
    for match in brand_matches:
        match["cibrc_verified"] = _cross_verify(match.get("company"), cibrc_matches)

    if brand_matches:
        layer = "web_scrape_verified" if any(m["cibrc_verified"] for m in brand_matches) else "web_scrape"
    elif cibrc_matches:
        layer = "cibrc_fallback"  # LAYER C: no retail brand found, but real registration data exists
    else:
        layer = "none"

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

    if result["cibrc_matches"]:
        print(f"\nCIBRC-registered entries found ({len(result['cibrc_matches'])}):")
        for m in result["cibrc_matches"][:5]:
            print(f"  - {m['pesticide']} | {m['company']} | {m.get('formulation')} | Section {m.get('section')} (match {m['match_score']})")

    if result["brand_matches"]:
        print(f"\nRetail brand matches found ({len(result['brand_matches'])}):")
        for m in result["brand_matches"]:
            verified = " [CIBRC-VERIFIED COMPANY]" if m.get("cibrc_verified") else ""
            print(f"  - Brand: {m.get('brand_name')} | Company: {m.get('company')} | {m.get('product_url')}{verified}")

    if not result["cibrc_matches"] and not result["brand_matches"]:
        print(f"\n'{chem}' -- koi match nahi mila CIBRC registry me na web scrape se.")
