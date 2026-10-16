# -*- coding: utf-8 -*-
"""selfcheck.py — 端到端自检，**完全不需要任何真实会话**。

它自己造一份「DSH 形状的合成会话」，打包成 zstd 多帧，然后：
  ① 跑 zstd_frames.mjs 解码，核对帧数/字节/行数
  ② 跑 dsh_adapter.py 映射，核对条数与首行
  ③ 正向控制：该出现的话**必须**出现
  ④ 负向控制：私有哨兵（藏在 reasoning / system 里）**必须不出现**
  ⑤ 幂等：同一份源再跑一次，**追加 0 字节**
  ⑥ 增量：源里追加一条记录后，只追加那一条

用法: python -B -X utf8 selfcheck.py [--node <node.exe>]
退出码: 0 全过；1 有断言失败；2 环境不满足
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ADAPTER = HERE / "dsh_adapter.py"
PACKER = HERE / "zstd_pack.mjs"

sys.path.insert(0, str(HERE))
import dsh_adapter  # noqa: E402  （复用同一套 node 发现逻辑，避免两份实现漂移）

SENTINEL = "SELFCHECK_PRIVATE_SENTINEL_must_never_reach_the_derived_log"
SID = "synthetic-session-0001"
SID2 = "synthetic-session-0002"
CLI_STAMP = "dsh-session-v4"
LINES_PER_FRAME = 2

# 往一个已有 .zstd 末尾追加一个「半条」帧（模拟日志正在被写入）
JS_APPEND_PARTIAL = """import fs from 'node:fs';
import zlib from 'node:zlib';
const [zst, txtFile] = process.argv.slice(2);
const txt = fs.readFileSync(txtFile, 'utf8');
fs.appendFileSync(zst, zlib.zstdCompressSync(Buffer.from(txt, 'utf8')));
process.stdout.write(JSON.stringify({ appended_chars: txt.length }));
"""

# 造一个【内部含魔数】的帧：如果解码器靠"找 4 字节魔数"切帧，就会把它劈成两半。
# 实测这套参数能稳定造出来（320 次里 40 次命中）。
JS_MAKE_ADVERSARIAL = """import fs from 'node:fs';
import zlib from 'node:zlib';
const [payloadPath, zstPath] = process.argv.slice(2);
const n = 8192;
const buf = Buffer.alloc(n);
let x = 1 * 2654435761 % 4294967296;
for (let i = 0; i < n; i++) { x = (x * 1103515245 + 12345) % 2147483648; buf[i] = x & 0xff; }
Buffer.from([0x28, 0xb5, 0x2f, 0xfd]).copy(buf, 1037);   // 埋一个魔数进去
buf[n - 1] = 0x0a;                                        // 结尾给个换行，免得被"半条"规则丢掉
fs.writeFileSync(payloadPath, buf);
const comp = zlib.zstdCompressSync(buf, { params: { [zlib.constants.ZSTD_c_compressionLevel]: -3 } });
fs.writeFileSync(zstPath, comp);
process.stdout.write(JSON.stringify({
  payload_bytes: n, frame_bytes: comp.length,
  magic_occurrences_in_frame: comp.toString('latin1').split('\\u0028\\u00b5\\u002f\\u00fd').length - 1
}));
"""

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  — " + str(detail)) if detail else ""))


def find_node(explicit):
    if explicit:
        return explicit
    return dsh_adapter.find_node()


def dsh_record(seq, rtype, data, extra=None):
    r = {"type": rtype, "seq": seq, "time": 1700000000000 + seq * 1000, "data": data}
    if extra:
        r.update(extra)
    return r


def build_source(cwd, with_extra=False, sid=None):
    """造一份 DSH 形状的合成日志（结构照实机实测的形状写）。"""
    sid = sid or SID
    recs = [
        {"type": "session", "version": 4, "id": sid, "createdAt": 1700000000000,
         "cwd": cwd, "isSeeded": False, "delegationDepth": 0, "agentPreset": "default"},
        dsh_record(0, "user/message", {
            "content": [{"type": "text", "text": "冷启动：现在到哪一步了？"}],
            "source": {"kind": "rpc", "rpcId": "r1"}, "role": "user", "id": "u1"}),
        dsh_record(1, "assistant/message", {
            "turn": 0, "step": 0,
            "message": {"role": "assistant", "id": "a1", "source": {"kind": "model"}, "content": [
                {"type": "reasoning", "text": SENTINEL},              # 私有 → 必须丢
                {"type": "text", "text": "到「写入并回读」这一步。"},
                {"type": "tool-call", "id": "c1", "name": "shell", "arguments": "{}"}]}}),
        dsh_record(2, "tool/call", {
            "turn": 0, "step": 0, "callId": "c1", "name": "shell", "arguments": "echo hello"}),
        dsh_record(3, "tool/result", {
            "turn": 0, "step": 0, "message": {
                "role": "tool", "source": {"kind": "rpc", "callId": "c1"}, "toolCallId": "c1",
                "content": [{"type": "text", "text": "hello"}], "isError": False, "id": "t1"}}),
        dsh_record(4, "system/message", {
            "turn": 0, "step": 0, "message": {
                "role": "system", "source": {"kind": "system"}, "id": "s1",
                "content": [{"type": "text", "text": SENTINEL}]}}),   # 私有 → 必须丢
        dsh_record(5, "todo/write", {"todos": []}),
    ]
    if with_extra:
        recs.append(dsh_record(6, "user/message", {
            "content": [{"type": "text", "text": "增量：这条是后加的。"}],
            "source": {"kind": "rpc", "rpcId": "r2"}, "role": "user", "id": "u2"}))
    return recs


def write_jsonl(path, recs):
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def run_adapter(node, src, out):
    env = dict(os.environ)
    if node:
        env["DSH_NODE"] = node
    p = subprocess.run([sys.executable, "-B", "-X", "utf8", str(ADAPTER),
                        "--session", str(src), "--out", str(out)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if p.returncode != 0:
        return None, (p.stderr or p.stdout)
    return json.loads(p.stdout), ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default=None)
    args = ap.parse_args()

    node = find_node(args.node)
    if not node:
        print("环境不满足：找不到 node（多帧解码需要它）。可用 --node 指定，或设 DSH_NODE。")
        return 2
    print("node =", node)

    tmp = Path(tempfile.mkdtemp(prefix="dsh-adapter-selfcheck-"))
    try:
        src_jsonl = tmp / "synthetic.jsonl"
        src_zst = tmp / "synthetic.v4.jsonl.zstd"
        out = tmp / "derived.jsonl"

        # ---- ① 造合成源 + 多帧打包
        print("\n① 合成源 + zstd 多帧打包")
        write_jsonl(src_jsonl, build_source(tmp.as_posix()))
        p = subprocess.run([node, str(PACKER), str(src_jsonl), str(src_zst), str(LINES_PER_FRAME)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        check("打包器 rc=0", p.returncode == 0, (p.stderr or "").strip())
        pack = json.loads(p.stdout)
        check("确实造出了多帧", pack["frames"] == 4, "frames=%d lines=%d" % (pack["frames"], pack["lines"]))

        # ---- ② 跑转接头
        print("\n② 转接头（第一次）")
        reading, err = run_adapter(node, src_zst, out)
        check("转接头 rc=0", reading is not None, err)
        if reading is None:
            return 1
        dec = reading["decode_reading"]
        check("解码：帧全部解出", dec["frames"] == dec["frames_decoded"] == 4,
              "%d/%d" % (dec["frames_decoded"], dec["frames"]))
        check("解码：没有丢尾帧", dec["truncated_tail_frame_dropped"] is False)
        check("映射条数 = 4", reading["mapped_items"] == 4, reading["mapped_items"])
        check("丢掉 2 条（system + todo）", reading["counters"].get("dropped_by_design") == 2,
              reading["counters"].get("dropped_by_design"))
        check("reasoning 被丢 1 条", reading["counters"].get("reasoning_dropped") == 1)

        derived = out.read_bytes()
        lines = [l for l in derived.decode("utf-8").split("\n") if l]
        head = json.loads(lines[0])
        check("首行 type=session_meta", head.get("type") == "session_meta")
        check("首行 id 正确", head["payload"]["id"] == SID)
        check("首行 cli_version 戳正确", head["payload"]["cli_version"] == CLI_STAMP)
        check("声明值是【真话】且故意不在默认允许表里（fail closed）",
              CLI_STAMP == "dsh-session-v4" and not CLI_STAMP.startswith("0.162."))
        check("派生日志共 5 行（1 头 + 4 条）", len(lines) == 5, len(lines))
        check("其余行都是 response_item",
              all(json.loads(l)["type"] == "response_item" for l in lines[1:]))

        # ---- ③ 正向控制
        print("\n③ 正向控制：该出现的话必须出现")
        check("用户那句话在", "冷启动：现在到哪一步了？" in derived.decode("utf-8"))
        check("助手那句话在", "到「写入并回读」这一步。" in derived.decode("utf-8"))
        check("工具调用在", '"type": "function_call"' in derived.decode("utf-8"))
        check("工具结果在", '"type": "function_call_output"' in derived.decode("utf-8"))

        # ---- ④ 负向控制
        print("\n④ 负向控制：私有哨兵必须不出现")
        check("哨兵【不在】派生日志里", SENTINEL not in derived.decode("utf-8"))
        check("哨兵确实在源里（控制组有效）", SENTINEL in src_jsonl.read_text(encoding="utf-8"))

        # ---- ⑤ 幂等
        print("\n⑤ 幂等：同一份源再跑一次")
        before = out.stat().st_size
        reading2, _ = run_adapter(node, src_zst, out)
        check("第二次追加 0 字节", reading2 and reading2["appended_bytes"] == 0,
              reading2 and reading2["appended_bytes"])
        check("文件长度没变", out.stat().st_size == before)

        # ---- ⑥ 增量
        print("\n⑥ 增量：源里追加一条后只追加那一条")
        write_jsonl(src_jsonl, build_source(tmp.as_posix(), with_extra=True))
        subprocess.run([node, str(PACKER), str(src_jsonl), str(src_zst), str(LINES_PER_FRAME)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
        reading3, _ = run_adapter(node, src_zst, out)
        check("第三次只追加 1 条", reading3 and reading3["mapped_items"] == 1,
              reading3 and reading3["mapped_items"])
        after_lines = [l for l in out.read_bytes().decode("utf-8").split("\n") if l]
        check("派生日志变成 6 行", len(after_lines) == 6, len(after_lines))
        check("新那句话在", "增量：这条是后加的。" in out.read_text(encoding="utf-8"))
        check("首行没被改写", json.loads(after_lines[0]) == head)

        # ---- ⑦ 截断尾帧：必须【真的丢掉】半条，而且不许崩
        print("\n⑦ 截断尾帧（日志正在被写）：必须丢掉半条，且不许崩")
        helper = tmp / "append_partial.mjs"
        helper.write_text(JS_APPEND_PARTIAL, encoding="utf-8")
        partial = '{"type":"response_item","payload":{"type":"message","role":"user","con'
        pf = tmp / "partial.txt"
        pf.write_text(partial, encoding="utf-8", newline="")
        p = subprocess.run([node, str(helper), str(src_zst), str(pf)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        check("半帧已追加到源上", p.returncode == 0, (p.stderr or "").strip())
        r4, err4 = run_adapter(node, src_zst, out)
        check("有半帧时转接头不崩（rc=0）", r4 is not None, err4)
        if r4:
            check("半帧被标记为丢弃", r4["decode_reading"]["truncated_tail_frame_dropped"] is True)
            check("半帧没有混进派生日志", partial not in out.read_text(encoding="utf-8"))
            check("半帧不产生新条目", r4["appended_bytes"] == 0, r4["appended_bytes"])
        # 复原：重新打包一份干净的源
        write_jsonl(src_jsonl, build_source(tmp.as_posix(), with_extra=True))
        subprocess.run([node, str(PACKER), str(src_jsonl), str(src_zst), str(LINES_PER_FRAME)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")

        # ---- ⑧ 已有档案 + 换了来源 ⇒ 必须拒绝改写
        print("\n⑧ 已有派生日志 + 换个来源写同一路径：必须拒绝")
        src2_jsonl = tmp / "synthetic2.jsonl"
        src2_zst = tmp / "synthetic2.v4.jsonl.zstd"
        write_jsonl(src2_jsonl, build_source(tmp.as_posix(), sid=SID2))
        subprocess.run([node, str(PACKER), str(src2_jsonl), str(src2_zst), str(LINES_PER_FRAME)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
        size_before = out.stat().st_size
        env = dict(os.environ)
        env["DSH_NODE"] = node
        p = subprocess.run([sys.executable, "-B", "-X", "utf8", str(ADAPTER),
                            "--session", str(src2_zst), "--out", str(out)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        check("换来源时非 0 退出", p.returncode != 0, p.returncode)
        check("报的是 header 变了", "header changed" in (p.stderr or ""), (p.stderr or "").strip()[:120])
        check("档案一个字节没被改", out.stat().st_size == size_before)

        # ---- ⑨ --dry-run 不写文件
        print("\n⑨ --dry-run：不写任何文件")
        dry_out = tmp / "dry.jsonl"
        p = subprocess.run([sys.executable, "-B", "-X", "utf8", str(ADAPTER),
                            "--session", str(src2_zst), "--out", str(dry_out), "--dry-run"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        check("--dry-run rc=0", p.returncode == 0, (p.stderr or "").strip()[:120])
        check("没有生成派生日志", not dry_out.exists())
        check("也没有生成 state.json", not dry_out.with_name(dry_out.name + ".state.json").exists())
        if p.returncode == 0:
            check("但仍然算了映射条数", json.loads(p.stdout)["mapped_items"] == 4,
                  json.loads(p.stdout)["mapped_items"])

        # ---- ⑩ --stamp 覆盖声明值
        print("\n⑩ --stamp：覆盖声明值（给已把允许表改成别样的人用）")
        st_out = tmp / "stamped.jsonl"
        p = subprocess.run([sys.executable, "-B", "-X", "utf8", str(ADAPTER),
                            "--session", str(src2_zst), "--out", str(st_out), "--stamp", "custom-stamp-9"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        check("--stamp rc=0", p.returncode == 0, (p.stderr or "").strip()[:120])
        if p.returncode == 0:
            head2 = json.loads(st_out.read_text(encoding="utf-8").split("\n")[0])
            check("首行用了覆盖值", head2["payload"]["cli_version"] == "custom-stamp-9",
                  head2["payload"]["cli_version"])
            check("state.json 也记覆盖值",
                  json.loads((st_out.with_name(st_out.name + ".state.json")).read_text(
                      encoding="utf-8"))["cli_version_stamp"] == "custom-stamp-9")

        # ---- ⑪ apply_patch 对哈希不匹配的副本必须拒
        print("\n⑪ apply_patch：来源哈希对不上时必须拒绝（不猜、不改）")
        fake = tmp / "fake-pkg"
        fake.mkdir()
        (fake / "memory.py").write_text("# not the real memory-pack\n", encoding="utf-8")
        p = subprocess.run([sys.executable, "-B", "-X", "utf8", str(HERE / "apply_patch.py"),
                            "--package", str(fake)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        check("哈希不匹配时 rc=2", p.returncode == 2, p.returncode)
        check("报的是拒绝", "拒绝" in (p.stdout or ""), (p.stdout or "").strip()[-120:])
        check("假文件没被改", (fake / "memory.py").read_text(encoding="utf-8") == "# not the real memory-pack\n")

        # ---- ⑫ 帧内部含魔数：结构扫描必须不受骗
        print("\n⑫ 帧内部含魔数（魔数扫描会把它劈开）：结构扫描必须不受骗")
        adv_helper = tmp / "make_adversarial.mjs"
        adv_helper.write_text(JS_MAKE_ADVERSARIAL, encoding="utf-8")
        payload = tmp / "adv-payload.bin"
        adv_zst = tmp / "adv.zstd"
        p = subprocess.run([node, str(adv_helper), str(payload), str(adv_zst)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        check("对抗样本已造出", p.returncode == 0, (p.stderr or "").strip()[:160])
        if p.returncode == 0:
            info = json.loads(p.stdout)
            raw = adv_zst.read_bytes()
            n_magic = sum(1 for i in range(len(raw) - 3)
                          if raw[i:i + 4] == b"\x28\xb5\x2f\xfd")
            check("这个帧内部确实含魔数（≥2 次出现）", n_magic >= 2, "魔数出现 %d 次" % n_magic)

            adv_out = tmp / "adv-out.bin"
            r = subprocess.run([node, str(HERE / "zstd_frames.mjs"), str(adv_zst), str(adv_out)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            check("含魔数的帧解码 rc=0", r.returncode == 0, (r.stderr or "").strip()[:160])
            if r.returncode == 0:
                rd = json.loads(r.stdout)
                check("结构扫描只认出 1 个帧", rd["structurally_complete_frames"] == 1,
                      rd["structurally_complete_frames"])
                check("解出的字节数与原样一致", rd["bytes_out"] == info["payload_bytes"],
                      "%s vs %s" % (rd["bytes_out"], info["payload_bytes"]))
                same = (hashlib.sha256(adv_out.read_bytes()).hexdigest()
                        == hashlib.sha256(payload.read_bytes()).hexdigest())
                check("解出的内容逐字节等于原始载荷", same)

        # ---- 以下 ⑬~⑰ 全部来自独立红队评审（2026-10-17）的 P0 发现，逐条补回归 ----

        # ---- ⑬ 档案被外部截断 ⇒ 必须拒绝（不能报成功）
        print("\n⑬ 派生日志被外部改动：必须拒绝，不能报成功")
        saved_archive = out.read_bytes()
        out.write_bytes(saved_archive[: max(10, len(saved_archive) // 2)])
        r7, err7 = run_adapter(node, src_zst, out)
        check("被截断后 adapter 必须非 0 退出", r7 is None, (err7 or "").strip()[:110])
        check("报的是『被本工具之外改过』",
              "modified/truncated outside this tool" in (err7 or ""), (err7 or "").strip()[:140])
        out.write_bytes(saved_archive)
        check("复原后档案与 state 一致", out.stat().st_size == len(saved_archive))

        # ---- ⑭ 水位文件丢了但档案还在 ⇒ 必须拒绝（否则整份重复追加）
        print("\n⑭ 水位丢失但档案还在：必须拒绝（否则整份会话重复追加）")
        st = out.with_name(out.name + ".state.json")
        saved_state = st.read_bytes()
        st.unlink()
        r8, err8 = run_adapter(node, src_zst, out)
        check("缺水位时必须非 0 退出", r8 is None, (err8 or "").strip()[:110])
        check("报的是水位缺失", "watermark" in (err8 or ""), (err8 or "").strip()[:140])
        check("档案没被追加（长度不变）", out.stat().st_size == len(saved_archive))
        st.write_bytes(saved_state)

        # ---- ⑮ 档案锁：别人持锁时第二个进程必须被拒
        print("\n⑮ 档案锁：别人持锁时，第二个进程必须被拒")
        lock_path = out.with_name(out.name + ".lock")
        lf = open(lock_path, "a+b")
        locked = False
        try:
            if lf.tell() == 0:
                lf.write(b"0")
                lf.flush()
            lf.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            print("      （本机不支持同进程测试加锁：%s）" % exc)
        if locked:
            r9, err9 = run_adapter(node, src_zst, out)
            check("持锁期间第二个进程必须非 0 退出", r9 is None, (err9 or "").strip()[:110])
            check("报的是锁冲突", "archive lock" in (err9 or ""), (err9 or "").strip()[:140])
            try:
                lf.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        lf.close()

        # ---- ⑯ verify_derived 在「什么都没验」时不许印 VERIFY OK
        print("\n⑯ verify_derived：空收件时必须判不通过（判据不许恒真）")
        empty_state = tmp / "empty-state"
        (empty_state / "runtime" / "inbox").mkdir(parents=True)
        (empty_state / "runtime" / "sources.json").write_text(
            json.dumps({"adapter": "x", "sources": []}), encoding="utf-8")
        r10 = subprocess.run([sys.executable, "-B", "-X", "utf8", str(HERE / "verify_derived.py"),
                              "--state", str(empty_state)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        check("空收件时 rc=2", r10.returncode == 2, r10.returncode)
        check("印的是 VACUOUS 而不是 OK",
              "VERIFY VACUOUS" in r10.stdout and "VERIFY OK" not in r10.stdout)

        # ---- ⑰ publish_check 在「内容类没跑」时不许印 CLEAN
        print("\n⑰ publish_check：缺会话目录时必须判『不完整』，不许印 CLEAN")
        r11 = subprocess.run([sys.executable, "-B", "-X", "utf8", str(HERE / "publish_check.py"),
                              "--release", str(HERE), "--sessions", str(tmp / "no-such-sessions")],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        check("缺会话目录时 rc=2", r11.returncode == 2, r11.returncode)
        check("印的是 INCOMPLETE 而不是 CLEAN",
              "PUBLISH-CHECK INCOMPLETE" in r11.stdout and "PUBLISH-CHECK CLEAN" not in r11.stdout)

        # ---- 汇总
        bad = [r for r in results if not r[1]]
        print("\n" + "=" * 56)
        print("自检：%d 项，通过 %d，失败 %d" % (len(results), len(results) - len(bad), len(bad)))
        for name, _, detail in bad:
            print("  失败：", name, detail)
        print("SELFCHECK " + ("OK" if not bad else "FAILED"))
        return 0 if not bad else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
