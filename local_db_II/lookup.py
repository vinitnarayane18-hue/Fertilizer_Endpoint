#!/usr/bin/env python3
"""
lookup.py -- kisi bhi crop naam se fungicide chemical dhundo.
Usage:
    python3 lookup.py "Tomato"
    python3 lookup.py "tamatar"        # fuzzy/typo bhi chalega
    python3 lookup.py "Rice" "blast"   # crop + disease dono
"""
import sys
import sqlite3
import difflib
import re
from pathlib import Path

# Default DB path: use the `fungicide.db` in the same package directory (one level up)
DB_PATH = str(Path(__file__).resolve().parent / "fungicide.db")


def normalize(s):
    return re.sub(r"[^a-z]", "", s.lower())


def get_all_crops(con):
    cur = con.execute("SELECT DISTINCT crop FROM fungicide_uses WHERE crop IS NOT NULL")
    return [r[0] for r in cur.fetchall()]


def find_matching_crops(user_crop, all_crops, limit=5):
    uc = normalize(user_crop)

    # 1. exact (case-insensitive)
    exact = [c for c in all_crops if normalize(c) == uc]
    if exact:
        return exact

    # 2. substring match (dono taraf)
    sub = [c for c in all_crops if uc in normalize(c) or normalize(c) in uc]
    if sub:
        return sub

    # 3. fuzzy match (typo / spelling galat ho to bhi)
    close = difflib.get_close_matches(user_crop, all_crops, n=limit, cutoff=0.5)
    if close:
        return close

    return []


def search(crop_query, disease_query=None):
    con = sqlite3.connect(DB_PATH)
    all_crops = get_all_crops(con)
    matched_crops = find_matching_crops(crop_query, all_crops)

    if not matched_crops:
        print(f"'{crop_query}' se koi crop match nahi mila DB me.")
        print("Available crops me se kuch:", ", ".join(sorted(all_crops)[:15]), "...")
        con.close()
        return []

    placeholders = ",".join("?" * len(matched_crops))
    sql = f"""
        SELECT chemical, crop, disease, dose_ai, dose_formulation,
               dilution, waiting_period
        FROM fungicide_uses
        WHERE crop IN ({placeholders})
    """
    params = list(matched_crops)

    if disease_query:
        sql += " AND disease LIKE ?"
        params.append(f"%{disease_query}%")

    rows = con.execute(sql, params).fetchall()
    con.close()
    return rows


def pretty_print(rows):
    if not rows:
        print("Koi fungicide record nahi mila.")
        return
    print(f"\n{len(rows)} result(s) mile:\n")
    for chem, crop, disease, ai, form, dil, wait in rows:
        print(f"Crop      : {crop}")
        print(f"Disease   : {disease}")
        print(f"Chemical  : {chem}")
        print(f"Dose(a.i.): {ai}   Dose(Formulation): {form}")
        print(f"Dilution  : {dil}   Waiting period: {wait} days")
        print("-" * 50)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 lookup.py <crop> [disease]")
        sys.exit(1)

    crop_arg = sys.argv[1]
    disease_arg = sys.argv[2] if len(sys.argv) > 2 else None
    results = search(crop_arg, disease_arg)
    pretty_print(results)
