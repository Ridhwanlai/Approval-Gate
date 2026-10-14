"""User settings that are not secrets: the model chosen for each agent, and email delivery details.

Stored as JSON next to the audit log (config.SETTINGS_FILE). Secrets never go here: the HarnessRouter
key and the SMTP password each live in their own owner-only file.
"""
import json
import threading

from . import config

_lock = threading.Lock()


def load():
    try:
        data = json.loads(config.SETTINGS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data):
    with _lock:
        config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = config.SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(config.SETTINGS_FILE)


def model_for(feature_key):
    """The user's chosen model for this feature, or None to use the harness's default."""
    return (load().get("models") or {}).get(feature_key) or None


def set_model(feature_key, model):
    data = load()
    models = data.setdefault("models", {})
    if model:
        models[feature_key] = model
    else:
        models.pop(feature_key, None)
    save(data)
