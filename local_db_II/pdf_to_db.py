#!/usr/bin/env python3
"""
Fungicide PDF -> SQLite DB converter.
Usage: python3 pdf_to_db.py input.pdf output.db
"""
import sys
import re
import sqlite3
import pdfplumber

FORM_END = re.compile(
    r'\b(SC|WP|WG|EC|GR|SL|FS|SP|DF|WS|OD|EW|SE|ZC|CS|DS|DP|FF|ES|DG|SG|OS|EO|SP|W\/W|W\/V|GEL|W\/W\s*FS)\b\.?\s*$',
    re.IGNORECASE,
)

def is_header(val):
    v = val.strip()
    if not v:
        return False
    if '%' in v and FORM_END.search(v):
        return True
    # combo chemicals w/ '+' almost always header lines
    if '+' in v and FORM_END.search(v):
        return True
    return False

def clean(cell):
    if cell is None:
        return None
    c = cell.replace('\n', ' ').strip()
    return c if c else None

def build_db(pdf_path, db_path):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("DROP TABLE IF EXISTS fungicide_uses")
    cur.execute("""
        CREATE TABLE fungicide_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section TEXT,        -- 'single' or 'combination'
            chemical TEXT,
            crop TEXT,
            disease TEXT,
            dose_ai TEXT,
            dose_formulation TEXT,
            dilution TEXT,
            waiting_period TEXT,
            page INTEGER
        )
    """)

    section = "single"
    chemical = None
    crop_carry = None
    last_row_id = None

    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if "Fungicides Combination Uses" in text or "2. Fungicides Combination" in text:
                section = "combination"

            table = page.extract_table()
            if not table:
                continue

            for row in table:
                cells = [clean(c) for c in row]
                vals = [c for c in cells if c]

                if len(vals) == 0:
                    continue

                if len(vals) == 1:
                    if is_header(vals[0]):
                        chemical = vals[0]
                        crop_carry = None
                        last_row_id = None
                    else:
                        # wrapped continuation text -> append to last disease field
                        if last_row_id:
                            cur.execute(
                                "UPDATE fungicide_uses SET disease = disease || ' ' || ? WHERE id = ?",
                                (vals[0], last_row_id),
                            )
                    continue

                # skip repeated table-header rows that leak in as data
                joined_lower = " ".join(vals).lower()
                if "common name" in joined_lower and "disease" in joined_lower:
                    continue
                if vals[0].strip().lower() in ("crop", "common name of the disease"):
                    continue

                if len(vals) >= 6:
                    crop, disease, ai, form, dil, wait = vals[:6]
                    crop_carry = crop
                elif len(vals) == 5:
                    disease, ai, form, dil, wait = vals
                    crop = crop_carry
                else:
                    # unexpected shape -> skip / dump raw
                    continue

                cur.execute(
                    """INSERT INTO fungicide_uses
                       (section, chemical, crop, disease, dose_ai, dose_formulation,
                        dilution, waiting_period, page)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (section, chemical, crop, disease, ai, form, dil, wait, pno),
                )
                last_row_id = cur.lastrowid

    con.commit()
    cur.execute("SELECT COUNT(*) FROM fungicide_uses")
    print(f"Rows inserted: {cur.fetchone()[0]}")
    con.close()

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 pdf_to_db.py input.pdf output.db")
        sys.exit(1)
    build_db(sys.argv[1], sys.argv[2])
