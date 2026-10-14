#!/usr/bin/env python3
"""Create or update Para's harnesses on HarnessRouter Cloud from the JSON files beside this script.

  python3 harnesses/provision.py           # reuse by name, update in place, create if missing
  python3 harnesses/provision.py --check   # show what would change, write nothing

Writes the non-secret feature_key -> harness_id registry the product server reads.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from para import config  # noqa: E402
from para.provisioning import ProvisionError, provision  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    try:
        provision(check=args.check)
    except ProvisionError as e:
        raise SystemExit(f"{e} Run the HarnessRouter Skill's key setup; never paste a key into chat.")
    if not args.check:
        print(f"Wrote {config.REGISTRY}")


if __name__ == "__main__":
    main()
