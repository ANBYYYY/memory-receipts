# -*- coding: utf-8 -*-
"""apply_patch.py — 把 memory-pack 的**允许表**改成如实接受 DSH（就两行）。

## 为什么必须打这个补丁（而不是让转接头假报版本）

`memory.py` 的 `register()` 有条硬闸：
    if not str(meta.get("cli_version", "")).startswith("0.162."):
        raise ValueError("unverified rollout version; adapt and test before registering")

DSH 的真实版本不是 `0.162.*`。想在**不改包**的前提下过闸，就只剩一条路：
让转接头**自称是 Codex**（例如把声明值写成 `0.162.0+dsh-adapter-1`）。

**那等于在回执链的第一环写一句假话。** 对一个以「可核验」为卖点的包，这条最不能省。
而上游 README §8 本来就写着：**换宿主必须改 `ADAPTER` 与允许表** —— 也就是说，
改这里不是破坏契约，**这就是契约给的扩展点**。

所以正确做法：**上游仓库一个字不动**，在**你自己的一份副本**上打这个两行补丁，
让第一环说真话。补丁把改了什么、原文件哈希、新文件哈希都打出来。

## 改哪两行（只有这两行）

| 行 | 原 | 新 |
|---|---|---|
| L17 | `ADAPTER = "codex-rollout-0.162-response-item-v1"` | `ADAPTER = "dsh-session-v4-response-item-v1"` |
| L137 | `startswith("0.162.")` | `in {"dsh-session-v4", "0.162.0"}` |

`ADAPTER` 也要改：它会被写进 `sources.json` 与每个收件批次 —— 只改闸门不改它，
`source.json` 里就会留一句"这份来源的适配器是 codex-rollout-…"，**那还是假话**。

## 用法

    python -B -X utf8 apply_patch.py --package <你的 memory-pack 副本目录> [--check-only]

退出码: 0 成功/检查通过；2 拒绝（哈希不匹配或没找到目标行）
**幂等**：已经是打过补丁的状态 ⇒ 报告"已是补丁态"，rc=0。
"""
import argparse
import hashlib
from pathlib import Path
import sys

# 本补丁只认这一个原始版本。对不上就拒绝 —— 不猜、不模糊匹配。
EXPECTED_PRISTINE_SHA256 = "7a5e99ee9695cfead1ab0b0d529ff391bb1091b6e3dc74e67968db209fe34f6d"
PRISTINE_SIZE = 31872

OLD_ADAPTER = 'ADAPTER = "codex-rollout-0.162-response-item-v1"'
NEW_ADAPTER = 'ADAPTER = "dsh-session-v4-response-item-v1"'
OLD_GATE = 'if not str(meta.get("cli_version", "")).startswith("0.162."):'
NEW_GATE = 'if str(meta.get("cli_version", "")) not in {"dsh-session-v4", "0.162.0"}:  # DSH adapter patch'


def sha(b):
    return hashlib.sha256(b).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", required=True, help="memory-pack 的目录（你自己的副本）")
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()

    mem = Path(args.package).resolve() / "memory.py"
    if not mem.is_file():
        print("找不到 %s" % mem)
        return 2

    raw = mem.read_bytes()
    text = raw.decode("utf-8")
    before = sha(raw)
    print("目标      : %s" % mem)
    print("原大小    : %d B" % len(raw))
    print("原 sha256 : %s" % before)

    already = (NEW_ADAPTER in text) and (NEW_GATE in text)
    if already:
        print("\n状态      : **已是补丁态**（幂等，不重复改）")
        print("新 sha256 : %s" % before)
        return 0

    if before.lower() != EXPECTED_PRISTINE_SHA256:
        print("\n拒绝：sha256 与本补丁认的原始版本不一致 —— 不猜、不改。")
        print("      期望 %s" % EXPECTED_PRISTINE_SHA256)
        print("      这可能是上游已更新，或这份副本已被别的东西改过。")
        return 2

    if OLD_ADAPTER not in text or OLD_GATE not in text:
        print("\n拒绝：没找到要改的那两行原文。")
        print("      ADAPTER 行: %s" % ("在" if OLD_ADAPTER in text else "缺失"))
        print("      闸门行    : %s" % ("在" if OLD_GATE in text else "缺失"))
        return 2

    print("\n将要改的（只有两行）：")
    for i, line in enumerate(text.split("\n"), 1):
        # ⚠️ 闸门行在原文件里是【缩进】的，必须比 strip 后的内容 ——
        # 否则这份"将要改的"清单会少列一行，审计的人就会以为只改了一处。
        if line.strip() in (OLD_ADAPTER.strip(), OLD_GATE.strip()):
            print("  L%d: %s" % (i, line))

    if args.check_only:
        print("\n--check-only：只检查，不写。可以打。")
        return 0

    new_text = text.replace(OLD_ADAPTER, NEW_ADAPTER).replace(OLD_GATE, NEW_GATE)
    new_raw = new_text.encode("utf-8")

    # 备份（可回滚）
    backup = mem.with_name("memory.py.prepatch-backup")
    if not backup.exists():
        backup.write_bytes(raw)
        print("\n已备份原文件 -> %s" % backup.name)

    mem.write_bytes(new_raw)
    after = sha(new_raw)
    print("\n已打补丁。")
    print("新大小    : %d B  (原 %d B)" % (len(new_raw), len(raw)))
    print("新 sha256 : %s" % after)
    print("改了几行  : 2")
    print("\n回滚一句话：把 %s 覆盖回 memory.py" % backup.name)
    print("复算一句话：python -B -X utf8 %s --package %s" % (Path(__file__).name, Path(args.package).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
