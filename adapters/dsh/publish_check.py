# -*- coding: utf-8 -*-
"""publish_check.py — 发布前净度自检：**这个目录里有没有掺进真实会话内容？**

它存在的理由：一句「我检查过了」不算证据 —— 检查必须是**别人也能重跑**的东西。
上游包以「可核验」为卖点；那这个转接头自己也得能被核。

它做两类检查：

**结构类**（不需要任何会话数据）
  · 目录里不该有数据文件（`*.jsonl` / `*.zstd` / `*.jsonl.state.json`）
  · 全是 LF-only（不该混进 CRLF）

**内容类**（需要本机的 DSH 会话目录）
  · 把每个会话日志解码出来，收集**真实会话 id** 与**长度 ≥ 阈值的真实正文片段**
  · 在发布集的每个文件里搜这些内容 —— 命中就是泄露

用法:
    python -B -X utf8 publish_check.py [--release <目录>] [--sessions <DSH 的 sessions 目录>]
                                       [--min-fragment 40]
退出码: 0 干净；1 命中；2 环境/参数问题
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dsh_adapter  # noqa: E402

DATA_SUFFIXES = (".jsonl", ".zstd", ".jsonl.state.json")
# 盘符绝对路径。前置负向断言很重要：否则 URL 协议头（https 后面那一段）
# 会被误报成盘符 —— 本文件第一版就被自己的注释里的字面量绊过一次。
ABS_PATH = re.compile(rb"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'`]*")


def find_sessions(explicit):
    if explicit:
        p = Path(explicit)
        return p if p.is_dir() else None
    cands = []
    h = os.environ.get("DSH_HOME")
    if h:
        cands.append(Path(h) / "sessions")
    cands.append(Path.home() / ".dsh" / "sessions")
    for c in cands:
        if c.is_dir():
            return c
    return None


def collect_real_content(sessions_dir):
    """返回 (会话 id 集合, 正文片段集合, 解码失败的日志数, 日志总数)。

    ⚠️ 红队 A-3：以前 `except: continue` 把解码失败**静默吞掉**，
    检查强度下降时输出仍是 CLEAN。所以这里把失败数**上报**，让调用方能判"不完整"。
    """
    logs = sorted(Path(sessions_dir).rglob("session.v4.jsonl.zstd"))
    ids, fragments = set(), set()
    skipped = 0
    tmp = Path(tempfile.mkdtemp(prefix="publish-check-"))
    try:
        for log in logs:
            try:
                _dec, _rd = dsh_adapter.decode_to_jsonl(log, tmp / "raw.jsonl")
                text = (tmp / "raw.jsonl").read_text(encoding="utf-8", errors="replace")
            except Exception:
                skipped += 1
                continue
            for line in text.split("\n"):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") == "session" and rec.get("id"):
                    ids.add(str(rec["id"]))
                d = rec.get("data") or {}
                for holder in (d, d.get("message") or {}):
                    for it in (holder.get("content") or []):
                        if isinstance(it, dict) and isinstance(it.get("text"), str):
                            fragments.add(it["text"])
                    if isinstance(holder.get("arguments"), str):
                        fragments.add(holder["arguments"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return ids, fragments, skipped, len(logs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", default=str(HERE))
    ap.add_argument("--sessions", default=None)
    ap.add_argument("--min-fragment", type=int, default=40)
    args = ap.parse_args()

    rel = Path(args.release).resolve()
    files = sorted(p for p in rel.rglob("*") if p.is_file())
    if not files:
        print("发布目录里没有文件:", rel)
        return 2

    print("=" * 60)
    print("发布集:", rel)
    for p in files:
        print("  %8d B  %s" % (p.stat().st_size, p.relative_to(rel)))

    problems = []

    print("\n--- 结构类检查 ---")
    data_files = [p.name for p in files if p.name.endswith(DATA_SUFFIXES)]
    if data_files:
        problems.append(("目录里有数据文件", data_files))
        print("  [FAIL] 发现数据文件:", data_files)
    else:
        print("  [PASS] 没有 *.jsonl / *.zstd / *.state.json 这类数据文件")

    crlf = []
    for p in files:
        b = p.read_bytes()
        if b.count(b"\r"):
            crlf.append((p.name, b.count(b"\r")))
    if crlf:
        problems.append(("不是 LF-only", crlf))
        print("  [FAIL] 有 CR 字节:", crlf)
    else:
        print("  [PASS] 全部 LF-only（CR = 0）")

    blob = {p: p.read_bytes() for p in files}
    abs_hits = [(p.name, ABS_PATH.search(b).group(0)[:60].decode("utf-8", "replace"))
                for p, b in blob.items() if ABS_PATH.search(b)]
    if abs_hits:
        print("  [注意] 含盘符绝对路径（不一定是问题，但要人看一眼）:")
        for n, s in abs_hits:
            print("      ", n, "->", s)
    else:
        print("  [PASS] 没有盘符绝对路径")

    print("\n--- 内容类检查 ---")
    sess = find_sessions(args.sessions)
    skipped, total_logs = 0, 0
    if sess is None:
        print("  [跳过] 找不到 DSH 会话目录（可用 --sessions 指定）。")
    else:
        print("  会话目录:", sess)
        ids, frags, skipped, total_logs = collect_real_content(sess)
        long_frags = sorted({f for f in frags if len(f) >= args.min_fragment})
        print("  真实会话 id: %d 个；真实正文片段: %d 个（其中 ≥%d 字的 %d 个）"
              % (len(ids), len(frags), args.min_fragment, len(long_frags)))
        if skipped:
            print("  [注意] %d/%d 个会话日志**解码失败、已跳过** —— 它们的内容没有参与比对"
                  % (skipped, total_logs))

        hits = []
        for p, b in blob.items():
            for i in ids:
                if i.encode("utf-8") in b:
                    hits.append(("会话 id", p.name, i))
            for f in long_frags:
                if f.encode("utf-8") in b:
                    hits.append(("真实正文", p.name, f[:50]))
        if hits:
            problems.append(("命中真实内容", len(hits)))
            print("  [FAIL] 命中 %d 处：" % len(hits))
            for h in hits[:10]:
                print("      ", h)
        else:
            print("  [PASS] 对 %d 个会话 id + %d 条长正文片段，命中 0"
                  % (len(ids), len(long_frags)))

    print("\n" + "=" * 60)
    print("⚠️ 它的诚实边界：「命中 0」只证明**发布集里没有本机这些会话的内容**，")
    print("   不证明作者没写过别处的内容，也不证明代码没有别的泄密路径。")
    if problems:
        print("PUBLISH-CHECK DIRTY —— 有 %d 类问题，别发。" % len(problems))
        return 1
    # 红队 A-3 / A-5：检查强度下降时**不许印 CLEAN、不许 rc=0**
    if sess is None or skipped:
        print("PUBLISH-CHECK INCOMPLETE —— 结构类过了，但内容类**没有完整跑**：")
        if sess is None:
            print("  · 找不到 DSH 会话目录 ⇒ 内容类整块未验")
        if skipped:
            print("  · %d/%d 个会话日志解码失败 ⇒ 那些内容没参与比对" % (skipped, total_logs))
        print("  「没检查」不等于「检查过没问题」—— 所以这里**不印 CLEAN**。")
        return 2
    print("PUBLISH-CHECK CLEAN —— 结构类 + 内容类都完整跑过，命中 0。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
