# -*- coding: utf-8 -*-
"""verify_derived.py — 独立复核：**不 import memory.py**，直接从磁盘重算。

判据 A  每个收件事件的 sha256(源文件[offset:end]) == 事件里的 raw_sha256
判据 B  该切片解析出的文本 == 事件里存的文本（回读一致性）
判据 C  （可选）--find <文本>：指定的一句话能不能被原样取回并溯源

用法:
    python -B -X utf8 verify_derived.py --state <memory-pack 的状态目录>
    python -B -X utf8 verify_derived.py --state <目录> --find "某句话"
退出码: 0 全过；1 有判据不通过；2 环境/参数问题
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8-sig"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True, help="memory-pack 的 --root 目录")
    ap.add_argument("--find", default=None, help="可选：要取回的一句话")
    args = ap.parse_args()

    state = Path(args.state).resolve()
    runtime = state / "runtime"
    if not (runtime / "sources.json").exists():
        print("找不到 %s —— 这个目录还没 register 过任何来源。" % (runtime / "sources.json"))
        return 2

    sources = {s["session_id"]: s for s in load(runtime / "sources.json")["sources"]}
    print("已登记来源: %d" % len(sources))
    for sid, s in sources.items():
        print("   %s -> %s" % (sid[:24] + ("…" if len(sid) > 24 else ""), s["path"]))

    events = {}
    for p in sorted((runtime / "inbox").glob("*.json")):
        for e in load(p)["events"]:
            events[e["id"]] = e
    print("\n收件事件总数: %d" % len(events))
    kinds = {}
    for e in events.values():
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print("按 kind: %s" % json.dumps(kinds, ensure_ascii=False, sort_keys=True))

    cache = {}
    checked = bad_a = bad_b = b_covered = 0
    samples = []
    for e in events.values():
        path = sources[e["session_id"]]["path"]
        if path not in cache:
            cache[path] = Path(path).read_bytes()
        chunk = cache[path][e["offset"]:e["end"]]
        checked += 1

        if hashlib.sha256(chunk).hexdigest() != e["raw_sha256"]:
            bad_a += 1
            if len(samples) < 3:
                samples.append(("A", e["id"]))
            continue

        try:
            rec = json.loads(chunk.decode("utf-8"))
            p = rec.get("payload", {})
            if e["kind"] in ("user", "assistant"):
                b_covered += 1
                got = "\n".join(c.get("text", "") for c in p.get("content", [])
                                if c.get("type") in ("input_text", "output_text", "text"))
                if got != e["text"]:
                    bad_b += 1
                    if len(samples) < 3:
                        samples.append(("B", e["id"]))
        except Exception as exc:
            bad_b += 1
            if len(samples) < 3:
                samples.append(("B-parse", str(exc)))

    # ⚠️ 红队 P0-3（2026-10-17）：以前 checked==0 时也打印 VERIFY OK（判据恒真的假通过）。
    # 空收件 / 还没 sync / inbox 被清掉 —— 都会走到这里，必须**明确判不过**。
    if checked == 0:
        print("\n判据 A/B 覆盖 0 条 —— **什么都没验**。")
        print("  可能原因：还没 sync；register 用了 --from end；runtime/inbox/ 被清掉；状态目录不是这个。")
        print("  「没检查」不等于「检查过没问题」—— 所以这里判 **不通过**。")
        print("\nVERIFY VACUOUS (nothing was checked)")
        return 2

    print("\n判据 A 独立复算 provenance : 检查 %d 条，不匹配 %d 条  %s"
          % (checked, bad_a, "OK" if bad_a == 0 else "FAIL"))
    print("判据 B 回读文本一致性      : 覆盖 %d 条（仅 user/assistant 有 text 字段），不匹配 %d 条  %s"
          % (b_covered, bad_b, "OK" if bad_b == 0 else "FAIL"))
    print("       ⚠️ 判据 B 不覆盖 tool_call / tool_result —— 它们的 text 字段名不同（input/output），"
          "本判据不比对它们（那由判据 A 的整行哈希覆盖）。")
    if samples:
        print("   不匹配样例:", samples)

    rc = 0 if (bad_a == 0 and bad_b == 0) else 1

    if args.find:
        print("\n判据 C —— 取回指定的一句话")
        hits = [e for e in events.values() if args.find in json.dumps(e, ensure_ascii=False)]
        print("   命中事件数: %d" % len(hits))
        for e in hits[:5]:
            again = hashlib.sha256(cache[sources[e["session_id"]]["path"]][e["offset"]:e["end"]]).hexdigest()
            print("   kind=%s  offset=%d..%d  %s"
                  % (e["kind"], e["offset"], e["end"],
                     "MATCH" if again == e["raw_sha256"] else "MISMATCH"))
            body = e.get("text") or e.get("input") or e.get("output") or ""
            for line in str(body).splitlines():
                if args.find in line:
                    print("      > " + line.strip()[:300])
        if not hits:
            rc = 1
            print("   未取到 —— 判据 C 不通过")

    print("\nVERIFY " + ("OK" if rc == 0 else "FAILED"))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
