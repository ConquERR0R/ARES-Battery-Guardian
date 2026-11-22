"""Check that the build Python matches requirements.txt EXACTLY (no installs, no changes).

    python packaging\\check_env.py      -> exit 0 when every pinned package matches
"""
from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    print(f"Python {sys.version.split()[0]} ({sys.executable})")
    ok = sys.version_info[:2] == (3, 14)
    if not ok:
        print("  FAIL: Python 3.14.x is required")
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([^\s;#]+)", line)
        if not m:
            continue
        name, want = m.groups()
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            have = None
        good = have == want
        ok &= good
        print(f"  {'OK  ' if good else 'FAIL'} {name}=={want} (installed: {have})")
    print("Environment matches requirements.txt" if ok else "Environment does NOT match requirements.txt")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
