"""Local job queue (fallback when the cloud farm queue is unavailable/not desired).

JSON file based: [{"id", "file", "slots", "status": pending|running|done|failed,
                   "cloud_file_id", "note", "added_at"}]. The MCP exposes
queue_add/list/remove/process; process() runs one iteration:
  - head running + printer busy  -> waiting_print
  - head running + finished      -> mark done, recurse to next pending
  - head pending                 -> precheck -> upload -> start -> started
"""
from __future__ import annotations

import json
import os
import time


class Queue:
    def __init__(self, path: str):
        self.path = path
        if os.path.exists(path):
            self.items = json.load(open(path, encoding="utf-8"))
        else:
            self.items = []

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=1)

    def add(self, file: str, slots: list[int], note: str = "") -> dict:
        next_id = max((t["id"] for t in self.items), default=0) + 1
        t = {"id": next_id, "file": file, "slots": slots, "status": "pending",
             "cloud_file_id": None, "note": note,
             "added_at": time.strftime("%m-%d %H:%M")}
        self.items.append(t)
        self._save()
        return t

    def remove(self, tid: int) -> bool:
        before = len(self.items)
        self.items = [t for t in self.items if t["id"] != tid]
        self._save()
        return len(self.items) < before

    def head(self) -> dict | None:
        for t in self.items:
            if t["status"] in ("pending", "running"):
                return t
        return None

    def mark(self, tid: int, status: str, **extra):
        for t in self.items:
            if t["id"] == tid:
                t["status"] = status
                t.update(extra)
        self._save()

    def pending_ids(self) -> list[int]:
        return [t["id"] for t in self.items if t["status"] == "pending"]
