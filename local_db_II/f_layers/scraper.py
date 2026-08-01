#!/usr/bin/env python3
"""
scraper.py -- LAYER 2
Trusted Government + Maharashtra krishi (agriculture) sites se
crop/disease ke baare me fungicide/fertilizer info scrape karta hai.

Trusted sources (hardcoded, jaanbujh kar limited rakha hai taaki
random/untrusted sites se data na aaye):
    - krishi.maharashtra.gov.in   -> Maharashtra Govt Krishi Vibhag
    - mpkv.ac.in                  -> Mahatma Phule Krishi Vidyapeeth, Rahuri
    - pdkv.ac.in                  -> Dr. Panjabrao Deshmukh Krishi Vidyapeeth, Akola
    - vnmkv.ac.in                 -> Vasantrao Naik Marathwada Krishi Vidyapeeth, Parbhani
    - ncipm.icar.gov.in           -> ICAR - Integrated Pest Management institute
    - icar.org.in                 -> ICAR (central research body)
    - farmer.gov.in               -> Farmer Portal, Govt of India
    - mkisan.gov.in                -> mKisan advisory portal, Govt of India
    - cibrc.nic.in                -> Central Insecticides Board (layer-1 ka source)

Requirements (apne local machine pe install kar, sandbox me nahi chalega
kyunki yaha internet band hai):
    pip install requests beautifulsoup4 ddgs
"""

import sqlite3
import time

import requests
from bs4 import BeautifulSoup

TRUSTED_DOMAINS = {
    "krishi.maharashtra.gov.in": "Maharashtra Krishi Vibhag (State Govt)",
    "mpkv.ac.in": "Mahatma Phule Krishi Vidyapeeth, Rahuri",
    "pdkv.ac.in": "Dr. Panjabrao Deshmukh Krishi Vidyapeeth, Akola",
    "vnmkv.ac.in": "Vasantrao Naik Marathwada Krishi Vidyapeeth, Parbhani",
    "ncipm.icar.gov.in": "ICAR - National Research Institute for Integrated Pest Management",
    "icar.org.in": "Indian Council of Agricultural Research (ICAR)",
    "farmer.gov.in": "Farmer Portal, Govt of India",
    "mkisan.gov.in": "mKisan Advisory Portal, Govt of India",
    "cibrc.nic.in": "Central Insecticides Board & Registration Committee",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FertilizerBot/1.0)"}


def search_trusted(crop, disease=None, max_per_domain=2, pause=1.5):
    """Har trusted domain ke andar site: search karta hai (DuckDuckGo, no API key)."""
    try:
        from ddgs import DDGS
    except ImportError:
        raise ImportError(
            "ddgs library missing. Install kar: pip install ddgs"
        )

    query_terms = f"{crop} {disease or ''} fungicide fertilizer disease control".strip()
    all_results = []

    with DDGS() as ddgs:
        for domain, source_name in TRUSTED_DOMAINS.items():
            site_query = f"site:{domain} {query_terms}"
            try:
                hits = list(ddgs.text(site_query, max_results=max_per_domain))
            except Exception as e:
                print(f"[warn] '{domain}' search fail hui: {e}")
                hits = []

            for h in hits:
                all_results.append({
                    "domain": domain,
                    "source_name": source_name,
                    "title": h.get("title"),
                    "url": h.get("href"),
                    "snippet": h.get("body"),
                })
            time.sleep(pause)  # rate-limit friendly rehna

    return all_results


def fetch_page_text(url, max_chars=3000, timeout=10):
    """Page khol ke saaf text nikalta hai (script/style/nav/footer hata ke)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        return text[:max_chars]
    except Exception as e:
        return f"[fetch error: {e}]"


def _ensure_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS layer2_scraped (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            crop TEXT,
            disease TEXT,
            source_name TEXT,
            domain TEXT,
            title TEXT,
            url TEXT,
            snippet TEXT,
            content TEXT,
            fetched_at TEXT DEFAULT (datetime('now'))
        )
    """)


def scrape_and_cache(crop, disease=None, db_path="fungicide.db", fetch_full_page=True):
    """
    Trusted sites search karo, page content nikalo, aur sqlite me cache
    kar do (baar baar internet call na karni pade).
    Returns: list of dict results.
    """
    results = search_trusted(crop, disease)

    con = sqlite3.connect(db_path)
    _ensure_table(con)

    for r in results:
        content = None
        if fetch_full_page and r.get("url"):
            content = fetch_page_text(r["url"])
        r["content"] = content
        con.execute(
            """INSERT INTO layer2_scraped
               (crop, disease, source_name, domain, title, url, snippet, content)
               VALUES (?,?,?,?,?,?,?,?)""",
            (crop, disease, r["source_name"], r["domain"], r.get("title"),
             r.get("url"), r.get("snippet"), content),
        )

    con.commit()
    con.close()
    return results


if __name__ == "__main__":
    import sys
    crop_arg = sys.argv[1] if len(sys.argv) > 1 else "Tomato"
    disease_arg = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"Searching trusted sources for: {crop_arg} / {disease_arg}")
    res = scrape_and_cache(crop_arg, disease_arg)

    if not res:
        print("Kuch nahi mila (ya internet/library issue hai).")
    for r in res:
        print(f"\n[{r['source_name']}] {r['title']}")
        print(r["url"])
        print((r.get("content") or r.get("snippet") or "")[:200], "...")
