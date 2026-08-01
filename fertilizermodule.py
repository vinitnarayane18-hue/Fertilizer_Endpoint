"""
fertilizermodule.py
=====================================
Hybrid Master query engine for the Farmyworth pest protection endpoint.

Flow:
  1. Orchestrator sends a standardized JSON payload (crop, pests, symptom, category_intent).
  2. Engine normalizes the crop. If no pests/symptoms, it triggers the PGR Bypass.
  3. Layer 1 (Fast Path): Normalizes and resolves known pest names instantly via dicts/aliases.
  4. Layer 2 (Gemini Fallback): If pests are unknown OR only a symptom is provided, 
     it dynamically calls Gemini to map the symptom/slang to exact DB pest keys.
  5. Queries DB for matching chemicals (insecticides, bio-pesticides, fungicides, PGRs).
  6. Enriches matches with brand info.
  7. Returns a clean, structured payload for the WhatsApp LLM to format.
"""

import sqlite3
import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional, List, Tuple, Set, Dict
import sys
import os
# --- SAFELY IMPORT BRAND MAPPER ---
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

try:
    from brand_mapper import get_brand
    HAS_BRAND_MAPPER = True
except Exception as e:
    # This will now print the exact missing module or SAC error!
    print(f"[WARNING] Scraper Import Failed: {e}")
    HAS_BRAND_MAPPER = False
# ----------------------------------

# ── CONFIG ────────────────────────────────────────────────────────────────────
DB_PATH = "farmyworth_agri_v1fin.db"

# Similarity threshold for fuzzy crop/pest matching (0-100)
FUZZY_THRESHOLD = 72

# Max results per category returned to LLM
MAX_CHEMICALS_PER_CATEGORY = 5
# --- SAFELY IMPORT BRAND MAPPER ---

# ----------------------------------
# ── NORMALISATION ─────────────────────────────────────────────────────────────

def normalize_to_key(text: str) -> str:
    """
    Convert any human input to the snake_case key format the DB uses.
    """
    if not text:
        return ""
    text = str(text)
    text = unicodedata.normalize("NFKC", text)
    text = text.lower().strip()
    text = re.sub(r'\s*\([^)]*\)', ' ', text)
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    text = re.sub(r'\s+', '_', text.strip())
    return text

def normalize_crop(crop: str) -> str:
    """Normalize crop input. Handles common aliases."""
    if not crop:
        return ""
    crop = crop.strip().lower()

    CROP_ALIASES = {
        "tamatar":     "tomato", "tamata":      "tomato",
        "bhindi":      "bhindi", "okra":        "bhindi", "lady finger": "bhindi",
        "kapas":       "cotton", "gahu":        "wheat",  "gehun":       "wheat",
        "tandul":      "rice",   "paddy":       "rice",   "bhat":        "rice",
        "makka":       "maize",  "corn":        "maize",
        "soyabean":    "soybean","soya":        "soybean",
        "mirchi":      "chilli", "chilly":      "chilli", "pepper":      "chilli",
        "kanda":       "onion",  "pyaaz":       "onion",
        "aalu":        "potato", "batata":      "potato", "bataata":     "potato",
        "groundnut":   "groundnut", "shengdana": "groundnut",
        "tuvar":       "pigeonpea", "arhar":     "pigeonpea", "tur":     "pigeonpea",
        "harbhara":    "chickpea", "gram":       "chickpea", "chana":    "chickpea",
        "urd":         "black_gram",
        "moong":       "green_gram", "mung":     "green_gram",
        "sunflower":   "sunflower", "safflower": "safflower",
        "sugarcane":   "sugarcane", "verdi":     "sugarcane", "oos":     "sugarcane",
        "द्राक्ष":     "grapes", "draksha":     "grapes", "grape":      "grapes",
        "pomegranate": "pomegranate", "dalimb":  "pomegranate",
        "orange":      "citrus",
        "mango":       "mango", "amba":         "mango",
        "banana":      "banana", "kela":        "banana",
        # Marathi
        "तांदूळ":      "rice", "तांदुळ":      "rice", "तूर":        "pigeonpea",
        "हरभरा":       "chickpea", "सोयाबीन":    "soybean", "कापूस":       "cotton",
        "गहू":         "wheat", "मका":         "maize", "टोमॅटो":      "tomato",
        "मिरची":       "chilli", "कांदा":       "onion", "बटाटा":       "potato",
        "वांगी":       "brinjal", "भेंडी":       "bhindi", "ऊस":          "sugarcane",
        "आंबा":        "mango", "केळी":        "banana", "द्राक्षे":  "grapes"
    }

    if crop in CROP_ALIASES:
        return CROP_ALIASES[crop]

    return normalize_to_key(crop)

def resolve_crop_key(crop_input: str, conn: sqlite3.Connection) -> Tuple[Optional[str], str]:
    cur = conn.cursor()
    normalized = normalize_crop(crop_input)

    # Exact
    cur.execute("SELECT COUNT(*) FROM crop_protection WHERE crop_normalized = ?", (normalized,))
    if cur.fetchone()[0] > 0:
        return normalized, crop_input

    # LIKE
    cur.execute("""
        SELECT DISTINCT crop_normalized FROM crop_protection
        WHERE crop_normalized LIKE ? ORDER BY LENGTH(crop_normalized) ASC LIMIT 5
    """, (f'{normalized}%',))
    hits = [r[0] for r in cur.fetchall()]
    if hits:
        return hits[0], crop_input

    # Fuzzy
    cur.execute("SELECT DISTINCT crop_normalized FROM crop_protection")
    all_crops = [r[0] for r in cur.fetchall()]
    scores = [(SequenceMatcher(None, normalized, c).ratio() * 100, c) for c in all_crops]
    scores.sort(reverse=True)
    if scores and scores[0][0] >= FUZZY_THRESHOLD:
        return scores[0][1], crop_input

    return None, crop_input

# ── LAYER 1: FAST-PATH PEST RESOLUTION ────────────────────────────────────────

def resolve_pest_keys_layer1(pest_input: str, conn: sqlite3.Connection) -> List[str]:
    """
    LAYER 1: Zero-latency dictionary, alias, and exact-match checking.
    If this returns [], it triggers Layer 2 (Gemini).
    """
    cur = conn.cursor()
    pest_input_clean = pest_input.strip()

    # Step 0: Inline Dict
    # Step 0: Inline Dict (Master Maharashtra Agronomy Map)
    INLINE_PEST_MAP = {
        # ── SUCKING PESTS & GENERAL INSECTS (रस शोषणारी आणि इतर प्रमुख कीड) ──
        "मावा": "aphids", "mava": "aphids", "माहू": "aphids", "mahu": "aphids", "चिकटा": "aphids",
        "तुडतुडे": "jassids", "tudtude": "jassids", "hoppers": "jassids", "leafhoppers": "jassids",
        "फुलकिडे": "thrips", "फुलकिडा": "thrips", "fulkide": "thrips", "thrip": "thrips", "उन्हाळी कीड": "thrips",
        "पांढरी माशी": "whitefly", "pandhari mashi": "whitefly", "सफेद मक्खी": "whitefly", "safed makkhi": "whitefly",
        "पिठ्या ढेकूण": "mealy_bugs", "pithya dhekun": "mealy_bugs", "mealy bug": "mealy_bugs", "ढेकूण": "mealy_bugs",
        "लाल कोळी": "mites", "कोळी": "mites", "lal koli": "mites", "koli": "mites", "red mite": "mites", "मकडी": "mites",
        "खवले कीड": "scale_insects", "khavle kid": "scale_insects", "scales": "scale_insects",

        # ── BORERS, WORMS & CATERPILLARS (अळी, खोडकिडे आणि छिद्र पाडणाऱ्या अळ्या) ──
        "खोडकिडा": "stem_borer", "खोडकिडे": "stem_borer", "khodkida": "stem_borer", "तना छेदक": "stem_borer", "खोदणारा कीडा": "stem_borer",
        "शेंगा पोखरणारी अळी": "pod_borer", "shenga pokharnari ali": "pod_borer", "फल्ली छेदक": "pod_borer", "घाटे अळी": "pod_borer",
        "फळ पोखरणारी अळी": "fruit_borer", "fal pokharnari ali": "fruit_borer", "फल छेदक": "fruit_borer",
        "बोंडअळी": "bollworm", "bondali": "bollworm", "american bollworm": "american_bollworm",
        "शेंदरी बोंडअळी": "pink_bollworm", "shendari bondali": "pink_bollworm", "गुलाबी बोंडअळी": "pink_bollworm", "gulabi bondali": "pink_bollworm",
        "लष्करी अळी": "fall_armyworm", "lashkari ali": "fall_armyworm", "armyworm": "fall_armyworm", "सैनिक अळी": "fall_armyworm", "sainik ali": "fall_armyworm",
        "उंट अळी": "semilooper", "unt ali": "semilooper", "सेमीलूपर": "semilooper",
        "पाने गुंडाळणारी अळी": "leaf_folder", "pane gundalnari ali": "leaf_folder", "पत्ता लपेटक": "leaf_folder", "पान गुंडाळणारा": "leaf_folder",
        "पाने खाणारी अळी": "caterpillar", "pane khanari ali": "caterpillar", "spodoptera": "spodoptera", "tobacco caterpillar": "spodoptera", "सुरळीतील अळी": "caterpillar",
        "नागअळी": "leaf_miner", "nag ali": "leaf_miner", "नाग अळी": "leaf_miner", "leaf miner": "leaf_miner", "पान पोھरणारी अळी": "leaf_miner",
        
        # ── CROP-SPECIFIC REGIONAL PESTS (विशेष पिकांमधील स्थानिक कीड - सोयाबीन, कापूस इ.) ──
        "चक्री भुंगा": "girdle_beetle", "chakri bhunga": "girdle_beetle", "girdle beetle": "girdle_beetle", "चक्र भुंगा": "girdle_beetle",
        "खोडातील माशी": "stem_fly", "khodatil mashi": "stem_fly", "stem fly": "stem_fly", "खोड माशी": "stem_fly",
        "तंबाखूची अळी": "spodoptera", "tambakhuchi ali": "spodoptera",

        # ── SOIL & ROOT PESTS (जमिनीतील आणि मुळांवरील कीड) ──
        "हुमणी": "white_grub", "humni": "white_grub", "white grub": "white_grub", "हुमणी अळी": "white_grub",
        "वाळवी": "termite", "valvi": "termite", "termites": "termite", "दीमक": "termite", "उधई": "termite",
        "सूत्रकृमी": "nematodes", "sutrakrumi": "nematodes", "nematode": "nematodes", "गाठी होणारा रोग": "nematodes",
        "गोगलगाय": "snails", "gogalgay": "snails", "slugs": "snails",
        "टोळ": "locust", "tol": "locust", "grasshopper": "locust", "नाकतोडा": "locust", "naktoda": "locust",

        # ── FUNGAL, BACTERIAL & VIRAL DISEASES (बुरशीजन्य, जिवाणूजन्य आणि विषाणूजन्य रोग) ──
        "करपा": "anthracnose", "karpa": "anthracnose", "blight": "blight", "पानावरील ठिपके": "anthracnose",
        "भुरी": "powdery_mildew", "bhuri": "powdery_mildew", "powdery mildew": "powdery_mildew", "पांढरी भुरी": "powdery_mildew",
        "केवडा": "downy_mildew", "kevda": "downy_mildew", "downy mildew": "downy_mildew", "केवडा रोग": "downy_mildew",
        "तांबेरा": "rust", "tambera": "rust", "rust": "rust", "हळदद्या": "rust", "haldya": "rust", "गेरवा": "rust",
        "मर": "wilt", "mar": "wilt", "wilt": "wilt", "उकळणे": "wilt", "अचानक झाड सुकणे": "wilt", "मानमोडी": "wilt",
        "सड": "root_rot", "sad": "root_rot", "root rot": "root_rot", "खोडाची सड": "stem_rot", "मूळ सडणे": "root_rot",
        "टिक्का": "tikka_disease", "tikka": "tikka_disease", "leaf spot": "leaf_spot", "ठिपके": "leaf_spot", "पानगळ": "tikka_disease",
        "मोझॅक": "mosaic_virus", "mosaic": "mosaic_virus", "ymv": "mosaic_virus", "पिवळा मोझॅक": "mosaic_virus", "पान आखडणे": "mosaic_virus", "चोंबडा": "mosaic_virus",
        "फायटोफ्थोरा": "phytophthora_rot", "phytophthora": "phytophthora_rot"
    }
    if pest_input_clean in INLINE_PEST_MAP:
        mapped_key = INLINE_PEST_MAP[pest_input_clean]
        cur.execute("""
            SELECT DISTINCT pest_normalized FROM crop_protection
            WHERE pest_normalized = ? OR pest_normalized LIKE ?
        """, (mapped_key, f'{mapped_key}_%'))
        keys = [r[0] for r in cur.fetchall()]
        if keys: return keys

    # Step 1: Alias Table
    cur.execute("""
        SELECT pest_key FROM pest_aliases WHERE LOWER(alias_term) = LOWER(?)
    """, (pest_input_clean,))
    alias_hits = [r[0] for r in cur.fetchall()]
    if alias_hits:
        keys = []
        for alias_key in alias_hits:
            cur.execute("""
                SELECT DISTINCT pest_normalized FROM crop_protection
                WHERE pest_normalized = ? OR pest_normalized LIKE ?
            """, (alias_key, f'{alias_key}_%'))
            keys.extend([r[0] for r in cur.fetchall()])
        if keys: return list(dict.fromkeys(keys))

    # Step 2: Exact Normalization
    normalized = normalize_to_key(pest_input_clean)
    cur.execute("SELECT COUNT(*) FROM crop_protection WHERE pest_normalized = ?", (normalized,))
    if cur.fetchone()[0] > 0:
        cur.execute("""
            SELECT DISTINCT pest_normalized FROM crop_protection
            WHERE pest_normalized = ? OR pest_normalized LIKE ?
        """, (normalized, f'{normalized}_%'))
        return [r[0] for r in cur.fetchall()]

    # Returns empty if fast-path fails -> triggers Gemini
    return []

# ── LAYER 2: GEMINI SYMPTOM/PEST MAPPER ───────────────────────────────────────

def get_all_pests_for_crop(crop_key: str, conn: sqlite3.Connection) -> List[str]:
    """Helper: Pulls the strict list of allowed pests for Gemini to choose from."""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT pest_normalized FROM crop_protection WHERE crop_normalized = ?", (crop_key,))
    return [r[0] for r in cur.fetchall()]



import os
from google import genai
from google.genai import types

# ── GEMINI CONFIGURATION ──────────────────────────────────────────────────────
# Replace with your actual API key or set it in your environment variables
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") 
client = genai.Client(api_key=GEMINI_API_KEY)


def call_gemini_pest_mapper(crop_key: str, context_text: str, allowed_pests: List[str]) -> List[str]:
    """
    LAYER 2: Live Google GenAI SDK Implementation (New SDK).
    Takes a symptom ("red curling leaves") or an unrecognized slang pest,
    and forces the LLM to return the closest matches from the allowed_pests list.
    """
    print(f"[API CALL] Gemini mapping '{context_text}' for {crop_key}...")
    
    # If there are no allowed pests for this crop in the DB, skip the API call
    if not allowed_pests:
        return []

    prompt = f"""
    You are an expert Maharashtrian agronomist.
    The farmer is growing: {crop_key}
    The farmer described this issue/pest: "{context_text}"

    Here is the strict list of available database keys for this crop:
    {allowed_pests}

    Task: Map the farmer's issue to the most accurate database keys from the list above.
    - If it's a disease, match the disease key.
    - If it's an insect, match the insect key.
    - Return a JSON array of strings containing a maximum of 2 keys.
    - Return ONLY valid keys exactly as they appear in the array provided.
    - If nothing matches, return an empty array [].
    """

    try:
        # Using the new google-genai client interface
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        # Parse the JSON response returned by Gemini
        matched_keys = json.loads(response.text)
        
        # Security/Validation: Ensure Gemini didn't hallucinate keys not in your DB
        valid_keys = [k for k in matched_keys if k in allowed_pests]
        
        print(f"[API SUCCESS] Gemini mapped to: {valid_keys}")
        return valid_keys
        
    except json.JSONDecodeError:
        print("[GEMINI ERROR] Failed to parse JSON from response.")
        return []
    except Exception as e:
        print(f"[GEMINI ERROR] API call failed: {e}")
        return []
# ── CORE QUERIES ──────────────────────────────────────────────────────────────

def query_chemicals(crop_key: str, pest_keys: List[str], conn: sqlite3.Connection) -> List[tuple]:
    """Query chemicals for specific pests."""
    if not pest_keys:
        return []
    cur = conn.cursor()
    placeholders = " OR ".join(["cp.pest_normalized = ? OR cp.pest_normalized LIKE ?" for _ in pest_keys])
    params = []
    for pk in pest_keys:
        params.extend([pk, f'{pk}_%'])

    query = f"""
        SELECT cp.id, cp.crop_normalized, cp.pest_normalized, cp.chemical_key,
               cp.chemical_name, cp.category, cp.ai_dose, cp.formulation_dose,
               cp.water_dilution_l, cp.waiting_period_days, cp.dose_application_method,
               cp.is_combination, be.brand_names, be.companies
        FROM crop_protection cp
        LEFT JOIN brand_enrichment be ON cp.chemical_key = be.chemical_key
        WHERE cp.crop_normalized = ? AND ({placeholders})
        ORDER BY cp.category, cp.chemical_name
    """
    cur.execute(query, [crop_key] + params)
    return cur.fetchall()

def query_pgrs(crop_key: str, conn: sqlite3.Connection) -> List[tuple]:
    """Bypass query used when farmer asks for growth/yield boosters."""
    cur = conn.cursor()
    query = """
        SELECT cp.id, cp.crop_normalized, cp.pest_normalized, cp.chemical_key,
               cp.chemical_name, cp.category, cp.ai_dose, cp.formulation_dose,
               cp.water_dilution_l, cp.waiting_period_days, cp.dose_application_method,
               cp.is_combination, be.brand_names, be.companies
        FROM crop_protection cp
        LEFT JOIN brand_enrichment be ON cp.chemical_key = be.chemical_key
        WHERE cp.crop_normalized = ? AND (cp.category LIKE '%pgr%' OR cp.category LIKE '%growth%')
        ORDER BY cp.chemical_name
    """
    cur.execute(query, [crop_key])
    return cur.fetchall()

# ── UTILS ─────────────────────────────────────────────────────────────────────

import re
from typing import List, Optional, Set

def _simplify_chemical_for_search(chem_name: str) -> str:
    """
    Cleans strict CIBRC chemical strings so search engines can actually find them.
    Example: 'Carbendazim 12%+ Mancozeb 63% WP' -> 'Carbendazim Mancozeb'
    """
    # 1. Remove everything inside parentheses
    text = re.sub(r'\(.*?\)', ' ', chem_name)
    # 2. Remove numbers, decimals, and percentage signs
    text = re.sub(r'\d+(\.\d+)?\s*%?', ' ', text)
    # 3. Remove formulation codes 
    text = re.sub(r'(?i)\b(w/w|w/v|sc|wg|wp|ec|sl|sp|od|fs|as|lf|gm|ml|kg|wdg|gr|ew)\b', ' ', text)
    # 4. Remove all non-alphabetical characters (like '+', '-', '&')
    text = re.sub(r'[^a-zA-Z\s]', ' ', text)
    # 5. Collapse extra spaces
    return ' '.join(text.split()).strip()

def fetch_live_brands_from_scraper(rows: list, overlap_keys: set = None, max_live_scrapes: int = 4) -> dict:
    """
    1. Prioritizes 'Overlap' (Best Match) chemicals.
    2. Cleans the string FIRST to guarantee high SEO match rates.
    3. Fetches from cache or live web using the CLEAN string.
    4. Limits live searches to prevent IP Bans.
    """
    try:
        from brand_mapper import get_brand
    except ImportError:
        print("[WARNING] brand_mapper.py not found. Skipping live brands.")
        return {}

    overlap_keys = overlap_keys or set()
    seen = set()
    overlap_chems = []
    regular_chems = []

    # 1. Deduplicate & Priority Sort (Best Matches go first)
    for row in rows:
        if row and len(row) > 4 and row[4]:
            chem_key = row[3]
            raw_chem_name = str(row[4]).strip()
            if raw_chem_name not in seen:
                seen.add(raw_chem_name)
                if chem_key in overlap_keys:
                    overlap_chems.append(raw_chem_name)
                else:
                    regular_chems.append(raw_chem_name)

    target_chems = (overlap_chems + regular_chems)
    live_brands = {}
    live_scrapes_done = 0

    for raw_chem in target_chems:
        brands = set()
        companies = set()
        
        try:
            # --- FIX: Clean the query BEFORE we pass it to the scraper! ---
            clean_query = _simplify_chemical_for_search(raw_chem)
            if len(clean_query) < 3:
                clean_query = raw_chem # Fallback if stripped completely
                
            # Let brand_mapper check cache OR scrape using the CLEAN, SEO-friendly query
            matches = get_brand(clean_query, use_cache=True)
            
            is_cached = False
            for m in matches:
                if m.get("brand_name"): brands.add(m.get("brand_name").strip())
                if m.get("company"): companies.add(m.get("company").strip())
                if m.get("source") == "cache": is_cached = True

            # Store the data under the RAW CIBRC chemical name so the formatter maps it correctly!
            live_brands[raw_chem] = {
                "brands": list(brands),
                "companies": list(companies)
            }

            # Only increment network scrapes if it actually hit the internet
            if not is_cached:
                live_scrapes_done += 1

            # Stop live scraping if we hit the limit to prevent 429 IP Bans
            if live_scrapes_done >= max_live_scrapes:
                break

        except Exception as e:
            print(f"[SCRAPER ERROR] Failed for {raw_chem}: {e}")
            live_brands[raw_chem] = {"brands": [], "companies": []}
            live_scrapes_done += 1
            if live_scrapes_done >= max_live_scrapes:
                break

    return live_brands

def _clean_field(val) -> Optional[str]:
    if val is None: return None
    s = str(val).strip()
    if s in ('', '-', '--', '---', 'Not specified', 'Not Specified', 'Not Required', 'NA', 'N/A', 'Nil', 'null', '0'):
        return None
    return s

def _normalize_waiting(val) -> Optional[str]:
    s = _clean_field(val)
    if not s: return None
    if re.match(r'^\d{1,3}$', s):
        n = int(s)
        return None if n == 0 else f"{n} days"
    m = re.match(r'^(\d+)\s*days?$', s, re.IGNORECASE)
    if m: return f"{int(m.group(1))} days"
    m = re.match(r'^(\d+)\s*weeks?$', s, re.IGNORECASE)
    if m: return f"{int(m.group(1)) * 7} days"
    if 'seed' in s.lower(): return "Not applicable (seed treatment)"
    m = re.match(r'^(\d+)-(\d+)$', s)
    if m: return f"{m.group(1)}–{m.group(2)} days"
    return s if re.search(r'\d', s) else None

def find_overlap_chemicals(crop_key: str, all_pest_keys: List[List[str]], conn: sqlite3.Connection) -> Set[str]:
    if len(all_pest_keys) <= 1: return set()
    cur = conn.cursor()
    sets = []
    for pest_keys in all_pest_keys:
        if not pest_keys: continue
        placeholders = " OR ".join(["pest_normalized = ? OR pest_normalized LIKE ?" for _ in pest_keys])
        params = []
        for pk in pest_keys: params.extend([pk, f'{pk}_%'])
        cur.execute(f"SELECT DISTINCT chemical_key FROM crop_protection WHERE crop_normalized = ? AND ({placeholders})", [crop_key] + params)
        sets.append(set(r[0] for r in cur.fetchall()))
    if not sets: return set()
    overlap = sets[0]
    for s in sets[1:]: overlap = overlap.intersection(s)
    return overlap

# ── MAIN ORCHESTRATOR ENDPOINT ────────────────────────────────────────────────

def get_protection_options(payload: dict, db_path: str = DB_PATH) -> dict:
    """
    Main entry point for the API.
    Expects payload: {"crop": str, "pest": list[str] | None, "symptom": str | None, "category_intent": str | None, "missing_info": bool}
    """
    if payload.get("missing_info"):
        return {"status": "missing_info", "message": "Trigger flow to ask farmer for crop details."}

    crop_input = payload.get("crop", "")
    pests = payload.get("pest") or []
    if isinstance(pests, str): pests = [pests]
    symptom = payload.get("symptom")
    category_intent = payload.get("category_intent")

    conn = sqlite3.connect(db_path)

    try:
        # 1. Resolve Crop
        crop_key, crop_display = resolve_crop_key(crop_input, conn)
        if not crop_key:
            return {"status": "crop_not_found", "message": f"Crop '{crop_input}' not found in database."}

        # 2. PGR / Fertilizer Bypass Check
        is_pgr_request = (category_intent == "PGR") or (not pests and not symptom)
        
        if is_pgr_request:
            rows = query_pgrs(crop_key, conn)
            # Pass an empty set for PGRs
            live_brands = fetch_live_brands_from_scraper(rows, set())
            return _format_payload_response(rows, crop_key, crop_display, ["Growth Boosters"], set(), live_brands=live_brands, is_pgr=True)

        # 3. Resolve Pests (Layer 1 Fast Path)
        all_resolved_pest_keys = []
        matched_pest_labels = []
        mapped_from_symptom = False

        allowed_pests_for_crop = get_all_pests_for_crop(crop_key, conn)

        for pest_inp in pests:
            keys = resolve_pest_keys_layer1(pest_inp, conn)
            if keys:
                all_resolved_pest_keys.append(keys)
                matched_pest_labels.append(pest_inp)
            else:
                # Layer 2 Fallback for unknown slang
                gemini_keys = call_gemini_pest_mapper(crop_key, pest_inp, allowed_pests_for_crop)
                if gemini_keys:
                    all_resolved_pest_keys.append(gemini_keys)
                    matched_pest_labels.append(f"{pest_inp} (AI Mapped)")

        # 4. Symptom-to-Pest Translation (Layer 2)
        if not all_resolved_pest_keys and symptom:
            gemini_keys = call_gemini_pest_mapper(crop_key, symptom, allowed_pests_for_crop)
            if gemini_keys:
                all_resolved_pest_keys.append(gemini_keys)
                matched_pest_labels.append("Symptom Translated")
                mapped_from_symptom = True

        # Flatten for querying
        flat_pest_keys = [pk for group in all_resolved_pest_keys for pk in group]

        if not flat_pest_keys:
            return {"status": "no_match", "message": "Could not map pests or symptoms to known database keys."}

        # 5. Database Execution
        
        rows = query_chemicals(crop_key, flat_pest_keys, conn)
        overlap_keys = find_overlap_chemicals(crop_key, all_resolved_pest_keys, conn)

        # Pass the calculated overlap_keys to prioritize best matches!
        live_brands = fetch_live_brands_from_scraper(rows, overlap_keys)

        return _format_payload_response(rows, crop_key, crop_display, matched_pest_labels, overlap_keys, live_brands=live_brands, mapped_from_symptom=mapped_from_symptom)

    finally:
        conn.close()

def _format_payload_response(rows, crop_key, crop_display, matched_pest_labels, overlap_keys, live_brands=None, is_pgr=False, mapped_from_symptom=False) -> dict:
    """Helper to process raw SQL rows into the nested JSON response format."""
    if not rows:
        return {"status": "no_match", "message": "No registered chemicals found for the criteria."}

    seen_chem_keys = {}
    for row in rows:
        (id_, crop_norm, pest_norm, chem_key, chem_name, category,
         ai_dose, form_dose, water, waiting, method, is_combo,
         brand_json, company_json) = row

        # --- NEW: Inject scraped brands if they exist, else fallback to empty master DB ---
        if live_brands and chem_name in live_brands and (live_brands[chem_name]["brands"] or live_brands[chem_name]["companies"]):
            brands = live_brands[chem_name]["brands"]
            companies = live_brands[chem_name]["companies"]
        else:
            brands = json.loads(brand_json) if brand_json else []
            companies = json.loads(company_json) if company_json else []

        if chem_key in seen_chem_keys:
            if pest_norm: seen_chem_keys[chem_key]["pests_covered"].add(pest_norm)
            continue

        seen_chem_keys[chem_key] = {
            "chemical_name": chem_name,
            "chemical_key": chem_key,
            "category": category,
            "is_combination_product": bool(is_combo),
            "covers_all_pests": chem_key in overlap_keys,
            "pests_covered": {pest_norm} if pest_norm else set(),
            "dosage": {
                "ai_dose": _clean_field(ai_dose),
                "formulation_dose": _clean_field(form_dose),
                "water_dilution": _clean_field(water),
                "waiting_period": _normalize_waiting(waiting),
                "application_method": _clean_field(method),
            },
            "brands": brands,
            "companies": companies,
            "has_brand_info": (len(brands) > 0) or (len(companies) > 0),
        }

    for entry in seen_chem_keys.values():
        entry["pests_covered"] = list(entry["pests_covered"])

    all_entries = list(seen_chem_keys.values())

    def sort_key(e):
        return (0 if e["covers_all_pests"] else 1, 0 if e["has_brand_info"] else 1, e["chemical_name"])

    chemicals_by_cat = {}
    for entry in all_entries:
        cat = entry["category"] or "other"
        # Coerce PGR formatting
        if is_pgr: cat = "pgr"
        if cat not in chemicals_by_cat: chemicals_by_cat[cat] = []
        chemicals_by_cat[cat].append(entry)

    for cat in chemicals_by_cat:
        chemicals_by_cat[cat].sort(key=sort_key)
        chemicals_by_cat[cat] = chemicals_by_cat[cat][:MAX_CHEMICALS_PER_CATEGORY]

    overlap_entries = [e for e in all_entries if e["covers_all_pests"]]
    overlap_entries.sort(key=sort_key)

    return {
        "status": "success",
        "resolved_parameters": {
            "crop": crop_key,
            "crop_display": crop_display,
            "targets_resolved": matched_pest_labels,
            "mapped_from_symptom": mapped_from_symptom,
            "is_pgr_query": is_pgr
        },
        "recommendations": {
            "overlap_best_matches": overlap_entries[:3],
            **{cat: entries for cat, entries in chemicals_by_cat.items() 
               if cat in ("insecticide", "bio_pesticide", "fungicide", "herbicide", "pgr")}
        },
        "summary": {
            "total_options": len(all_entries),
            "has_bio_options": any(e["category"] == "bio_pesticide" for e in all_entries),
            "has_branded_options": any(e["has_brand_info"] for e in all_entries),
        }
    }

# ── TEST RUNNER ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Testing Symptom Bypass (Layer 2 Fallback)")
    payload= {
  "crop": "kapas",
  "pest": None,
  "symptom": "झाडावर पांढरी माशी आणि पानांवर चिकटा पडलाय",
  "category_intent": None,
  "missing_info": None
}
    
    # Needs a dummy DB to run locally, but outputs logic validation.
    res = get_protection_options(payload)
    print(json.dumps(res, indent=2))