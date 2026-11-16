import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
RUNS_DIR = DATA_DIR / "runs"
UPLOADS_DIR = DATA_DIR / "uploads"
EXECUTION_TOKEN_LIMIT = 250_000
MAX_HTTP_JSON_BYTES = 35 * 1024 * 1024
DB_PATH = DATA_DIR / "agentforge.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
RUNS_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

def mode_label() -> str:
    keys = (
        "MDATAPLUS_API_KEY",
        "AF_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY",
        "AF_MIMO_API_KEY", "MIMO_API_KEY",
    )
    if any(os.getenv(name, "").strip() for name in keys):
        return "live"
    legacy_provider = os.getenv("AF_LLM_PROVIDER", "mock").strip().lower()
    legacy_key = os.getenv("AF_LLM_API_KEY", "").strip()
    return "live" if legacy_provider != "mock" and legacy_key else "mock"


SCHEDULER_WEIGHTS = {
    "capability": 0.40,
    "success": 0.25,
    "load": 0.20,
    "cost": 0.15,
}

DEGRADE_PENALTY = 0.25
CIRCUIT_PENALTY = 0.55
