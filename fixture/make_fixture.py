# -*- coding: utf-8 -*-
"""Rebuildable synthetic fixture for memory-pack. No real session data, ever.

Usage:
    python -B -X utf8 fixture/make_fixture.py <STATE_DIR>

Writes (deterministic -- running it twice gives byte-identical files):
    <STATE_DIR>/fixture-source.jsonl     synthetic rollout log (the "native log")
    <STATE_DIR>/candidate-initial.json   a valid first candidate state (version 0)
    <STATE_DIR>/candidate-bad.json       same, but it rewrites an accepted decision
                                         (used to show that a rejected publish leaves
                                          no partial state)

The synthetic log deliberately contains three kinds of records that must NEVER
reach the inbox (reasoning / developer message / assistant analysis channel).
They all carry the same sentinel string, so fixture/verify.py can prove the
inbox is clean by looking for one token.
"""
import json
import pathlib
import sys

SENTINEL = "FIXTURE_PRIVATE_SENTINEL_must_never_reach_the_inbox"
SESSION_ID = "fixture-session-0001"
CLI_VERSION = "0.162.0"


def build(state_dir):
    state_dir = pathlib.Path(state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)

    meta = {"type": "session_meta",
            "payload": {"id": SESSION_ID, "cwd": state_dir.as_posix(), "cli_version": CLI_VERSION}}
    rows = [
        meta,
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "冷启动：现在到哪一步了？"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "到「写入并回读」这一步。"}]}},
        # --- the three that must be dropped by memory.public() ---
        {"type": "response_item", "payload": {"type": "reasoning", "text": SENTINEL}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                              "content": [{"type": "input_text", "text": SENTINEL}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "analysis",
                                              "content": [{"type": "output_text", "text": SENTINEL}]}},
        # --- one tool call / result pair, kept as evidence ---
        {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "call-1",
                                              "name": "shell", "input": "echo fixture"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call-1",
                                              "output": "fixture"}},
    ]
    log = state_dir / "fixture-source.jsonl"
    with log.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    initial = {
        "goal": "演示：把当前状态保存成唯一入口，并能核验地恢复",
        "snapshot": "fixture（合成数据，不是任何真实会话）",
        "constraints": ["只使用本目录内的合成数据"],
        "decisions": [{"id": "D1", "text": "当前状态只由 publish 生成，唯一入口是 outputs/memory-card.md",
                       "source": "fixture"}],
        "tasks": [{"id": "T1", "title": "走通保存与恢复", "owner": "owner", "status": "in_progress",
                   "source": "fixture", "next": "运行 resume，查看未处理来源"}],
        "current": {"owner": "T1"},
        "waiting": ["暂无"],
        "details": [],
        "coverage": [],
    }
    bad = json.loads(json.dumps(initial, ensure_ascii=False))
    bad["decisions"][0]["text"] = "试图改写一条已经接受的决定"
    for name, data in (("candidate-initial.json", initial), ("candidate-bad.json", bad)):
        p = state_dir / name
        with p.open("w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    print("session_id  = %s" % SESSION_ID)
    print("cli_version = %s" % CLI_VERSION)
    print("cwd         = %s" % state_dir.as_posix())
    print("wrote       = %s" % log.name)
    print("wrote       = candidate-initial.json")
    print("wrote       = candidate-bad.json")
    print("sentinel    = %s" % SENTINEL)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__.strip())
        raise SystemExit(2)
    raise SystemExit(build(sys.argv[1]))
