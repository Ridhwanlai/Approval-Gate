#!/usr/bin/env python3
"""Start Para from a source checkout: the mock Ezio API on a private port, then the product server.

  python3 run.py                  # http://127.0.0.1:9000/
  PORT=50412 python3 run.py       # any port
  python3 run.py setup|provision|status

Same commands as the one-file download (python3 para.pyz). From source the browser is not opened.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from para.cli import main  # noqa: E402

if __name__ == "__main__":
    main(sys.argv[1:] or ["run", "--no-browser"])
