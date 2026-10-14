"""Project-scoped Codex memory. Standard library only; no model/database writes.

The rollout adapter is explicitly versioned and allowlisted. A successful sync
means received, never semantically accepted. Only publish changes current state.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

DEFAULT_ROOT = Path(__file__).resolve().parent
ADAPTER = "codex-rollout-0.162-response-item-v1"

# --- Layout and role vocabulary (de-privatised defaults; adapt for another host) ---
PUBLISHER = "owner"        # the only role allowed to publish current state
WORKER = "worker"          # scoped implementer: may only touch its own tasks/current slot
BOOTSTRAP = "bootstrap"    # one-shot actor allowed to create version 1
BASE_DIR = "runtime"                        # state root, relative to --root
CARD_FILE = "outputs/memory-card.md"        # the single current card
TERMINAL = {"accepted", "completed", "cancelled", "superseded"}
ACTIVE = {"pending", "in_progress", "waiting", "review", "blocked", "unknown"}


def now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextlib.contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as f:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


class Memory:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root / BASE_DIR
        self.card = self.root / CARD_FILE
        self.script = Path(__file__).resolve()

    def config(self):
        return read(self.base / "sources.json")

    def resume_cmd(self):
        # sys.executable = the interpreter actually running, so the printed command is
        # correct on Windows (python.exe), macOS and Linux (python3) alike.
        return '"' + sys.executable + '" -B -X utf8 "' + self.script.as_posix() + '" --root "' + self.root.as_posix() + '"'

    def resolve_ref(self, ref):
        path = Path(ref)
        mapping = self.base / "path-resolution.json"
        for item in read(mapping) if mapping.exists() else []:
            try:
                relative = path.relative_to(Path(item["old_prefix"]))
            except ValueError:
                continue
            target = Path(item["physical_prefix"]) / relative
            if path.exists() and target.exists() and path.resolve() != target.resolve():
                raise ValueError("path aliases resolve to different trees")
            return target.resolve()
        return path.resolve()

    def head(self):
        p = self.base / "head.json"
        if not p.exists():
            return None, None
        h = read(p)
        data = (self.base / "versions" / h["file"]).read_bytes()
        if digest(data) != h["state_sha256"]:
            raise ValueError("state version hash mismatch")
        s = json.loads(data)
        if s["version"] != h["version"]:
            raise ValueError("state version mismatch")
        return h, s

    def register(self, path, sid, cwd, start):
        path = Path(path).resolve()
        with lock(self.base / "write.lock"):
            with path.open("rb") as stream:
                first = json.loads(stream.readline())
            meta = first["payload"]
            if first["type"] != "session_meta" or (meta.get("id") or meta.get("session_id")) != sid:
                raise ValueError("source session ID mismatch")
            if Path(meta["cwd"]).resolve() != Path(cwd).resolve():
                raise ValueError("source workspace mismatch")
            if not str(meta.get("cli_version", "")).startswith("0.162."):
                raise ValueError("unverified rollout version; adapt and test before registering")
            config = self.config() if (self.base / "sources.json").exists() else {"adapter": ADAPTER, "sources": []}
            if any(x["session_id"] == sid for x in config["sources"]):
                raise ValueError("source already registered; baseline cannot be reset")
            data = path.read_bytes() if start == "end" else b""
            if data and not data.endswith(b"\n"):
                raise ValueError("baseline ends in partial record; retry later")
            offset = len(data)
            source = {"session_id": sid, "path": path.as_posix(), "cwd": Path(cwd).resolve().as_posix(),
                      "baseline": offset, "baseline_at": now(), "baseline_sha256": digest(data),
                      "historical_scope": "excluded_before_baseline" if offset else "included_from_start",
                      "cli_version": meta["cli_version"]}
            config["sources"].append(source)
            atomic(self.base / "cursors" / (sid + ".json"), encoded({"offset": offset, "prefix_sha256": digest(data)}))
            atomic(self.base / "sources.json", encoded(config))
            return source

    @staticmethod
    def public(record):
        if record.get("type") != "response_item":
            return None
        p = record.get("payload", {})
        t = p.get("type")
        if t == "message" and p.get("role") in {"user", "assistant"}:
            if p.get("channel") not in {None, "final", "commentary"}:
                return None
            if p.get("phase") not in {None, "final", "final_answer", "commentary"}:
                return None
            # Text only: preserve references to source records for images/audio.
            text = "\n".join(c.get("text", "") for c in p.get("content", [])
                             if c.get("type") in {"input_text", "output_text", "text"})
            return {"kind": p["role"], "text": text, "message_id": p.get("id")}
        if t in {"function_call", "custom_tool_call"}:
            return {"kind": "tool_call", "call_id": p.get("call_id"), "name": p.get("name"),
                    "input": p.get("arguments", p.get("input"))}
        if t in {"function_call_output", "custom_tool_call_output"}:
            out = p.get("output")
            if isinstance(out, list):
                out = "\n".join(c.get("text", "") for c in out if isinstance(c, dict) and "text" in c)
            return {"kind": "tool_result", "call_id": p.get("call_id"), "output": out}
        # reasoning, developer/system messages and event duplicates never enter inbox.
        return None

    def sync(self, trigger="manual"):
        reports = []
        with lock(self.base / "write.lock"):
            config = self.config()
            if config["adapter"] != ADAPTER:
                raise ValueError("unverified adapter")
            for source in config["sources"]:
                sid = source["session_id"]
                try:
                    cursor = read(self.base / "cursors" / (sid + ".json"))
                    data = self.resolve_ref(source["path"]).read_bytes()
                    offset = cursor["offset"]
                    if len(data) < offset or digest(data[:offset]) != cursor["prefix_sha256"]:
                        raise ValueError("source truncated/replaced; cursor unchanged")
                    first = json.loads(data.split(b"\n", 1)[0])
                    meta = first["payload"]
                    if (meta.get("id") or meta.get("session_id")) != sid or self.resolve_ref(meta["cwd"]) != self.resolve_ref(source["cwd"]):
                        raise ValueError("source identity changed")
                    events = []
                    end = offset
                    for line in data[offset:].splitlines(keepends=True):
                        if not line.endswith(b"\n"):
                            break  # Don't commit an incomplete record.
                        rec = json.loads(line.decode("utf-8"))
                        event = self.public(rec)
                        if event is not None:
                            event.update({"id": sid + ":" + str(end) + ":" + digest(line),
                                          "session_id": sid, "path": source["path"], "offset": end,
                                          "end": end + len(line), "timestamp": rec.get("timestamp"),
                                          "ordinal": rec.get("ordinal"), "raw_sha256": digest(line)})
                            events.append(event)
                        end += len(line)
                    if end > offset:
                        # Persist sources BEFORE advancing the cursor. Replay has stable IDs.
                        batch = {"adapter": ADAPTER, "events": events, "start": offset, "end": end,
                                 "session_id": sid, "trigger": trigger, "received_at": now()}
                        name = sid + "-" + str(offset) + "-" + str(end) + ".json"
                        atomic(self.base / "inbox" / name, encoded(batch))
                        atomic(self.base / "cursors" / (sid + ".json"), encoded({"offset": end, "prefix_sha256": digest(data[:end])}))
                    reports.append({"session_id": sid, "status": "received", "events": len(events),
                                    "offset": end, "source_size": len(data), "partial_bytes": len(data) - end})
                except Exception as exc:
                    reports.append({"session_id": sid, "status": "check_failed", "error": str(exc)})
            result = {"run_id": uuid.uuid4().hex, "at": now(), "trigger": trigger, "sources": reports,
                      "ok": all(x["status"] == "received" for x in reports) and bool(reports)}
            atomic(self.base / "last-sync.json", encoded(result))
            return result

    def events(self):
        items = {}
        for path in sorted((self.base / "inbox").glob("*.json")):
            for event in read(path)["events"]:
                if event["id"] in items and items[event["id"]] != event:
                    raise ValueError("inbox event collision")
                items[event["id"]] = event
        return items

    def evidence_path(self, evidence):
        """Resolve an exact historical hash, never endorse changed live code."""
        path = self.resolve_ref(evidence["path"])
        if path.exists() and digest(path.read_bytes()) == evidence["sha256"]:
            return path, "original_reference"
        index = self.base / "historical-evidence.json"
        for item in read(index) if index.exists() else []:
            if item["source_ref"] == evidence["path"] and item["sha256"] == evidence["sha256"]:
                archived = (self.root / item["archive_relative"]).resolve()
                if not archived.is_relative_to(self.root):
                    raise ValueError("historical evidence escapes project")
                if archived.exists() and digest(archived.read_bytes()) == evidence["sha256"]:
                    return archived, "frozen_historical_version_not_current_code_acceptance"
        raise ValueError("acceptance evidence absent or changed: " + evidence["path"])

    def validate(self, candidate, old):
        for k in ["goal", "snapshot", "constraints", "decisions", "tasks", "current", "waiting", "details", "coverage"]:
            if k not in candidate:
                raise ValueError("missing state field: " + k)
        tasks = candidate["tasks"]
        decisions = candidate["decisions"]
        if len({d["id"] for d in decisions}) != len(decisions):
            raise ValueError("duplicate decision ID")
        previous_ids = set()
        for d in decisions:
            if not d.get("source") or not d.get("text") or (d.get("supersedes") and d["supersedes"] not in previous_ids):
                raise ValueError("decision requires source/text and a preceding superseded decision")
            previous_ids.add(d["id"])
        if len({t["id"] for t in tasks}) != len(tasks):
            raise ValueError("duplicate task ID")
        for t in tasks:
            if t["status"] not in ACTIVE | TERMINAL or not t.get("owner") or not t.get("source"):
                raise ValueError("invalid task status/owner/source")
        by_id = {t["id"]: t for t in tasks}
        for role, tid in candidate["current"].items():
            if tid is not None and (tid not in by_id or by_id[tid]["status"] in TERMINAL):
                raise ValueError("current task is completed/cancelled/superseded: " + str(tid))
            if tid is not None and by_id[tid]["owner"] != role:
                raise ValueError("current task owner mismatch")
        if old:
            for decision in old["decisions"]:
                match = next((d for d in candidate["decisions"] if d["id"] == decision["id"]), None)
                if match != decision:
                    raise ValueError("accepted decisions are immutable; append a superseding decision")
            for task in old["tasks"]:
                if task["id"] not in by_id:
                    raise ValueError("history tasks cannot be removed")
            oldcov = {x["id"]: x for x in old["coverage"]}
            newcov = {x["id"]: x for x in candidate["coverage"]}
            if any(newcov.get(k) != v for k, v in oldcov.items()):
                raise ValueError("coverage history cannot be removed/rewritten")
        events = self.events()
        if len({c["id"] for c in candidate["coverage"]}) != len(candidate["coverage"]):
            raise ValueError("duplicate coverage ID")
        for c in candidate["coverage"]:
            if c["id"] not in events or not c.get("destination") or not c.get("reason"):
                raise ValueError("coverage requires received ID, destination and semantic reason")
        # Accepted means a scoped review with evidence, not a general machine truth claim.
        for t in tasks:
            prior = next((p for p in (old or {}).get("tasks", []) if p["id"] == t["id"]), None)
            if t["status"] == "accepted" and t != prior:
                review = t.get("review", {})
                if not review.get("criterion") or not review.get("reviewer") or not review.get("evidence"):
                    raise ValueError("acceptance requires criterion, reviewer and evidence")
                for e in review["evidence"]:
                    path = Path(e["path"])
                    if not path.is_absolute():
                        raise ValueError("acceptance evidence absent or changed")
                    self.evidence_path(e)

    def render(self, s):
        task = {t["id"]: t for t in s["tasks"]}
        out = ["# 当前接手入口", "", "> 唯一现行入口；由 memory.py publish 生成。不要手改。",
               "> 保存与回读只证明本版存在；语义取舍由 " + PUBLISHER + " 角色的会话承担。", "",
               "状态版本：" + str(s["version"]) + "；提交：" + s["commit_id"] + "；保存 UTC：" + s["saved_at"],
               "事实快照：" + s["snapshot"], "", "## 目标", "", s["goal"], "", "## 有效约束", ""]
        out += ["- " + x for x in s["constraints"]]
        out += ["", "## 已接受决定", ""]
        superseded = {d.get("supersedes") for d in s["decisions"]}
        out += ["- " + d["id"] + "：" + d["text"] + "（来源：" + d["source"] + "）"
                for d in s["decisions"] if d["id"] not in superseded]
        out += ["", "## 当前球与职责", ""]
        for role, tid in s["current"].items():
            t = task.get(tid)
            empty = "当前无进行中任务；已交付结果见下方历史" if any(x["owner"] == role and x["status"] in TERMINAL for x in s["tasks"]) else "尚未选定"
            out.append("- " + role + "：" + (t["id"] + " · " + t["title"] + "；状态 " + t["status"] + "；下一步：" + t["next"] if t else empty))
        out += ["", "## 未完成", ""]
        out += ["- " + t["owner"] + "/" + t["id"] + "：" + t["title"] + "（" + t["status"] + "）；" + t["next"] for t in s["tasks"] if t["status"] in ACTIVE]
        out += ["", "## 正在等待", ""] + ["- " + x for x in s["waiting"]]
        out += ["", "## 已接受结果与历史去向", ""]
        out += ["- " + t["id"] + "：" + t["title"] + "（" + t["status"] + "；来源：" + t["source"] + "）；" + t["next"] for t in s["tasks"] if t["status"] in TERMINAL]
        out += ["", "## 详情与恢复", ""] + ["- " + x for x in s["details"]]
        out += ["", "收件与覆盖状态必须现场检查；本卡不保留健康绿灯。", ""]
        command = self.resume_cmd()
        out += ["```", command + " resume", "```", "", "先读取恢复输出中的本次检查结果及未处理来源，再由 " + PUBLISHER + " 角色的会话选择下一步。",
                "原生日志在 baseline 之前的历史未全读、未自动整理；它们仍在原处。",
                "自动收件/压缩注入：见实施回执中的运行证据。手动 resume 不证明自动恢复。", "",
                "当前球：" + "；".join(role + "=" + str(tid) for role, tid in s["current"].items()),
                "未完成：见本卡未完成项及本次 resume 的未处理来源。", "待我方决定：" + s.get("needs_decision", "暂无。"), ""]
        return "\n".join(out).encode("utf-8")

    def publish(self, candidate, expected, actor):
        with lock(self.base / "write.lock"):
            head, old = self.head()
            version = old["version"] if old else 0
            if expected != version:
                raise ValueError("stale version; candidate kept, current decision unchanged")
            if actor == WORKER and old:
                # The worker owns implementation progress only; never the publisher's decisions.
                protected = [k for k in candidate if k not in {"tasks", "current", "version", "commit_id", "saved_at", "publisher"}]
                if any(candidate[k] != old.get(k) for k in protected):
                    raise ValueError(WORKER + " cannot change " + PUBLISHER + " fields")
                old_tasks = {t["id"]: t for t in old["tasks"] if t["owner"] != WORKER}
                new_tasks = {t["id"]: t for t in candidate["tasks"] if t["owner"] != WORKER}
                if old_tasks != new_tasks:
                    raise ValueError(WORKER + " cannot change other owners' tasks")
                if {k: v for k, v in candidate["current"].items() if k != WORKER} != {k: v for k, v in old["current"].items() if k != WORKER}:
                    raise ValueError(WORKER + " cannot change other owners' current selection")
            elif actor != PUBLISHER and not (actor == BOOTSTRAP and version == 0):
                raise ValueError("only " + PUBLISHER + " publishes current state; " + BOOTSTRAP + " allowed once")
            self.validate(candidate, old)
            s = dict(candidate)
            s.update({"version": version + 1, "commit_id": uuid.uuid4().hex, "saved_at": now(), "publisher": actor})
            raw = encoded(s)
            card = self.render(s)
            name = str(s["version"]) + "-" + s["commit_id"] + ".json"
            vp = self.base / "versions" / name
            atomic(vp, raw)
            if vp.read_bytes() != raw:
                raise ValueError("state read-back failed")
            # Immutable version is prepared first; head commits it. A crash at any
            # later step produces projection mismatch, never a success receipt.
            newhead = {"version": s["version"], "file": name, "state_sha256": digest(raw), "card_sha256": digest(card), "commit_id": s["commit_id"]}
            atomic(self.base / "head.json", encoded(newhead))
            atomic(self.card, card)
            if read(self.base / "head.json") != newhead or self.card.read_bytes() != card:
                raise ValueError("head/card read-back failed; run repair")
            receipt = {"status": "saved_and_read_back", "version": s["version"], "commit_id": s["commit_id"],
                       "state_path": vp.as_posix(), "card_path": self.card.as_posix(), "state_sha256": digest(raw),
                       "card_sha256": digest(card), "at": now(), "work_acceptance": "separate_review_required"}
            atomic(self.base / "receipts" / (s["commit_id"] + ".json"), encoded(receipt))
            return receipt

    def repair(self):
        with lock(self.base / "write.lock"):
            h, s = self.head()
            raw = self.render(s)
            if digest(raw) != h["card_sha256"]:
                raise ValueError("head projection definition differs; cannot repair")
            atomic(self.card, raw)
            if self.card.read_bytes() != raw:
                raise ValueError("repair read-back failed")
            return {"status": "projection_repaired", "version": s["version"], "card": self.card.as_posix()}

    def check(self):
        # Read-only; a fresh check ID is independent of the stopped collector.
        result = {"check_id": uuid.uuid4().hex, "at": now(), "sources": [], "automation": "unconfirmed"}
        try:
            h, s = self.head()
            if not h or not self.card.exists() or digest(self.card.read_bytes()) != h["card_sha256"]:
                raise ValueError("current card missing/changed; run repair")
            result["acceptance_evidence"] = []
            for task in s["tasks"]:
                if task["status"] == "accepted":
                    locations = []
                    for evidence in task["review"]["evidence"]:
                        path, mode = self.evidence_path(evidence)
                        locations.append({"source_ref": evidence["path"], "resolved_path": path.as_posix(), "sha256": evidence["sha256"], "mode": mode})
                    result["acceptance_evidence"].append({"task_id": task["id"], "status": "referenced_versions_present", "semantic_judgment": "reviewer_scoped", "locations": locations})
            events = self.events()
            covered = {x["id"] for x in s["coverage"]}
            pending = [e for k, e in events.items() if k not in covered and e["kind"] in {"user", "assistant"}]
            calls = {e["call_id"]: e for e in events.values() if e["kind"] == "tool_call"}
            results = {e["call_id"] for e in events.values() if e["kind"] == "tool_result"}
            unknown = [e["id"] for cid, e in calls.items() if cid not in results]
            for source in self.config()["sources"]:
                c = read(self.base / "cursors" / (source["session_id"] + ".json"))
                data = self.resolve_ref(source["path"]).read_bytes()
                if len(data) < c["offset"] or digest(data[:c["offset"]]) != c["prefix_sha256"]:
                    raise ValueError("source changed/truncated: " + source["session_id"])
                result["sources"].append({"session_id": source["session_id"], "received_offset": c["offset"],
                                          "source_size": len(data), "unreceived_bytes": len(data) - c["offset"],
                                          "historical_scope": source["historical_scope"], "baseline": source["baseline"]})
            lastpath = self.base / "last-sync.json"
            last = read(lastpath) if lastpath.exists() else None
            result.update({"version": s["version"], "pending_public_ids": [e["id"] for e in pending],
                           "pending_public_count": len(pending), "received_event_count": len(events),
                           "unknown_tool_calls": unknown, "last_sync": last})
            from organizer import Organizer
            result["organization"] = Organizer(self).status()
            unreceived = any(x["unreceived_bytes"] for x in result["sources"])
            result["content_status"] = "pending_increment" if pending or unreceived else "covered"
            # No positive automatic status from a manual run or an old receipt.
            if last and last["ok"] and last["trigger"].startswith("hook-handler:"):
                result["automation"] = "handler_ran_host_trigger_unverified"
            result["status"] = result["content_status"]
            if last is not None and not last["ok"]:
                result["status"] = "check_failed"
                result["error"] = "last sync failed; inspect sources and retry, no old green"
        except Exception as exc:
            result.update({"status": "check_failed", "error": str(exc)})
        return result

    def resume(self):
        before = self.check()
        synced = self.sync("manual-resume")
        result = self.check()
        h, s = self.head()
        events = self.events()
        pending_ids = result.get("pending_public_ids", [])
        return {"mode": "manual_recovery", "before_sync": before, "sync": synced, "check": result,
                "card": self.card.as_posix(), "state": s, "pending_public": [events[k] for k in pending_ids]}

    def hook(self, event):
        control = self.base / "automation.json"
        if control.exists() and not read(control).get("enabled", False):
            return {}
        sid = event.get("session_id")
        sources = self.config()["sources"]
        source = next((s for s in sources if s["session_id"] == sid), None)
        allowed = [source["cwd"]] + source.get("hook_cwds", []) if source else []
        if not source or not event.get("cwd") or self.resolve_ref(event["cwd"]) not in [self.resolve_ref(p) for p in allowed]:
            return {}  # Registered sessions only; never attach another project.
        name = event["hook_event_name"]
        try:
            synced = self.sync("hook-handler:" + name)
            from organizer import Organizer
            organized = Organizer(self).run()
            checked = self.check()
            payload = {"event": name, "source": event.get("source"), "session_id": sid,
                       "run_id": synced["run_id"], "organization": organized, "check": checked, "at": now(),
                       "host_trigger": "requires_correlated_native_hook_notification", "turn_id": event.get("turn_id")}
            evidence = self.base / "hook-evidence" / (synced["run_id"] + ".json")
            if name in {"SessionStart", "UserPromptSubmit"}:
                context = self.short_view(checked, synced["run_id"], evidence)
                payload["context_sha256"] = digest(context.encode("utf-8"))
                payload["context"] = context
                atomic(evidence, encoded(payload))
                return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": context}}
            atomic(evidence, encoded(payload))
            return {}
        except Exception as exc:
            # Advisory only, never block the ordinary conversation.
            return {"systemMessage": "项目记忆未确认：" + str(exc), "continue": True}

    def short_view(self, checked=None, run_id="manual", evidence=None):
        h, s = self.head()
        checked = checked or self.check()
        tasks = {t["id"]: t for t in s["tasks"]}
        # Coordinates and recovery are first, so a host spill cannot hide them.
        lines = ["项目记忆恢复：本段是短视图，内容不完整；完整卡与未处理来源须按入口读取。",
                 "版本=" + str(s["version"]) + "；恢复运行=" + run_id,
                 "唯一卡=" + self.card.as_posix(),
                 "恢复入口：" + self.resume_cmd() + " resume",
                 "收件=" + checked.get("status", "unknown") + "；未整理公开项=" + str(checked.get("pending_public_count", "unknown")) + "；未知工具调用=" + str(len(checked.get("unknown_tool_calls", []))),
                 "已保存来源只含登记后公开输入、公开回复及工具记录；baseline 前历史未整理。"]
        for p in checked.get("sources", []):
            lines.append("来源 " + p["session_id"] + "：baseline=" + str(p["baseline"]) + "，已收=" + str(p["received_offset"]) + "，现长=" + str(p["source_size"]) + "，未收字节=" + str(p["unreceived_bytes"]))
        for role, tid in s["current"].items():
            task = tasks.get(tid)
            lines.append(role + "=" + (tid + "（" + task["status"] + "）：" + task["next"] if task else "无进行中任务；不重启历史任务"))
        lines += ["分类候选仅待主会话复核；收件、整理和已接受决定是不同状态。",
                  "候选队列=" + (self.base / "organization/review").as_posix(),
                  "未知边界：钩子处理回执独自不能证明宿主触发或模型采用。"]
        if evidence:
            lines.append("本次运行证据=" + evidence.as_posix())
        return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", default=str(DEFAULT_ROOT))
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("register")
    a.add_argument("--path", required=True)
    a.add_argument("--session", required=True)
    a.add_argument("--cwd", required=True)
    a.add_argument("--from", dest="start", choices=["start", "end"], required=True)
    a = sub.add_parser("publish")
    a.add_argument("--input", required=True)
    a.add_argument("--expected", required=True, type=int)
    a.add_argument("--actor", choices=[PUBLISHER, BOOTSTRAP, WORKER], required=True)
    for cmd in ["sync", "check", "resume", "repair", "hook", "export", "pending", "organize", "organization-status", "view"]:
        sub.add_parser(cmd)
    args = p.parse_args()
    mem = Memory(args.root)
    try:
        if args.command == "register":
            result = mem.register(args.path, args.session, args.cwd, args.start)
        elif args.command == "publish":
            result = mem.publish(read(args.input), args.expected, args.actor)
        elif args.command == "hook":
            result = mem.hook(json.load(sys.stdin))
        elif args.command == "export":
            result = mem.head()[1]
        elif args.command == "pending":
            ids = mem.check().get("pending_public_ids", [])
            result = [mem.events()[k] for k in ids]
        elif args.command in {"organize", "organization-status"}:
            from organizer import Organizer
            result = Organizer(mem).run() if args.command == "organize" else Organizer(mem).status()
        elif args.command == "view":
            result = {"view": mem.short_view()}
        else:
            result = getattr(mem, args.command)()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and (result.get("status") == "check_failed" or result.get("ok") is False or result.get("check", {}).get("status") == "check_failed"):
            return 2
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "success_receipt": False, "side_effects": "inspect_head_and_check_before_retry", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
