"""Create or update Para's harnesses on HarnessRouter Cloud from harnesses/para-*.json.

The JSON files are the source of truth. Harnesses are reused by name, updated in place when they
drift, and created when missing. The result is the non-secret feature_key -> harness_id registry
the server reads (config.REGISTRY).
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import config

SPEC_DIR = config.ROOT / "harnesses"
# Response field (camelCase) for each request field (snake_case) the JSON files manage.
FIELDS = {"base": "base", "default_model": "defaultModel", "system_prompt": "systemPrompt",
          "disabled_tools": "disabledTools", "max_step": "maxStep", "timeout_seconds": "timeoutSeconds",
          "max_cost_usd": "maxCostUsd", "reasoning_effort": "reasoningEffort"}
# Fields the files do not manage; carried over unchanged on update so a PUT never erases them.
KEEP = {"skills": "skills", "mcp_servers": "mcpServers", "plugins": "plugins",
        "additional_headers": "additionalHeaders", "env": "env"}


class ProvisionError(Exception):
    pass


def _call(method, path, body=None):
    req = urllib.request.Request(
        config.HR_BASE_URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + config.hr_api_key(), "Content-Type": "application/json",
                 "Accept": "application/json", "UHP-Version": config.HR_UHP_VERSION})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        hint = {401: "the HarnessRouter key was rejected", 403: "the key may not manage harnesses"}.get(e.code, "")
        raise ProvisionError(f"{method} {path} failed with HTTP {e.code}" + (f": {hint}" if hint else "")) from None
    except OSError as e:
        raise ProvisionError(f"HarnessRouter could not be reached ({type(e).__name__})") from None


def _drift(existing, wanted):
    return [f for f, camel in FIELDS.items() if f in wanted and existing.get(camel) != wanted[f]]


def specs():
    return [json.loads(p.read_text()) for p in sorted(SPEC_DIR.glob("para-*.json"))]


def provision(check=False, log=print):
    """Apply every spec; return the registry dict (and write it unless check=True)."""
    if not config.hr_api_key():
        raise ProvisionError("No HarnessRouter key is configured.")
    me = _call("GET", "/v1/me")
    existing = {h["name"]: h for h in _call("GET", "/v1/harnesses").get("harnesses", []) if isinstance(h, dict)}
    features, harnesses = {}, {}
    for spec in specs():
        body = spec["body"]
        current = existing.get(body["name"])
        if current is None:
            action = "create"
        else:
            body = {**{k: current[c] for k, c in KEEP.items() if current.get(c) is not None}, **body}
            changed = _drift(current, body)
            action = f"update {changed}" if changed else "unchanged"
        log(f"{body['name']}: {action}")
        if check:
            continue
        if current is None:
            hid = _call("POST", "/v1/harnesses", body)["id"]
        else:
            hid = current["id"]
            if action != "unchanged":
                _call("PUT", f"/v1/harnesses/{hid}", body)
        models = _call("GET", f"/v1/harnesses/{hid}/models")
        ids = {m.get("id") for m in (models.get("models") or models.get("data") or []) if isinstance(m, dict)}
        if ids and body.get("default_model") not in ids:
            log(f"  warning: {body.get('default_model')} is not offered for {body['name']}")
        harnesses[body["name"]] = hid
        for key in spec["feature_keys"]:
            features[key] = hid
    registry = {"_help": "Non-secret. Written by Para's provisioning; the server maps features to harnesses with it.",
                "workspace": me.get("workspace"), "base_url": config.HR_BASE_URL, "uhp_version": config.HR_UHP_VERSION,
                "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "features": features, "harnesses": harnesses}
    if not check:
        config.REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        config.REGISTRY.write_text(json.dumps(registry, indent=2) + "\n")
    return registry
