"""Durable incremental draft queue. Mechanical extraction; no model calls.

The queue never publishes state or acknowledges semantic coverage. A review
artifact is consumed by this worker into a durable queue, not accepted by the publisher.
"""
from pathlib import Path
from memory import atomic, digest, encoded, lock, now, read


class Organizer:
    def __init__(self, memory):
        self.mem = memory
        self.base = memory.base / "organization"

    def status(self):
        index = read(self.base / "index.json") if (self.base / "index.json").exists() else {"consumed_ids": [], "jobs": []}
        jobs = [read(p) for p in sorted((self.base / "jobs").glob("*.json"))]
        return {"consumed_event_count": len(index["consumed_ids"]),
                "jobs": [{"id": j["id"], "status": j["status"], "base_version": j["base_version"], "event_count": len(j["event_ids"]), "model_calls": j["model_calls"]} for j in jobs],
                "acceptance": "drafts_only_main_session_review_required", "offline_service": False}

    def run(self):
        with lock(self.base / "worker.lock"):
            head, state = self.mem.head()
            index_path = self.base / "index.json"
            index = read(index_path) if index_path.exists() else {"consumed_ids": [], "jobs": []}
            events = self.mem.events()
            covered = {c["id"] for c in state["coverage"]}
            pending = sorted(set(events) - covered - set(index["consumed_ids"]))
            jobs_dir = self.base / "jobs"
            active = [read(p) for p in sorted(jobs_dir.glob("*.json")) if read(p)["status"] in {"queued", "draft_ready", "failed"}]
            if active:
                job = active[0]  # Restart resumes the same durable job first.
            elif pending:
                job_id = digest(encoded({"version": state["version"], "ids": pending}))
                job = {"id": job_id, "status": "queued", "base_version": state["version"],
                       "event_ids": pending, "created_at": now(), "model_calls": 0, "error_stage": None,
                       "method": "local_mechanical_extraction", "usage": None}
                atomic(jobs_dir / (job_id + ".json"), encoded(job))
            else:
                return {"status": "no_increment", "model_calls": 0, "version": state["version"]}
            job_path = jobs_dir / (job["id"] + ".json")
            draft_path = self.base / "drafts" / (job["id"] + ".json")
            try:
                if not draft_path.exists():
                    entries = []
                    for event_id in job["event_ids"]:
                        e = events[event_id]
                        category = {"user": "user_input_requires_review", "assistant": "public_reply_requires_review",
                                    "tool_call": "tool_request_evidence", "tool_result": "tool_result_evidence"}[e["kind"]]
                        entry = {"event_id": event_id, "category": category,
                                 "source_ref": {k: e.get(k) for k in ["session_id", "path", "offset", "end", "raw_sha256", "call_id"]},
                                 "payload": {k: e[k] for k in ["text", "input", "output", "name"] if k in e},
                                 "acceptance": "unreviewed"}
                        entries.append(entry)
                    draft = {"job_id": job["id"], "base_version": job["base_version"], "entries": entries,
                             "task_candidates": [{"source_id": e["event_id"], "status": "unreviewed", "instruction_text": e["payload"].get("text", "")} for e in entries if e["category"] == "user_input_requires_review"],
                             "conflict_review": {"status": "main_session_judgment_required", "against_decisions": [d["id"] for d in state["decisions"]]},
                             "current_state_changed": False, "model_calls": 0}
                    atomic(draft_path, encoded(draft))
                draft = read(draft_path)
                if draft["job_id"] != job["id"] or [e["event_id"] for e in draft["entries"]] != job["event_ids"]:
                    raise ValueError("draft provenance mismatch")
                job["status"] = "draft_ready"
                atomic(job_path, encoded(job))
                # Serialize consumption with publish so a late draft cannot bind
                # to a newer decision. Never call Memory.publish here.
                with lock(self.mem.base / "write.lock"):
                    latest, _ = self.mem.head()
                    if latest["version"] != job["base_version"]:
                        job.update({"status": "stale", "error_stage": "consume", "observed_version": latest["version"], "finished_at": now()})
                        atomic(job_path, encoded(job))
                        return {"status": "stale_candidate_kept", "job_id": job["id"], "current_version": latest["version"], "model_calls": 0}
                    review_path = self.base / "review" / (job["id"] + ".json")
                    raw = encoded(draft)
                    atomic(review_path, raw)
                    if review_path.read_bytes() != raw:
                        raise ValueError("review queue read-back failed")
                    index["consumed_ids"] = sorted(set(index["consumed_ids"]) | set(job["event_ids"]))
                    if job["id"] not in index["jobs"]:
                        index["jobs"].append(job["id"])
                    atomic(index_path, encoded(index))
                    job.update({"status": "consumed_into_review_queue", "finished_at": now(), "review_relative": review_path.relative_to(self.mem.root).as_posix(), "draft_sha256": digest(raw)})
                    atomic(job_path, encoded(job))
                return {"status": job["status"], "job_id": job["id"], "event_count": len(job["event_ids"]),
                        "review_path": review_path.as_posix(), "draft_sha256": job["draft_sha256"], "model_calls": 0, "state_version": latest["version"], "acceptance": "unreviewed"}
            except Exception as exc:
                job.update({"status": "failed", "error_stage": "draft_or_consume", "error": str(exc), "failed_at": now()})
                atomic(job_path, encoded(job))
                raise
