#!/usr/bin/env python3
"""
gemini_advisor.py -- LAYER 3
Gemini AI ka use karke Layer 1 (official DB) + Layer 2 (scraped trusted
sources) ke data ko samajhne layak advice, precautions aur "kya avoid
kare" me convert karta hai.

IMPORTANT: Gemini khud se dose invent nahi karega -- prompt me clearly
bola gaya hai ki wahi dose bataye jo Layer 1/2 me diya gaya hai, aur
agar data hi nahi mila to seedha bol de "certified dealer / KVK se
salah lo", galat guess na kare.

Requirements:
    pip install google-generativeai
    set GEMINI_API_KEY environment variable (apni actual valid key se)
"""

import os

try:
    import google.generativeai as genai
except ImportError:
    genai = None


def _configure():
    if genai is None:
        raise ImportError(
            "google-generativeai library missing. Install kar: "
            "pip install google-generativeai"
        )
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable set nahi hai. "
            "PowerShell me: $env:GEMINI_API_KEY='tumhari-key'"
        )
    genai.configure(api_key=api_key)


def _format_layer1(rows):
    if not rows:
        return "Koi official (CIB&RC registered) record nahi mila."
    lines = []
    for r in rows:
        lines.append(
            f"- {r.get('chemical')}: dose(a.i.)={r.get('dose_ai')}, "
            f"dose(formulation)={r.get('dose_formulation')}, "
            f"dilution={r.get('dilution')}, "
            f"waiting period={r.get('waiting_period')} din"
        )
    return "\n".join(lines)


def _format_layer2(rows):
    if not rows:
        return "Koi additional trusted-source jaankari nahi mili."
    lines = []
    for r in rows:
        snippet = (r.get("content") or r.get("snippet") or "")[:400]
        lines.append(f"- [{r.get('source_name')}] ({r.get('url')}): {snippet}")
    return "\n".join(lines)


def build_prompt(crop, disease, layer1_rows, layer2_rows):
    return f"""Tum ek anubhavi krishi (agriculture) advisor ho jo Indian
farmers ko unki bhasha me samjha ke madad karta hai.

Neeche diya gaya data do trusted sources se hai:
1. Layer 1 = Government of India CIB&RC ka official registered fungicide
   database (Insecticides Act 1968 ke tahat).
2. Layer 2 = Maharashtra ke krishi vidyapeeth / ICAR / govt portals se
   scrape kiya gaya supporting data.

Crop: {crop}
Disease: {disease or 'specify nahi kiya gaya'}

=== Layer 1: Official registered fungicide data ===
{_format_layer1(layer1_rows)}

=== Layer 2: Trusted sources (Maharashtra krishi vidyapeeth / ICAR / Govt) ===
{_format_layer2(layer2_rows)}

Ab niche diye gaye format me jawab do (Hindi-English mix, simple farmer
ki bhasha, taaki koi bhi samajh sake):

**Recommendation:** Kaunsa fungicide/chemical sabse suitable hai aur kyu.

**Dosage:** SIRF upar diye gaye Layer 1/2 data me se hi dose bata --
khud se koi number mat banao. Agar data nahi mila to saaf likho:
"Official dose data nahi mila -- kripya apne nazdiki Krishi Vigyan
Kendra ya certified pesticide dealer se salah le."

**Precautions:** Spray kaise aur kab kare (mausam, timing, PPE
kit/mask-gloves use karna), taaki asar sahi ho.

**Kya Avoid Kare:** Crop ko nuksan na ho iske liye kya galtiyan nahi
karni -- jaise overdose, waiting period ignore karna, do incompatible
chemicals mix karna, phool/flowering stage me spray karna, etc.
"""


def get_gemini_advice(crop, disease, layer1_rows, layer2_rows, model_name="gemini-1.5-flash"):
    """
    layer1_rows: list of dicts jaise
        {"chemical":..., "dose_ai":..., "dose_formulation":..., "dilution":..., "waiting_period":...}
    layer2_rows: list of dicts jaise scraper.py se aata hai
        {"source_name":..., "url":..., "snippet":..., "content":...}
    """
    try:
        _configure()
    except Exception as e:
        return f"[Gemini setup error: {e}]"

    prompt = build_prompt(crop, disease, layer1_rows, layer2_rows)
    try:
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return (
            f"[Gemini API call fail hui: {e}]\n"
            "Fallback: sirf Layer 1 aur Layer 2 ka raw data upar dekh lo."
        )


if __name__ == "__main__":
    import sys

    crop_arg = sys.argv[1] if len(sys.argv) > 1 else "Tomato"
    disease_arg = sys.argv[2] if len(sys.argv) > 2 else "Early blight"

    # quick manual test data (real use me orchestrator.py Layer1/2 se bharega)
    demo_layer1 = [{
        "chemical": "Captan 50% WP", "dose_ai": "1250gm", "dose_formulation": "2.5kg",
        "dilution": "750-1000", "waiting_period": "-",
    }]
    demo_layer2 = []

    print(get_gemini_advice(crop_arg, disease_arg, demo_layer1, demo_layer2))
