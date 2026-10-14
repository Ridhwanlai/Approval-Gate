"""Para's command line. The one-file download (para.pyz) and run.py both land here.

  para run [--port N] [--no-browser]   set up if needed, then start the gate (default command)
  para setup                           save or replace your HarnessRouter key (hidden prompt)
  para provision                       create or update Para's three harnesses in your account
  para status                          show what is configured, never the key itself
"""
import argparse
import getpass
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser

from . import config

KEY_RE = re.compile(r"sk-hr-[A-Za-z0-9_-]{8,}")
SIGNUP = "https://harnessrouter.ai"


def _say(msg=""):
    print(msg, flush=True)


# ---------------------------------------------------------------- key

def verify_key(key):
    """'ok', 'rejected' or 'unreachable'. Reads only the status code, never a response body."""
    req = urllib.request.Request(config.HR_BASE_URL + "/v1/models",
                                 headers={"Authorization": "Bearer " + key, "UHP-Version": config.HR_UHP_VERSION})
    try:
        with urllib.request.urlopen(req, timeout=15):
            return "ok"
    except urllib.error.HTTPError as e:
        return "rejected" if e.code in (401, 403) else "unreachable"
    except OSError:
        return "unreachable"


def save_key(key):
    """Write the key owner-only, atomically, to config.KEY_FILE (one line: HR_API_KEY=...)."""
    target = config.KEY_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise SystemExit(f"Refusing to write the key through a symlink at {target}")
    fd, tmp = tempfile.mkstemp(prefix=".hr-key-", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("HR_API_KEY=" + key + "\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def setup():
    if not sys.stdin.isatty():
        _say("Setup needs an interactive terminal. Alternatively set HR_API_KEY in the server's environment.")
        return False
    _say("Para runs its AI checks on HarnessRouter, billed to your own HarnessRouter account.")
    _say(f"Create an API key in the HarnessRouter console ({SIGNUP}), then paste it below.")
    _say("Typing is hidden. The key is stored only on this computer, readable by you alone.\n")
    for _ in range(3):
        try:
            key = getpass.getpass("HarnessRouter API key (sk-hr-...): ").strip()
        except (EOFError, KeyboardInterrupt):
            _say("\nSetup cancelled. Nothing was saved.")
            return False
        if not KEY_RE.fullmatch(key):
            _say("That does not look like a HarnessRouter key (it starts with sk-hr-). Try again.")
            continue
        result = verify_key(key)
        if result == "rejected":
            _say("HarnessRouter rejected that key. Check it in the console and try again.")
            continue
        save_key(key)
        _say(f"Key saved to {config.KEY_FILE}" + (" (HarnessRouter was unreachable, so it is not verified yet)." if result != "ok" else "."))
        return True
    _say("No key saved.")
    return False


# ---------------------------------------------------------------- provisioning

def ensure_harnesses(force=False):
    from .provisioning import ProvisionError, provision, specs
    wanted = {k for s in specs() for k in s["feature_keys"]}
    if not force and wanted <= set(config.registry()):
        return True
    _say("Setting up Para's three agents in your HarnessRouter account…")
    try:
        provision(log=lambda line: _say("  " + line))
        return True
    except ProvisionError as e:
        _say(f"Could not set up the agents: {e}")
        return False


def status():
    from . import agents
    key_source = "HR_API_KEY environment variable" if os.getenv("HR_API_KEY") else (str(config.KEY_FILE) if config.hr_api_key() else "none")
    reg = config.registry()
    info = {"key": key_source, "agents": agents.status(), "registry": str(config.REGISTRY),
            "features": sorted(reg), "data": str(config.DATA_DIR), "harnessrouter": config.HR_BASE_URL}
    _say(json.dumps(info, indent=2))


# ---------------------------------------------------------------- run

def _port_free(port):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(port=None, open_browser=True):
    if not config.hr_api_key():
        _say("No HarnessRouter key yet.\n")
        if not setup():
            _say("\nStarting without AI agents: every memo will be held for review, never sent unread.\n")
    if config.hr_api_key():
        ensure_harnesses()

    explicit = port or os.getenv("PORT")
    port = int(explicit or 9000)
    if not _port_free(port):
        if explicit:
            raise SystemExit(f"Port {port} is already in use.")
        port = next(p for p in range(9001, 9100) if _port_free(p))

    # A real Ezio endpoint (EZIO_URL, with EZIO_KEY) is used when given; otherwise the bundled simulation runs locally.
    mock = None
    if os.getenv("EZIO_URL"):
        _say(f"Using the Ezio API at {os.environ['EZIO_URL']}")
    else:
        mock_port = _free_port()
        env = {k: v for k, v in os.environ.items() if k != "HR_API_KEY"}  # the mock never needs the key
        env.update(HOST="127.0.0.1", PORT=str(mock_port), LATENCY=os.getenv("LATENCY", "0.5-1.5"))
        mock = subprocess.Popen([sys.executable, str(config.ROOT / "mock_ezio.py")], env=env, cwd=config.ROOT,
                                stdout=subprocess.DEVNULL)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{mock_port}/v1/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        os.environ["EZIO_URL"] = f"http://127.0.0.1:{mock_port}"
        _say("Using the simulated Ezio bundled with Para (set EZIO_URL and EZIO_KEY to use a live Ezio).")
    from . import server  # imported after EZIO_URL is set; ezio_client reads it at import
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    try:
        server.serve("127.0.0.1", port)
    finally:
        if mock:
            mock.terminate()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="para", description="Para: an approval gate for AI-drafted legal research memos.")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="start the gate (default)")
    r.add_argument("--port", type=int)
    r.add_argument("--no-browser", action="store_true")
    sub.add_parser("setup", help="save or replace your HarnessRouter key")
    sub.add_parser("provision", help="create or update the three harnesses")
    sub.add_parser("status", help="show what is configured")
    args = ap.parse_args(argv)
    if args.cmd == "setup":
        sys.exit(0 if setup() else 1)
    if args.cmd == "provision":
        sys.exit(0 if ensure_harnesses(force=True) else 1)
    if args.cmd == "status":
        return status()
    run(getattr(args, "port", None), not getattr(args, "no_browser", False))


if __name__ == "__main__":
    main()
