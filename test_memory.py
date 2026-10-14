"""Adversarial tests in disposable workspace-local fixtures, no global changes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import memory

PUBLISHER, WORKER, BOOTSTRAP = memory.PUBLISHER, memory.WORKER, memory.BOOTSTRAP


def scratch_root():
    """Return a directory for disposable fixtures that is NEVER inside the package.

    tempfile falls back to the current working directory when no system temp dir
    is writable (measured on a sandboxed shell: the user Temp dir was denied, so
    TemporaryDirectory() silently used the package dir itself). A test run must
    not leave anything behind in the package, so re-check the candidate and fall
    back to the package's parent directory.
    """
    pkg = Path(__file__).resolve().parent
    for candidate in (tempfile.gettempdir(), pkg.parent):
        path = Path(candidate).resolve()
        if path != pkg and not path.is_relative_to(pkg):
            return str(path)
    raise RuntimeError("no scratch directory available outside the package")


def state():
    return {"goal": "恢复测试", "snapshot": "fixture", "constraints": ["不重派已完成的任务"],
            "decisions": [{"id": "D1", "text": "T0 已完成，owner 做 T1", "source": "test-input"}],
            "tasks": [{"id": "T0", "title": "已完成的任务", "owner": PUBLISHER, "status": "completed", "source": "test-input", "next": "无需重做"},
                      {"id": "T1", "title": "当前任务", "owner": PUBLISHER, "status": "pending", "source": "test-input", "next": "核实入口"}],
            "current": {PUBLISHER: "T1", WORKER: None}, "waiting": ["暂无"], "details": [], "coverage": []}


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=scratch_root())
        self.root = Path(self.tmp.name)
        self.mem = memory.Memory(self.root)
        self.source = self.root / "rollout.jsonl"
        self.source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "fixture", "cwd": str(self.root), "cli_version": "0.162.0"}}) + "\n", encoding="utf-8")
        self.mem.register(self.source, "fixture", self.root, "start")
        self.mem.publish(state(), 0, BOOTSTRAP)

    def tearDown(self):
        # fixtures must never be written inside the package (a run leaves it byte-identical)
        inside = self.root.resolve().is_relative_to(Path(__file__).resolve().parent)
        self.tmp.cleanup()
        self.assertFalse(inside)

    def append(self, payload, newline=True):
        with self.source.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "response_item", "payload": payload}, ensure_ascii=False) + ("\n" if newline else ""))

    def test_write_readback_binds_this_version_and_body(self):
        old = self.mem.card.read_bytes()
        s = self.mem.head()[1]
        s["goal"] = "这次提交的新正文"
        receipt = self.mem.publish(s, 1, PUBLISHER)
        self.assertEqual(receipt["version"], 2)
        self.assertNotEqual(old, self.mem.card.read_bytes())
        self.assertIn("这次提交的新正文", self.mem.card.read_text(encoding="utf-8"))
        self.assertEqual(memory.digest(self.mem.card.read_bytes()), receipt["card_sha256"])
        self.assertEqual(memory.read(receipt["state_path"])["commit_id"], receipt["commit_id"])

    def test_write_failure_cannot_use_old_card_as_success(self):
        before = self.mem.card.read_bytes()
        receipts = list((self.mem.base / "receipts").glob("*.json"))
        s = self.mem.head()[1]
        s["goal"] = "尚未成功保存"
        original = memory.atomic
        def failing(path, data):
            if Path(path) == self.mem.card:
                raise OSError("injected card write failure")
            original(path, data)
        with patch.object(memory, "atomic", failing):
            with self.assertRaises(OSError):
                self.mem.publish(s, 1, PUBLISHER)
        self.assertEqual(before, self.mem.card.read_bytes())
        self.assertEqual(len(receipts), len(list((self.mem.base / "receipts").glob("*.json"))))
        self.assertEqual(self.mem.check()["status"], "check_failed")
        self.mem.repair()
        self.assertIn("尚未成功保存", self.mem.card.read_text(encoding="utf-8"))

    def test_stale_candidate_cannot_overwrite_new_decision(self):
        stale = self.mem.head()[1]
        s = copy.deepcopy(stale)
        s["decisions"].append({"id": "D2", "text": "新决定", "source": "new-user"})
        self.mem.publish(s, 1, PUBLISHER)
        with self.assertRaisesRegex(ValueError, "stale version"):
            self.mem.publish(stale, 1, PUBLISHER)
        self.assertEqual(self.mem.head()[1]["decisions"][-1]["id"], "D2")

    def test_terminal_current_rejected_and_history_retained(self):
        for status in ["completed", "accepted", "cancelled", "superseded"]:
            s = self.mem.head()[1]
            s["tasks"][1]["status"] = status
            with self.assertRaisesRegex(ValueError, "current task is"):
                self.mem.publish(s, 1, PUBLISHER)
        s = self.mem.head()[1]
        s["tasks"] = s["tasks"][1:]
        with self.assertRaisesRegex(ValueError, "history tasks"):
            self.mem.publish(s, 1, PUBLISHER)

    def test_decisions_immutable_and_worker_role_is_scoped(self):
        s = self.mem.head()[1]
        s["decisions"][0]["text"] = "迟到的候选改掉决定"
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.mem.publish(s, 1, PUBLISHER)
        s = self.mem.head()[1]
        s["goal"] = WORKER + " 冒改主线"
        with self.assertRaisesRegex(ValueError, WORKER + " cannot"):
            self.mem.publish(s, 1, WORKER)

    def test_public_receipt_does_not_ack_semantics_or_capture_reasoning(self):
        self.append({"type": "reasoning", "text": "PRIVATE_SENTINEL"})
        self.append({"type": "message", "role": "assistant", "channel": "analysis", "content": [{"type": "output_text", "text": "PRIVATE_SENTINEL"}]})
        self.append({"type": "message", "role": "assistant", "phase": "analysis", "content": [{"type": "output_text", "text": "PRIVATE_SENTINEL"}]})
        self.append({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "PRIVATE_SENTINEL"}]})
        self.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "以后先检查有效决定"}]})
        self.mem.sync()
        check = self.mem.check()
        self.assertEqual(check["status"], "pending_increment")
        self.assertEqual(check["pending_public_count"], 1)
        raw = "".join(p.read_text(encoding="utf-8") for p in (self.mem.base / "inbox").glob("*.json"))
        self.assertNotIn("PRIVATE_SENTINEL", raw)
        s = self.mem.head()[1]
        s["coverage"].append({"id": check["pending_public_ids"][0], "destination": "D1", "reason": "已复核，纳入有效约束"})
        self.mem.publish(s, 1, PUBLISHER)
        self.assertEqual(self.mem.check()["status"], "covered")

    def test_partial_malformed_rotation_do_not_advance_cursor(self):
        self.mem.sync()
        before = memory.read(self.mem.base / "cursors/fixture.json")
        self.append({"type": "message", "role": "user", "content": []}, newline=False)
        self.assertEqual(self.mem.sync()["sources"][0]["offset"], before["offset"])
        with self.source.open("a", encoding="utf-8") as f:
            f.write("\nNOT_JSON\n")
        self.assertFalse(self.mem.sync()["ok"])
        self.assertEqual(memory.read(self.mem.base / "cursors/fixture.json"), before)
        self.source.write_text("replaced\n", encoding="utf-8")
        self.assertEqual(self.mem.check()["status"], "check_failed")

    def test_replay_after_cursor_write_failure_deduplicates(self):
        self.append({"type": "message", "role": "user", "content": []})
        original = memory.atomic
        def failing(path, data):
            if Path(path).parent.name == "cursors":
                raise OSError("cursor write failed")
            original(path, data)
        with patch.object(memory, "atomic", failing):
            self.assertFalse(self.mem.sync()["ok"])
        self.mem.sync()
        self.assertEqual(len(self.mem.events()), 1)

    def test_component_absent_new_input_never_reuses_old_green(self):
        self.mem.sync()
        old = self.mem.check()
        self.assertEqual(old["status"], "covered")
        self.append({"type": "message", "role": "user", "content": []})
        new = self.mem.check()
        self.assertNotEqual(new["check_id"], old["check_id"])
        self.assertEqual(new["status"], "pending_increment")
        self.assertEqual(new["automation"], "unconfirmed")
        self.assertGreater(new["sources"][0]["unreceived_bytes"], 0)

    def test_unknown_tool_call_and_scope_filter(self):
        self.append({"type": "custom_tool_call", "call_id": "c1", "name": "test", "input": "test"})
        self.mem.sync()
        self.assertEqual(len(self.mem.check()["unknown_tool_calls"]), 1)
        self.assertEqual(self.mem.hook({"session_id": "unrelated", "cwd": str(self.root), "hook_event_name": "UserPromptSubmit"}), {})
        self.append({"type": "custom_tool_call_output", "call_id": "c1", "output": "done"})
        self.mem.sync()
        self.assertEqual(self.mem.check()["unknown_tool_calls"], [])

    def test_acceptance_requires_version_bound_evidence(self):
        s = self.mem.head()[1]
        s["tasks"][1]["status"] = "accepted"
        s["current"][PUBLISHER] = None
        with self.assertRaisesRegex(ValueError, "acceptance requires"):
            self.mem.publish(s, 1, PUBLISHER)
        evidence = self.root / "actual.txt"
        evidence.write_text("actual v1", encoding="utf-8")
        s["tasks"][1]["review"] = {"criterion": "本次正文实际存在", "reviewer": "test", "evidence": [{"path": str(evidence), "sha256": "old_hash"}]}
        with self.assertRaisesRegex(ValueError, "changed"):
            self.mem.publish(s, 1, PUBLISHER)
        s["tasks"][1]["review"]["evidence"][0]["sha256"] = memory.digest(evidence.read_bytes())
        self.mem.publish(s, 1, PUBLISHER)

    def test_manual_resume_labelled_manual(self):
        self.assertEqual(self.mem.resume()["mode"], "manual_recovery")

    def test_missing_source_and_handler_run_do_not_prove_automation(self):
        output = self.mem.hook({"session_id": "fixture", "cwd": str(self.root), "hook_event_name": "SessionStart", "source": "resume"})
        self.assertIn("additionalContext", output["hookSpecificOutput"])
        self.assertEqual(self.mem.check()["automation"], "handler_ran_host_trigger_unverified")
        self.source.unlink()
        self.assertEqual(self.mem.check()["status"], "check_failed")

    def test_worker_closes_own_task_without_reassigning_main_line(self):
        s = self.mem.head()[1]
        s["tasks"].append({"id": "C1", "owner": WORKER, "title": "实施", "status": "review", "next": "待外部判断", "source": "test"})
        s["current"][WORKER] = "C1"
        self.mem.publish(s, 1, PUBLISHER)
        s = self.mem.head()[1]
        s["tasks"][-1]["status"] = "completed"
        s["current"][WORKER] = None
        self.mem.publish(s, 2, WORKER)
        self.assertEqual(self.mem.head()[1]["current"][PUBLISHER], "T1")

    def test_changed_acceptance_evidence_invalidates_old_status(self):
        s = self.mem.head()[1]
        evidence = self.root / "proof.txt"
        evidence.write_text("proof v1", encoding="utf-8")
        s["tasks"][1]["status"] = "accepted"
        s["tasks"][1]["review"] = {"criterion": "证据版本", "reviewer": "fixture", "evidence": [{"path": str(evidence), "sha256": memory.digest(evidence.read_bytes())}]}
        s["current"][PUBLISHER] = None
        self.mem.publish(s, 1, PUBLISHER)
        self.assertEqual(self.mem.check()["status"], "pending_increment")
        evidence.write_text("proof v2 changed", encoding="utf-8")
        self.assertEqual(self.mem.check()["status"], "check_failed")


if __name__ == "__main__":
    # print which branch of scratch_root() was taken, so a run is checkable
    print("fixture scratch dir = %s" % scratch_root())
    print("tempfile.gettempdir() = %s" % tempfile.gettempdir())
    unittest.main(verbosity=2)
