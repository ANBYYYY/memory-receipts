# -*- coding: utf-8 -*-
"""dsh_adapter.py — 把 DSH 的会话日志翻成 memory-pack 认识的格式（转接头）。

设计约束（三条，都别破）：
  1. **不改 memory-pack 一个字节。** 本文件在包【外面】工作：读 DSH 日志 → 写一份
     「派生日志」（Codex rollout 形状）→ 交给 memory.py register/sync。
  2. **只追加。** 派生日志一旦写出，前面的字节永不改（memory.py 的游标存的是
     prefix_sha256，改写前缀会让 sync 报 source truncated/replaced）。
  3. **不打印任何正文。** 读数只报计数/字节/哈希；内容只落盘。

用法:
    python -B -X utf8 dsh_adapter.py --session <session.v4.jsonl.zstd> --out <derived.jsonl>

产出:
    <derived.jsonl>              派生日志（memory.py 的 --path 指向它）
    <derived.jsonl>.state.json   本项目自己的水位（最后处理到哪个 seq）
    stdout                       JSON 读数（含可直接粘贴的 register 命令）

需要:
    一个能解 zstd 多帧的解码器（按优先级自动找）：node → zstd CLI → python zstandard 模块。
"""
import argparse
import collections
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
DECODER_MJS = HERE / "zstd_frames.mjs"

# 声明值：**说真话** —— 这是「产这份日志的东西是谁」，不是「像谁」。
#
# 它故意【不在】memory-pack 的允许表里，于是 register() 会拒绝：
#     unverified rollout version; adapt and test before registering
# **这个拒绝是设计要的行为**（上游 README §8：换宿主必须改 ADAPTER 与允许表）。
# ⇒ 正确顺序：先用 allowlist.patch 把允许表改成如实接受 DSH，再 register。
#   这样回执链的第一环才是【真话】。
#
# ⚠️ 曾经的做法（已废弃、已作废）：把它写成一具假前缀 "0.162.0+dsh-adapter-1" 靠伪装过闸。
#    那等于在回执链的第一环写一句假话 —— 对一个以「可核验」为卖点的包，
#    这是最不能省的一条。是台式机侧把它挡下来的。
CLI_VERSION_STAMP = "dsh-session-v4"

DROPPED_TYPES = (
    "system/message", "developer/message", "request/header", "request/context",
    "session/title", "session/title-llm-request", "step/start", "step/end",
    "turn/start", "turn/end", "agent/inbox/spliced", "compaction/start",
    "compaction/summary", "compaction/end", "compaction/prune", "goal/change",
    "todo/write", "workspace/changes", "deliverables/presented", "command/run",
    "command/done", "approval/asked", "approval/decided", "permission/preset",
    "sandbox/mode", "approval/policy", "session-log-deepseek/delivery-accepted",
    "session/end-seed",
)


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def iso(ms):
    if not isinstance(ms, (int, float)):
        return None
    return datetime.datetime.fromtimestamp(ms / 1000.0, datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------- 档案锁

_LOCK_HANDLES = []   # 持到进程退出；进程退出时由 OS 释放


def acquire_archive_lock(path):
    """对派生日志加独占锁。

    红队 P0-1（2026-10-17）：原先「读水位 → 组字节 → open("ab") 追加」全程无锁。
    3 进程并跑实测 6/6 复现：每轮多出 2 行 `session_meta`、8002 行逐字节重复，
    而且**三个进程 rc 全 0**。下游不会拦 —— memory.py 的事件 id 含 offset，
    重复行落在不同 offset 就成了"不同事件"。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+b")
    if f.tell() == 0:
        f.write(b"0")
        f.flush()
    f.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise SystemExit(
            "another run already holds the archive lock: " + str(path) + "\n"
            "  Concurrent runs would append the same records twice (and could write a second\n"
            "  session_meta line into the middle of the file). Refusing.\n"
            "  Retry after the other run finishes."
        )
    _LOCK_HANDLES.append(f)


# ---------------------------------------------------------------- 解码器发现

def find_node():
    env = os.environ.get("DSH_NODE")
    if env and Path(env).exists():
        return env
    w = shutil.which("node")
    if w:
        return w
    roots = []
    h = os.environ.get("DSH_HOME")
    if h:
        roots.append(Path(h))
    roots.append(Path.home() / ".dsh")
    for r in roots:
        if not r.is_dir():
            continue
        for pat in ("dsh-runtimes/*/dependencies/node/bin/node.exe",
                    "dsh-runtimes/*/dependencies/node/bin/node"):
            for p in sorted(r.glob(pat)):
                if p.exists():
                    return str(p)
    return None


def decode_to_jsonl(src, dst, allow_untested=False):
    """把 zstd 多帧日志解成原始 JSONL。返回 (解码器名, 读数 dict)。

    只有 **node** 这条路是本项目实测过的。另两条（zstd CLI / python zstandard）
    从未实测，且已知 python 那条默认只解第一帧 —— 所以它们**必须显式放行**。
    """
    notes = []

    node = find_node()
    if node:
        if not DECODER_MJS.exists():
            raise SystemExit("decoder missing: " + str(DECODER_MJS))
        r = subprocess.run([node, str(DECODER_MJS), str(src), str(dst)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise SystemExit("zstd_frames.mjs failed rc=%s: %s" % (r.returncode, (r.stderr or "").strip()))
        try:
            return "node:" + node, json.loads((r.stdout or "").strip().splitlines()[-1])
        except Exception:
            return "node:" + node, {}

    # ---- 以下两条回退路径【本机没有、从未实测】。
    # 红队评审（2026-10-17）指出：python-zstandard 的 stream_reader 默认
    # read_across_frames=False ⇒ 【只解第一帧】，也就是说这条回退路径会原样复现
    # "本工具存在的唯一理由"那个 bug，而且静默无声。
    # ⇒ 处理办法：两条都改成【显式选择加入】，默认硬失败。宁可让人装 node，
    #    也不能让人静默拿到 0.001%。
    if not allow_untested:
        raise SystemExit(
            "no *verified* zstd multi-frame decoder found.\n"
            "  verified path: node (DSH ships one; set DSH_NODE if not on PATH)\n"
            "  unverified alternates exist (zstd CLI / python 'zstandard'), but they are\n"
            "  NOT tested by this project and can silently decode only the FIRST frame.\n"
            "  If you knowingly accept that risk: --allow-untested-decoder\n"
            "\n"
            "  note: python stdlib CANNOT do this -- and the obvious APIs silently return\n"
            "        only the FIRST frame, so a partial decode looks like success."
        )

    zstd_cli = shutil.which("zstd")
    if zstd_cli:
        notes.append("zstd-cli-path-UNTESTED")
        notes.append("recovered_frames_unverified")
        with open(dst, "wb") as f:
            r = subprocess.run([zstd_cli, "-d", "-c", str(src)], stdout=f, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise SystemExit("zstd CLI failed rc=%s" % r.returncode)
        return "zstd-cli(UNTESTED):" + zstd_cli, {"notes": notes}

    try:
        import zstandard
    except ImportError:
        raise SystemExit(
            "no zstd multi-frame decoder found.\n"
            "  need node (DSH ships one). Tried node, zstd CLI, python 'zstandard'."
        )
    notes.append("python-zstandard-path-UNTESTED")
    import inspect
    sig = None
    try:
        sig = inspect.signature(zstandard.ZstdDecompressor.stream_reader)
    except Exception:
        pass
    if sig is not None and "read_across_frames" not in sig.parameters:
        raise SystemExit(
            "this python-zstandard is too old to guarantee multi-frame decoding\n"
            "  (stream_reader has no read_across_frames parameter). Refusing to guess."
        )
    notes.append("read_across_frames=True")
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        dctx = zstandard.ZstdDecompressor()
        # 必须显式 True：默认 False 会在第一帧末尾停下（且不报错）。
        with dctx.stream_reader(fi, read_across_frames=True) as reader:
            shutil.copyfileobj(reader, fo)
    return "python-zstandard(UNTESTED)", {"notes": notes}


# ---------------------------------------------------------------- 形状映射

def take_text(items):
    """从 content 项数组里取文本。返回 (text, n_image, n_other)。"""
    texts, n_image, n_other = [], 0, 0
    for it in items or []:
        if not isinstance(it, dict):
            n_other += 1
            continue
        k = it.get("type")
        if k == "text" and isinstance(it.get("text"), str):
            texts.append(it["text"])
        elif k == "image":
            n_image += 1
        else:
            n_other += 1
    return "\n".join(texts), n_image, n_other


def map_record(rec, stats):
    """DSH 记录 → 一条或多条 Codex 形状的 response_item；不适用则返回 []。"""
    t = rec.get("type")
    d = rec.get("data") or {}
    ts = iso(rec.get("time"))
    seq = rec.get("seq")
    common = {"timestamp": ts, "ordinal": seq}

    if t == "user/message":
        text, n_img, n_oth = take_text(d.get("content"))
        stats["images_skipped"] += n_img
        stats["other_content_items"] += n_oth
        if not text:
            stats["empty_text_skipped"] += 1
            return []
        return [dict(common, type="response_item", payload={
            "type": "message", "role": "user", "id": d.get("id"),
            "content": [{"type": "input_text", "text": text}]})]

    if t == "assistant/message":
        msg = d.get("message") or {}
        texts, n_img, n_oth = [], 0, 0
        for it in msg.get("content") or []:
            if not isinstance(it, dict):
                n_oth += 1
                continue
            k = it.get("type")
            if k == "text" and isinstance(it.get("text"), str):
                texts.append(it["text"])
            elif k == "reasoning":
                stats["reasoning_dropped"] += 1          # 包的 public() 按设计丢弃推理
            elif k == "tool-call":
                stats["toolcall_in_message_deduped"] += 1  # 另有独立 tool/call 记录，避免重复
            elif k == "image":
                n_img += 1
            else:
                n_oth += 1
        stats["images_skipped"] += n_img
        stats["other_content_items"] += n_oth
        if not texts:
            stats["empty_text_skipped"] += 1
            return []
        return [dict(common, type="response_item", payload={
            "type": "message", "role": "assistant", "id": msg.get("id"),
            "content": [{"type": "output_text", "text": "\n".join(texts)}]})]

    if t == "tool/call":
        return [dict(common, type="response_item", payload={
            "type": "function_call", "call_id": d.get("callId"),
            "name": d.get("name"), "arguments": d.get("arguments")})]

    if t == "tool/result":
        msg = d.get("message") or {}
        text, n_img, n_oth = take_text(msg.get("content"))
        stats["images_skipped"] += n_img
        stats["other_content_items"] += n_oth
        return [dict(common, type="response_item", payload={
            "type": "function_call_output", "call_id": msg.get("toolCallId"),
            "output": text})]

    if t == "session":
        return []                                  # 已变成首行 session_meta
    if t in DROPPED_TYPES:
        stats["dropped_by_design"] += 1
        return []
    stats["dropped_unknown_" + str(t)] += 1
    return []


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="DSH session log -> memory-pack derived log")
    ap.add_argument("--session", required=True, help="path to session.v4.jsonl.zstd")
    ap.add_argument("--out", required=True, help="derived JSONL path (this becomes memory.py --path)")
    ap.add_argument("--dry-run", action="store_true", help="decode + map, write nothing")
    ap.add_argument("--stamp", default=None,
                    help="覆盖 cli_version 声明值。默认说真话（%s）。"
                         "只有当你已经把允许表改成接受别的值时，才该用它。" % CLI_VERSION_STAMP)
    ap.add_argument("--allow-untested-decoder", action="store_true",
                    help="放行【未实测】的解码回退路径（zstd CLI / python zstandard）。"
                         "已知 python 那条默认只解第一帧 —— 除非你清楚风险，否则别开。")
    args = ap.parse_args()
    stamp = args.stamp or CLI_VERSION_STAMP

    src = Path(args.session).resolve()
    out = Path(args.out).resolve()
    state_path = out.with_name(out.name + ".state.json")
    if not src.is_file():
        raise SystemExit("session log not found: " + str(src))
    # 独占锁必须**早于**读水位 —— 竞争窗口就是"读水位 → 追加"这一段
    if not args.dry_run:
        acquire_archive_lock(out.with_name(out.name + ".lock"))

    tmp = Path(tempfile.mkdtemp(prefix="dsh-adapter-"))
    raw = tmp / "raw.jsonl"
    try:
        decoder, dec = decode_to_jsonl(src, raw, allow_untested=args.allow_untested_decoder)
        raw_bytes = raw.read_bytes()
        lines = [ln for ln in raw_bytes.decode("utf-8").split("\n") if ln]
        recs = [json.loads(ln) for ln in lines]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not recs or recs[0].get("type") != "session":
        raise SystemExit("first record is not 'session'; refusing to guess")

    sess = recs[0]
    sid = sess.get("id")
    cwd = sess.get("cwd")
    if not sid or not cwd:
        raise SystemExit("session record missing id/cwd")

    header = {"type": "session_meta",
              "payload": {"id": sid, "cwd": cwd, "cli_version": stamp}}
    header_line = (json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8")

    # 水位
    prev = {}
    if state_path.exists():
        prev = json.loads(state_path.read_text(encoding="utf-8"))
    since = int(prev.get("last_seq", -1))

    # ---- 档案完整性（红队 P0-5 / P0-6 / P1-3）：档案存在时，"只追加"必须被**验证**，不能只是假设
    if out.exists():
        size_now = out.stat().st_size
        first = out.read_bytes().split(b"\n", 1)[0]
        if first + b"\n" != header_line:
            raise SystemExit("derived log header changed; refusing to touch an existing archive")
        if not prev:
            raise SystemExit(
                "archive exists but its watermark (.state.json) is missing: " + str(state_path) + "\n"
                "  Refusing to continue -- without the watermark the whole session would be appended AGAIN\n"
                "  (duplicate records), and there is no way to tell how far we got.\n"
                "  Fix: restore the watermark file, or point --out at a NEW path and re-register."
            )
        recorded = prev.get("derived_bytes")
        if isinstance(recorded, int) and recorded != size_now:
            raise SystemExit(
                "derived log was modified/truncated outside this tool: expected %d B, found %d B\n"
                "  Refusing to append. (memory.py would report 'source truncated/replaced' later anyway --\n"
                "  better to fail here than to report success now.)\n"
                "  Fix: restore the archive from backup, or start a new archive and re-register."
                % (recorded, size_now)
            )
        if size_now and out.read_bytes()[-1:] != b"\n":
            raise SystemExit(
                "derived log does not end on a line boundary; appending now would produce a corrupt JSON line\n"
                "  (and the next sync would fail the whole source). Fix: repair the trailing partial line first."
            )

    stats = collections.Counter()
    items = []
    for rec in recs[1:]:
        seq = rec.get("seq")
        if isinstance(seq, int) and seq <= since:
            continue
        items.extend(map_record(rec, stats))

    # 组装要追加的字节（首行 header 已在上面校验过）
    buf = b""
    if not out.exists():
        buf += header_line
    for it in items:
        buf += (json.dumps(it, ensure_ascii=False) + "\n").encode("utf-8")

    last_seq = max([r.get("seq") for r in recs[1:] if isinstance(r.get("seq"), int)] or [since])

    if not args.dry_run and buf:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("ab") as f:
            f.write(buf)
            f.flush()
            os.fsync(f.fileno())
        state_path.write_text(json.dumps({
            "session_id": sid, "cwd": cwd, "cli_version_stamp": stamp,
            "last_seq": last_seq, "src_path": src.as_posix(), "src_bytes": src.stat().st_size,
            "derived": out.as_posix(), "derived_bytes": out.stat().st_size,
            "appended_bytes": len(buf), "appended_sha256": sha256_bytes(buf),
            "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif not args.dry_run:
        state_path.write_text(json.dumps({
            "session_id": sid, "cwd": cwd, "cli_version_stamp": stamp,
            "last_seq": last_seq, "src_path": src.as_posix(), "src_bytes": src.stat().st_size,
            "derived": out.as_posix(),
            "derived_bytes": out.stat().st_size if out.exists() else 0,
            "appended_bytes": 0, "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "session_id": sid, "cwd": cwd, "cli_version_stamp": stamp,
        "decoder": decoder, "decode_reading": dec,
        "dsh_records": len(recs), "dsh_lines": len(lines),
        "new_since_seq": since, "last_seq": last_seq,
        "mapped_items": len(items), "appended_bytes": len(buf),
        "dry_run": args.dry_run,
        "derived": out.as_posix(),
        "derived_header_line1": json.loads(header_line.decode("utf-8")),
        "counters": stats,
        "next_command": ('python -B -X utf8 "<PACKAGE>/memory.py" --root "<STATE_DIR>" register '
                         '--path "' + out.as_posix() + '" --session "' + str(sid) + '" '
                         '--cwd "' + str(cwd) + '" --from start'),
        "prerequisite": ("memory-pack 的默认允许表只认 0.162.*，会拒绝本声明值 "
                         "(unverified rollout version) —— **这是设计要的行为**。"
                         "先跑 apply_patch.py 把允许表改成如实接受 DSH，再 register。"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
