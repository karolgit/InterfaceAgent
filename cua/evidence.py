"""Run evidence: a structured JSONL log plus redacted screenshots and tree snapshots.

Every line passes through the run's Redactor before it touches disk.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redact import Redactor
from .surface.base import Observation

ROOT = Path(__file__).resolve().parents[1]


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


class Evidence:
    def __init__(self, run_id: str, redactor: Redactor, base: Path | None = None):
        self.run_id = run_id
        self.redactor = redactor
        self.dir = (base or ROOT / "runs") / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "log.jsonl"
        self._shots = 0
        self._t0 = time.monotonic()

    def log(self, event: str, **fields: Any) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "t_ms": int((time.monotonic() - self._t0) * 1000), "event": event, **fields}
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.redactor.obj(rec), default=str) + "\n")

    def screenshot(self, surface: Any, obs: Observation, label: str) -> str | None:
        """Save a REDACTED screenshot; returns its path relative to the run dir."""
        try:
            png, origin = surface.screenshot()
        except Exception as e:  # evidence must never crash the run
            self.log("evidence_error", what="screenshot", error=str(e))
            return None
        self._shots += 1
        name = f"{self._shots:02d}-{label}.png"
        (self.dir / name).write_bytes(self.redactor.screenshot(png, origin, obs))
        return name

    def tree(self, obs: Observation, label: str) -> str:
        """Save a redacted, compact accessibility snapshot (the 'DOM snapshot' of a desktop app)."""
        sensitive = self.redactor.sensitive_nodes(obs)
        out = []
        for w in obs.windows:
            nodes = []
            for n in w.nodes:
                name, value = self.redactor.node_text(n, sensitive)
                item = {"ref": n.ref, "role": n.role, "name": name}
                if value is not None:
                    item["value"] = value
                if n.label:
                    item["label"] = n.label
                if n.rows:
                    item["rows"] = [[self.redactor.text(c) for c in r] for r in n.rows]
                if n.container:
                    item["container"] = n.container
                nodes.append(item)
            out.append({"title": w.title, "kind": w.kind, "modal": w.modal, "nodes": nodes})
        name = f"tree-{label}.json"
        (self.dir / name).write_text(json.dumps(out, indent=1), encoding="utf-8")
        return name

    def write_json(self, name: str, data: Any, redact: bool = True) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(self.redactor.obj(data) if redact else data, indent=2, default=str), encoding="utf-8")
        return p
