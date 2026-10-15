# -*- coding: utf-8 -*-
"""The ONE place that runs the cold path: the four README steps + a package-integrity proof.

Called by START-HERE.cmd (Windows) and start-here.sh (macOS/Linux) *after* they
have located a real interpreter. This module never searches for one, so exactly
one place in the tree decides "is this a real Python" -- the shell.

Everything the demo writes goes into a scratch directory OUTSIDE the package,
and the last check re-reads every package file and compares it with the bytes
taken before the run. That comparison -- not "it printed no error" -- is the
judge.
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
PY = sys.executable
BASE = [PY, "-B", "-X", "utf8"]
FAILS = []


def hdr(text):
    print("")
    print("=" * 70)
    print(text)
    print("=" * 70)


def ok(text):
    print("  [OK]   " + text)


def bad(text):
    print("  [FAIL] " + text)
    FAILS.append(text)


def note(text):
    print("         " + text)


def inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def fingerprint(root):
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def run(args, label, expect=0, must_contain=None):
    cmd = BASE + [str(a) for a in args]
    proc = subprocess.run(cmd, cwd=str(PKG), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = proc.stdout.decode("utf-8", "replace")
    good = (proc.returncode == expect)
    if must_contain is not None and must_contain not in out:
        good = False
    if good:
        ok("%-52s rc=%d" % (label, proc.returncode))
    else:
        bad("%-52s rc=%d (expected %d)" % (label, proc.returncode, expect))
        for line in out.strip().splitlines()[-6:]:
            note(line[:150])
    return good, out


def main():
    hdr("memory-pack - 冷路径自检")
    note("说明：只跑合成演示数据，不碰任何真实会话；状态全部落在本包之外。")
    note("解释器：" + PY)
    note("包目录：" + str(PKG))

    # Same lesson as the shell probe: an unwritable or unsuitable scratch area
    # must NOT be reported as "the demo cannot run". Try, then fall back.
    state = None
    for kwargs in ({}, {"dir": str(PKG.parent)}):
        try:
            candidate = Path(tempfile.mkdtemp(prefix="memory-pack-demo-", **kwargs))
        except OSError:
            continue
        if inside(candidate, PKG):
            shutil.rmtree(candidate, ignore_errors=True)
            continue
        state = candidate
        if kwargs:
            note("系统临时目录不可用（或落在包内），已改用包的父目录。")
        break
    if state is None:
        print("  无法在包外创建临时目录；请手动指定一个可写目录后重试。")
        return 1
    note("状态目录：" + str(state))
    note("          在包外 = " + str(not inside(state, PKG)) + "（这一条必须为 True）")

    before = fingerprint(PKG)

    src = state / "fixture-source.jsonl"
    init = state / "candidate-initial.json"
    badc = state / "candidate-bad.json"
    card = state / "outputs" / "memory-card.md"

    hdr("§2  造合成 fixture（合成数据，不是任何真实会话）")
    run([PKG / "fixture" / "make_fixture.py", state], "make_fixture.py")

    hdr("§3①  写入并回读")
    run([PKG / "memory.py", "--root", state, "register", "--path", src,
         "--session", "fixture-session-0001", "--cwd", state, "--from", "start"], "register")
    run([PKG / "memory.py", "--root", state, "publish", "--input", init,
         "--expected", "0", "--actor", "bootstrap"], "publish 第 1 版")

    hdr("§3②  中断 / 恢复 / 不许留旧绿灯")
    run([PKG / "memory.py", "--root", state, "resume"], "resume")
    run([PKG / "memory.py", "--root", state, "check"], "check")
    if card.exists():
        card.unlink()
        ok("已删掉唯一卡 -- 下一步必须变红，留旧绿灯就是错")
    else:
        bad("找不到唯一卡，删卡这一步做不了")
    run([PKG / "memory.py", "--root", state, "check"], "check 删卡后（必须 rc=2）", expect=2)
    run([PKG / "memory.py", "--root", state, "repair"], "repair")
    run([PKG / "memory.py", "--root", state, "check"], "check 修复后")

    hdr("§3③  被拒的候选 / 迟到的候选")
    run([PKG / "memory.py", "--root", state, "publish", "--input", badc,
         "--expected", "1", "--actor", "owner"], "坏候选（必须 rc=2）", expect=2)
    run([PKG / "memory.py", "--root", state, "publish", "--input", init,
         "--expected", "0", "--actor", "owner"], "迟到候选（必须 rc=2）", expect=2)

    hdr("§3④  原文追溯（独立核验器，它不 import memory.py）")
    run([PKG / "memory.py", "--root", state, "pending"], "pending")
    run([PKG / "fixture" / "verify.py", state], "fixture/verify.py", expect=0, must_contain="VERIFY OK")

    hdr("§4  包自带测试")
    run([PKG / "test_memory.py"], "test_memory.py")
    run([PKG / "test_automation.py"], "test_automation.py")

    hdr("§5  包有没有被改动（这条才是判据，不是「没报错」）")
    after = fingerprint(PKG)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    if not (added or removed or changed):
        ok("包目录 %d 个文件，跑完与跑前逐字节全等" % len(before))
    else:
        bad("包目录被改动了：新增 %s / 删除 %s / 改动 %s" % (added, removed, changed))

    hdr("结果")
    if FAILS:
        print("  有 %d 项没对上：" % len(FAILS))
        for f in FAILS:
            print("     - " + f)
        print("")
        print("  这多半不是你的错，是环境差异。请把上面整屏原文贴回给我们。")
        print("  状态目录（可整个删掉）：" + str(state))
        return 1

    print("  四步全过；VERIFY OK；包目录逐字节未变。")
    print("")
    print("  接下来请你回三行（这是这个包最想要的反馈）：")
    print("    1) 我用它干了什么活：")
    print("    2) 哪一步卡了（没卡就写「没卡」）：")
    print("    3) 第二天我又打开它了吗（打开了 / 没有 / 忘了）：")
    print("")
    print("  上面这个状态目录可以随时整个删掉 -- 它全在包外：")
    print("    " + str(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
