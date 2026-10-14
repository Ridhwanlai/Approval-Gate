#!/usr/bin/env python3
"""Create or update Para's harnesses on HarnessRouter Cloud from the JSON files beside this script.

  python3 harnesses/provision.py           # reuse by name, update in place, create if missing
  python3 harnesses/provision.py --check   # show what would change, write nothing

The JSON files are the source of truth. Each run writes harnesses/registry.json, the non-secret
feature_key -> harness_id map the product server reads.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from para import config  # noqa: E402

# Response field (camelCase) for each request field (snake_case) we manage.
FIELDS = {"base": "base", "default_model": "defaultModel", "system_prompt": "systemPrompt",
          "disabled_tools": "disabledTools", "max_step": "maxStep", "timeout_seconds": "timeoutSeconds",
          "max_cost_usd": "maxCostUsd", "reasoning_effort": "reasoningEffort"}
# Fields these files do not manage; carried over unchanged on update so a PUT never erases them.
KEEP = {"skills": "skills", "mcp_servers": "mcpServers", "plugins": "plugins",
        "additional_headers": "additionalHeaders", "env": "env"}


def call(method, path, body=None):
    req = urllib.request.Request(
        config.HR_BASE_URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + config.hr_api_key(), "Content-Type": "application/json",
                 "Accept": "application/json", "UHP-Version": config.HR_UHP_VERSION})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read(2000).decode(errors="replace")
        raise SystemExit(f"{method} {path} -> HTTP {e.code}: {detail}") from None


def drift(existing, wanted):
    return [f for f, camel in FIELDS.items() if f in wanted and existing.get(camel) != wanted[f]]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if not config.hr_api_key():
        raise SystemExit("No HarnessRouter key. Run the HarnessRouter Skill's key setup; never paste a key into chat.")

    me = call("GET", "/v1/me")
    existing = {h["name"]: h for h in call("GET", "/v1/harnesses").get("harnesses", []) if isinstance(h, dict)}
    features, harnesses = {}, {}
    for spec_file in sorted(HERE.glob("para-*.json")):
        spec = json.loads(spec_file.read_text())
        body = spec["body"]
        current = existing.get(body["name"])
        if current is None:
            action = "create"
        else:
            # Preserve everything we do not manage (skills, MCP servers, plugins, headers, env).
            body = {**{k: current[c] for k, c in KEEP.items() if current.get(c) is not None}, **body}
            changed = drift(current, body)
            action = f"update {changed}" if changed else "unchanged"
        print(f"{body['name']}: {action}")
        if args.check:
            continue
        if current is None:
            hid = call("POST", "/v1/harnesses", body)["id"]
        else:
            hid = current["id"]
            if action != "unchanged":
                call("PUT", f"/v1/harnesses/{hid}", body)
        models = call("GET", f"/v1/harnesses/{hid}/models")
        ids = {m.get("id") for m in (models.get("models") or models.get("data") or []) if isinstance(m, dict)}
        if ids and body.get("default_model") not in ids:
            print(f"  WARNING: {body.get('default_model')} is not offered for {body['name']}")
        harnesses[body["name"]] = hid
        for key in spec["feature_keys"]:
            features[key] = hid
    if args.check:
        return
    registry = {"_help": "Non-secret. Written by harnesses/provision.py; the product server maps features to harnesses with it.",
                "workspace": me.get("workspace"), "base_url": config.HR_BASE_URL, "uhp_version": config.HR_UHP_VERSION,
                "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "features": features, "harnesses": harnesses}
    config.REGISTRY.write_text(json.dumps(registry, indent=2) + "\n")
    print(f"Wrote {config.REGISTRY.relative_to(config.ROOT)}")


if __name__ == "__main__":
    main()
