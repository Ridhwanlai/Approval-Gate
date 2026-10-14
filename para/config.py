"""Server-side configuration. The HarnessRouter key never leaves this process.

The key comes from the HR_API_KEY environment variable or, failing that, the project's
owner-only `.env.harnessrouter` file (one line, HR_API_KEY=...), which the HarnessRouter
Skill's secure-input helper writes. The file is read as data, never sourced as shell code.
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = ROOT / ".env.harnessrouter"
FIRM_DIR = ROOT / "firm"
DATA_DIR = Path(os.getenv("PARA_DATA_DIR") or ROOT / "data")
REGISTRY = ROOT / "harnesses" / "registry.json"

HR_BASE_URL = os.getenv("HR_BASE_URL", "https://api.harnessrouter.ai").rstrip("/")
# Discovered from GET /v1/uhp on 2026-10-11; re-run discovery before changing it.
HR_UHP_VERSION = os.getenv("HR_UHP_VERSION", "2026-09-28")
# Simultaneous HarnessRouter tasks this server starts. The Free plan runs 2 at a time.
HR_MAX_CONCURRENT = max(1, int(os.getenv("HR_MAX_CONCURRENT", "2")))


def hr_api_key():
    value = os.getenv("HR_API_KEY", "").strip()
    if value:
        return value
    try:
        if KEY_FILE.is_symlink() or not KEY_FILE.is_file() or KEY_FILE.stat().st_size > 8192:
            return ""
        lines = KEY_FILE.read_text().splitlines()
    except OSError:
        return ""
    if len(lines) == 1 and lines[0].startswith("HR_API_KEY="):
        return lines[0][len("HR_API_KEY="):].strip()
    return ""


def registry():
    """feature_key -> harness_id, written by harnesses/provision.py. Non-secret."""
    try:
        return json.loads(REGISTRY.read_text()).get("features", {})
    except (OSError, ValueError):
        return {}
