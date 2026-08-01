#!/usr/bin/env python3
"""
brand_mapper.py -- Chemical/active-ingredient naam (jaise "Carbendazim 50%
WP") se REAL MARKET BRAND NAME (jaise "Bavistin") dhundhta hai, taaki
farmer ko "Carbendazim 50% WP" jaisa technical naam nahi, seedha dukaan
me milne wala product naam bataya ja sake.

Trusted source: BigHaat.com -- India ka verified agri e-commerce jaha
har product page pe exact composition likha hota hai (govt-registered
format ke bilkul match), isliye chemical-string se brand match karna
reliable hai. (Aur sources baad me isi list me add kar sakte ho.)

Alag database file me store hota hai: brand_names.db
Table: chemical_brand_map (chemical, brand_name, company, product_url,
                            matched_text, fetched_at)

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
    # yaha aage aur verified agri-store domains add kar sakte ho,
    # e.g. "agribegri.com", "dehaat.com" -- jab tak un par bhi
    # composition-per-product listing verify na ho jaaye add mat karo.
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BrandMapperBot/1.0)"}

DB_PATH = "brand_names.db"


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


def _extract_brand_company(title, page_text):
    """
    BigHaat title pattern zyada tar aisa hota hai:
        "Buy <Brand> Fungicide by <Company> | ..."
        "Shop <Brand> Fungicide - <composition> | BigHaat"
    Aur body text me: "<Brand> Fungicide is a <Company> product containing ..."
    """
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
    """Trusted stores me se chemical composition se milta product dhundo."""
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
                except Exception as e:
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


def get_brand(chemical, db_path=DB_PATH, use_cache=True, force_refresh=False):
    """
    Pehle local cache (brand_names.db) me dekho, na mile to scrape karo
    aur cache kar do. Returns: list of dict matches (best-first).
    """
    con = sqlite3.connect(db_path)
    _ensure_table(con)

    if use_cache and not force_refresh:
        cur = con.execute(
            "SELECT brand_name, company, product_url, matched_text "
            "FROM chemical_brand_map WHERE chemical = ? AND brand_name IS NOT NULL",
            (chemical,),
        )
        cached = cur.fetchall()
        if cached:
            con.close()
            return [
                {"chemical": chemical, "brand_name": r[0], "company": r[1],
                 "product_url": r[2], "matched_text": r[3], "source": "cache"}
                for r in cached
            ]

    results = search_brand(chemical)
    for r in results:
        con.execute(
            """INSERT INTO chemical_brand_map
               (chemical, brand_name, company, product_url, matched_text)
               VALUES (?,?,?,?,?)""",
            (chemical, r.get("brand_name"), r.get("company"),
             r.get("product_url"), r.get("matched_text")),
        )
    con.commit()
    con.close()
    return results


if __name__ == "__main__":
    import sys

    chem = sys.argv[1] if len(sys.argv) > 1 else "Carbendazim 50% WP"
    matches = get_brand(chem)

    if not matches:
        print(f"'{chem}' ka koi brand match nahi mila (internet/library issue ho sakta).")
    for m in matches:
        print(f"\nChemical : {m['chemical']}")
        print(f"Brand    : {m.get('brand_name')}")
        print(f"Company  : {m.get('company')}")
        print(f"URL      : {m.get('product_url')}")