#!/usr/bin/env python3
"""
orchestrator.py -- teeno layers ko ek saath manage karta hai.

    Layer 1 -> lookup.py          (local SQLite DB, CIB&RC registered data)
    Layer 2 -> scraper.py         (Maharashtra/govt trusted sites se scraping)
    Layer 3 -> gemini_advisor.py  (Gemini AI se final samjhaya hua advice)

Usage:
    python3 orchestrator.py "Tomato" "Early blight"
    python3 orchestrator.py "Tomato" "Early blight" --no-scraper
    python3 orchestrator.py "Tomato" "Early blight" --no-gemini
"""

import json
import sys

import lookup
import scraper
import brand_mapper
import gemini_advisor

LAYER1_COLUMNS = [
    "chemical", "crop", "disease", "dose_ai",
    "dose_formulation", "dilution", "waiting_period",
]


def _rows_to_dicts(rows):
    return [dict(zip(LAYER1_COLUMNS, r)) for r in rows]


class FertilizerAdvisor:
    def __init__(self, db_path="fungicide.db"):
        self.db_path = db_path

    def get_recommendation(self, crop, disease=None, use_scraper=True,
                            use_gemini=True, use_brand_names=True):
        # ---------- Layer 1: local DB ----------
        print(f"[Layer 1] Local DB me '{crop}' dhundh rahe...")
        raw_rows = lookup.search(crop, disease)
        layer1 = _rows_to_dicts(raw_rows)
        print(f"[Layer 1] {len(layer1)} record mile.\n")

        # ---------- Layer 1.5: chemical -> real brand/product name ----------
        if use_brand_names:
            print("[Layer 1.5] Chemical naam ko market brand naam me convert kar rahe...")
            seen = {}
            for row in layer1:
                chem = row.get("chemical")
                if not chem:
                    continue
                if chem not in seen:
                    try:
                        matches = brand_mapper.get_brand(chem, db_path="brand_names.db")
                        seen[chem] = matches[0] if matches else None
                    except Exception as e:
                        print(f"[Layer 1.5] '{chem}' ke liye brand dhundhna fail hua: {e}")
                        seen[chem] = None
                m = seen[chem]
                row["brand_name"] = m.get("brand_name") if m else None
                row["brand_company"] = m.get("company") if m else None
            print("[Layer 1.5] done.\n")

        # ---------- Layer 2: trusted-site scraping ----------
        layer2 = []
        if use_scraper:
            print("[Layer 2] Trusted govt/Maharashtra krishi sites se scrape kar rahe...")
            try:
                layer2 = scraper.scrape_and_cache(crop, disease, db_path=self.db_path)
                print(f"[Layer 2] {len(layer2)} source mile.\n")
            except Exception as e:
                print(f"[Layer 2] scraping fail hui (internet/library issue ho sakta): {e}\n")
        else:
            print("[Layer 2] skip kiya gaya.\n")

        # ---------- Layer 3: Gemini synthesis ----------
        advice = None
        if use_gemini:
            print("[Layer 3] Gemini se final samjhi hui advice generate ho rahi...")
            advice = gemini_advisor.get_gemini_advice(crop, disease, layer1, layer2)
            print("[Layer 3] done.\n")
        else:
            print("[Layer 3] skip kiya gaya.\n")

        return {
            "crop": crop,
            "disease": disease,
            "layer1_official_data": layer1,
            "layer2_scraped_sources": layer2,
            "layer3_gemini_advice": advice,
        }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python3 orchestrator.py "<crop>" ["<disease>"] [--no-scraper] [--no-gemini]')
        sys.exit(1)

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    crop_arg = args[0]
    disease_arg = args[1] if len(args) > 1 else None
    use_scraper = "--no-scraper" not in flags
    use_gemini = "--no-gemini" not in flags

    advisor = FertilizerAdvisor()
    result = advisor.get_recommendation(
        crop_arg, disease_arg, use_scraper=use_scraper, use_gemini=use_gemini
    )

    print("=" * 60)
    print(json.dumps(result, indent=2, ensure_ascii=False))
