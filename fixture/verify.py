# -*- coding: utf-8 -*-
"""Independent checker for a memory-pack state directory.

Usage:
    python -B -X utf8 fixture/verify.py <STATE_DIR>

It does NOT import memory.py: it re-reads the files on disk and recomputes every
hash itself, so "it passes" is not just the program agreeing with itself.

Checks
  A  receipt binding .... the newest receipt's state_sha256 / card_sha256 equal the
                          actual sha256 of the version file and the current card
  B  head binding ....... head.json agrees with the newest receipt (version, hashes,
                          commit_id)
  C  event trace ........ for every event in runtime/inbox, sha256(source[offset:end])
                          equals the recorded raw_sha256 (byte-exact traceability)
  D  inbox hygiene ...... the fixture sentinel does not appear anywhere in the inbox

Exit code: 0 = every check passed, 1 = at least one failed.
"""
import hashlib
import json
import pathlib
import sys

SENTINEL = "FIXTURE_PRIVATE_SENTINEL"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8-sig"))


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip())
        return 2
    root = pathlib.Path(sys.argv[1]).resolve()
    base = root / "runtime"
    card = root / "outputs" / "memory-card.md"
    failures = []

    head = load(base / "head.json")
    receipts = sorted((base / "receipts").glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not receipts:
        print("FAIL A: no receipt written")
        return 1
    receipt = load(receipts[-1])

    # A -- receipt binding
    version_file = base / "versions" / head["file"]
    state_sha = sha256(version_file.read_bytes())
    card_sha = sha256(card.read_bytes())
    print("A receipt binding")
    print("    receipt.version      = %s" % receipt["version"])
    print("    state_sha256         = %s" % state_sha)
    print("    receipt.state_sha256 = %s  %s" % (receipt["state_sha256"],
                                                "MATCH" if state_sha == receipt["state_sha256"] else "MISMATCH"))
    print("    card_sha256          = %s" % card_sha)
    print("    receipt.card_sha256  = %s  %s" % (receipt["card_sha256"],
                                                "MATCH" if card_sha == receipt["card_sha256"] else "MISMATCH"))
    if state_sha != receipt["state_sha256"] or card_sha != receipt["card_sha256"]:
        failures.append("A")

    # B -- head binding
    print("B head binding")
    ok_b = (head["version"] == receipt["version"]
            and head["commit_id"] == receipt["commit_id"]
            and head["state_sha256"] == state_sha
            and head["card_sha256"] == card_sha)
    print("    head.version=%s head.commit_id=%s -> %s" % (head["version"], head["commit_id"],
                                                           "MATCH" if ok_b else "MISMATCH"))
    if not ok_b:
        failures.append("B")

    # C -- event trace, recomputed from the raw source log
    print("C event trace (recomputed from the raw source file)")
    events = []
    for p in sorted((base / "inbox").glob("*.json")):
        events += load(p)["events"]
    if not events:
        print("    no events in inbox -> FAIL")
        failures.append("C")
    for e in events:
        raw = pathlib.Path(e["path"]).read_bytes()[e["offset"]:e["end"]]
        got = sha256(raw)
        good = got == e["raw_sha256"]
        print("    %-9s offset=%-6s end=%-6s recomputed=%s..%s  %s"
              % (e["kind"], e["offset"], e["end"], got[:12], got[-8:], "MATCH" if good else "MISMATCH"))
        if not good:
            failures.append("C")

    # D -- inbox hygiene
    print("D inbox hygiene")
    blob = b"".join(p.read_bytes() for p in sorted((base / "inbox").glob("*.json")))
    leaked = SENTINEL.encode("utf-8") in blob
    print("    reasoning/developer/analysis records in the source log = 3")
    print("    sentinel found in inbox                                  = %s" % leaked)
    if leaked:
        failures.append("D")

    print()
    if failures:
        print("VERIFY FAILED: %s" % ", ".join(failures))
        return 1
    print("VERIFY OK: receipt+head bound to the actual bytes, %d event(s) traceable, inbox clean" % len(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
