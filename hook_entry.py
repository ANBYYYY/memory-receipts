"""Lifecycle entry pinned by code digest in the product hook definition."""
import argparse
import json
from pathlib import Path
import sys
from memory import Memory, digest


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', required=True)
    p.add_argument('--code-sha256', required=True)
    args = p.parse_args()
    base = Path(__file__).resolve().parent
    actual = digest(b''.join((base / name).read_bytes() for name in ['memory.py', 'organizer.py', 'hook_entry.py']))
    if actual != args.code_sha256:
        print(json.dumps({'systemMessage':'项目记忆代码已变更；需重新审查钩子定义，自动面停用。','continue':True},ensure_ascii=False))
        return 0
    print(json.dumps(Memory(args.root).hook(json.load(sys.stdin)), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
