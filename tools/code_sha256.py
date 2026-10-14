# -*- coding: utf-8 -*-
"""Print the code digest that hook_entry.py pins.

Usage (run from the package directory):
    python -B -X utf8 tools/code_sha256.py

It is the sha256 of the BYTES of memory.py, organizer.py and hook_entry.py,
concatenated in exactly that order -- the same way hook_entry.py itself computes it.
Put the printed value into the host hook definition:

    "<PYTHON>" -X utf8 "<PACKAGE_DIR>/hook_entry.py" --root "<PROJECT_ROOT>" --code-sha256 <VALUE>

Whenever you edit any of those three files the digest changes and the automatic
face disables itself on purpose (it prints a systemMessage and exits 0).
"""
import hashlib
import pathlib
import sys

FILES = ("memory.py", "organizer.py", "hook_entry.py")


def main():
    base = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
    base = base.resolve()
    blob = b""
    for name in FILES:
        p = base / name
        if not p.is_file():
            print("missing: %s" % p)
            return 2
        n = p.stat().st_size
        print("  %-16s %7d B" % (name, n))
        blob += p.read_bytes()
    print("code_sha256 = %s" % hashlib.sha256(blob).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
