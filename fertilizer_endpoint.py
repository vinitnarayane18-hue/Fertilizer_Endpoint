"""
protectionendpoint.py (v1)
-----------------------
FastAPI route: POST /fertilizer-info
x402-avm payment gate: $0.04 USDC on Algorand mainnet

Payment flow (M2M / direct x402):
    Any caller -> sends X-PAYMENT header with USDC tx
    -> GoPlausible facilitator verifies on Algorand
    -> 200 OK with structured JSON

Human farmer flow (via WhatsApp agent):
    Razorpay webhook fires payment.captured
    -> WhatsApp backend calls this endpoint from float wallet
    -> Returns JSON -> WhatsApp agent formats into Marathi

This endpoint does NOT know or care which flow called it.
"""
import os
import sys
import pathlib
import logging
from typing import Optional, List, Union

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        env_path = args[0] if args else kwargs.get("dotenv_path")
        if not env_path or not os.path.exists(env_path):
            return False
        with open(env_path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
        return True

load_dotenv(pathlib.Path(__file__).with_name(".env"))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# x402-avm: pip install "x402-avm[fastapi,avm,extensions]"
from x402.schemas import AssetAmount, Network
from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.avm.exact import ExactAvmServerScheme
from x402.server import x402ResourceServer
from x402.extensions import bazaar_resource_server_extension, declare_discovery_extension

# Import pure data module from the current directory
sys.path.append(str(pathlib.Path(__file__).parent))
from fertilizermodule import get_protection_options

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config - set via environment variables (Defaults to Mainnet)
# ---------------------------------------------------------------------------

os.environ["ALGOD_TOKEN"] = ""
os.environ["AVM_ALGOD_TOKEN"] = ""

# 1. Your production merchant wallet address
AVM_ADDRESS = os.getenv("AVM_ENDPOINT_WALLET", "BRSMWTNWFRW26LU7FQ7CG2KY65P5HTCBXX6QAOIEM35NESQFGWM4KWEYDU")
FACILITATOR_URL = "https://facilitator.goplausible.xyz"

# 2. Mainnet Genesis Hash
AVM_NETWORK: Network = os.getenv(
    "AVM_NETWORK", 
    "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="
)

# 3. Real USDC on Algorand Mainnet ASA ID: 31566704
USDC_ASA_ID = os.getenv("USDC_ASA_ID", "31566704")

# 4. Price targeted via absolute atomic micro-units ($0.04 USDC = 40000 micro-units)
PROTECTION_PRICE = os.getenv("PROTECTION_PRICE_USDC", "40000")

# ---------------------------------------------------------------------------
# x402 server setup
# ---------------------------------------------------------------------------

facilitator = HTTPFacilitatorClient(
    FacilitatorConfig(url=FACILITATOR_URL)
)

server = x402ResourceServer(facilitator)
server.register(AVM_NETWORK, ExactAvmServerScheme())
server.register_extension(bazaar_resource_server_extension)

routes: dict[str, RouteConfig] = {
    "POST /fertilizer-info": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                network=AVM_NETWORK,
                pay_to=AVM_ADDRESS,
                price=AssetAmount(
                    amount=PROTECTION_PRICE,
                    asset=USDC_ASA_ID,
                ),
                extra={"name": "USDC", "decimals": 6},
            ),
        ],
        description=(
            "Crop protection,Fertilizer & chemical advisory API for Maharashtra. "
            "Evaluates crops, pests, and fuzzy symptoms using an AI-backed diagnostic layer. "
            "Returns highly specific, CIBRC-approved chemical formulations, real-market brands, "
            "waiting periods, and safe dosage metrics."
        ),
        mime_type="application/json",
        resource="https://agriintellect.site/fertilizer-info",
        extensions=declare_discovery_extension(
            
            input={
                "method": "POST",
                "crop": "tomato",
                "pest": ["fruit borer", "whitefly"],
                "symptom": None,
                "category_intent": None,
                "missing_info": False
            },
            input_schema={
                "type": "object",
                "properties": {
                    "crop": {"type": "string", "description": "Target crop (e.g., tomato, cotton, soybean)."},
                    "pest": {"type": "array", "items": {"type": "string"}, "description": "List of pest names or slang (e.g., ['fruit borer', 'मावा'])."},
                    "symptom": {"type": "string", "description": "Fuzzy symptom description to be parsed by Gemini if pest is unknown."},
                    "category_intent": {"type": "string", "description": "Filter by intent (e.g., 'PGR', 'fungicide')."},
                    "missing_info": {"type": "boolean", "description": "Flags if the orchestrator is missing core crop data."}
                },
                "required": ["crop"]
            },
            body_type="json"
        )
    ),
}

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AgriIntel Crop Protection API",
    description="Agricultural pest routing and chemical optimization endpoint - x402 payment gated, Algorand USDC",
    version="1.0.0",
)

# Add x402 payment middleware (checks X-PAYMENT header before route handler runs)
app.add_middleware(PaymentMiddlewareASGI, server=server, routes=routes)

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class CropProtectionRequest(BaseModel):
    crop: str = Field(
        ..., 
        description="Target crop (e.g., tomato, cotton, soybean, rice)."
    )
    pest: Optional[Union[List[str], str]] = Field(
        None, 
        description="List of pest names or regional slangs."
    )
    symptom: Optional[str] = Field(
        None, 
        description="Fuzzy symptom description if the exact pest is unknown."
    )
    category_intent: Optional[str] = Field(
        None, 
        description="Specific chemical category to filter by (e.g., 'PGR')."
    )
    missing_info: bool = Field(
        False, 
        description="Set to true if upstream LLM could not determine the crop."
    )

# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------

@app.post(
    "/fertilizer-info",
    responses={
        402: {
            "description": "Payment Required. A cryptographically signed Algorand transaction proof for $0.04 USDC must be provided in the X-PAYMENT header."
        }
    }
)
async def crop_protection(request: Request, body: CropProtectionRequest):
    """
    Returns net-profit optimized APMC markets, logistics vehicle recommendations,
    freight & deduction breakdowns, and AI execution rules for a given location.

    Payment: $0.04 USDC via x402 header (Algorand mainnet)
    No API key required. No account needed.
    """
    # Convert Pydantic object to the pure dictionary payload our engine expects
    payload = body.model_dump()
    
    # Execute the synchronous engine locally
    result = get_protection_options(payload)

    # Top-level error checks
    status = result.get("status")
    
    if status == "missing_info":
        return JSONResponse(status_code=400, content=result)
    
    if status == "crop_not_found":
        return JSONResponse(status_code=404, content=result)
        
    if status == "no_match":
        # 200 OK because the engine processed successfully, just found 0 chemicals.
        return JSONResponse(status_code=200, content=result)

    return result

# ---------------------------------------------------------------------------
# Health check (unpaid - for monitoring)
# ---------------------------------------------------------------------------

@app.get("/health")
@app.head("/health")
async def health():
    return {"status": "ok", "endpoint": "fertilizer-info", "price_usdc": "0.04"}

# ---------------------------------------------------------------------------
# Discovery endpoint (unpaid - for Bazaar indexing)
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return {
        "name": "AgriIntel Fertilizer API",
        "version": "1.0.0",
        "endpoint": "POST /fertilizer-info",
        "price": "$0.04 USDC",
        "network": "Algorand mainnet",
        "payment": "x402 (X-PAYMENT header)",
        "coverage": "Maharashtra, India",
        "inputs": {
            "crop": "string, required",
            "pest": "list[string] or string, optional",
            "symptom": "string, optional",
            "category_intent": "string, optional",
            "missing_info": "boolean, default False",
        },
        "outputs": {
            "status": "success | no_match | crop_not_found",
            "resolved_parameters": "dict of interpreted inputs",
            "recommendations": "dict categorized by chemical type containing dosages, waiting periods, and brand names",
            "summary": "metadata regarding total options found"
        }
    }

# ---------------------------------------------------------------------------
# Run (dev only - use gunicorn/uvicorn in production)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
