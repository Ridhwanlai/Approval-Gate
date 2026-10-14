#!/usr/bin/env python3
"""Start Para: the mock Ezio API on a private port, then the product server.

  python3 run.py                 # http://127.0.0.1:9000/
  PORT=50412 python3 run.py      # any port
  LATENCY=2-6 python3 run.py     # the mock's original latency (default here: 0.5-1.5 s)

HarnessRouter agents need harnesses/registry.json (python3 harnesses/provision.py) and a server-side
key in HR_API_KEY or .env.harnessrouter. Without them every memo is held for review, never sent unread.
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    mock_port = free_port()
    env = {**os.environ, "HOST": "127.0.0.1", "PORT": str(mock_port), "LATENCY": os.getenv("LATENCY", "0.5-1.5")}
    env.pop("HR_API_KEY", None)  # the mock never needs the HarnessRouter key
    mock = subprocess.Popen([sys.executable, str(ROOT / "mock_ezio.py")], env=env, cwd=ROOT, stdout=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{mock_port}/v1/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    os.environ["EZIO_URL"] = f"http://127.0.0.1:{mock_port}"
    sys.path.insert(0, str(ROOT))
    from para import server  # imported after EZIO_URL is set; ezio_client reads it at import
    try:
        server.serve()
    finally:
        mock.terminate()


if __name__ == "__main__":
    main()
