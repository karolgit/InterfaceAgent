"""Surface abstraction: the seam between "how we perceive/act on a UI" and "the recorded flow".

Everything above this layer (agent, compiler, replay, policy) sees only Observation / Node and
calls Surface.act with a small verb set. A Swing app, a legacy web app, a 3270 terminal, or a
Windows UIA desktop app each get their own Surface implementation that produces the same
shapes. Screenshot-only surfaces would populate Node from OCR/vision with role guesses and
bounds, and the same locator strategies (label proximity, coordinates) still apply.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

STRUCTURAL_ROLES = {"panel", "root pane", "layered pane", "viewport", "filler", "scroll bar", "desktop pane"}
INPUT_ROLES = {"text", "password text", "combo box", "check box", "radio button"}


@dataclass
class Node:
    ref: str
    role: str
    name: str = ""
    value: str | None = None
    states: list[str] = field(default_factory=list)
    bounds: tuple[int, int, int, int] | None = None
    window: str = ""
    window_index: int = 0
    container: str | None = None
    labeled_by: str | None = None
    label: str | None = None  # labeled_by, or inferred from geometry
    group: str | None = None  # nearest named ancestor panel (e.g. a titled border "Share Accounts")
    options: list[str] | None = None
    columns: list[str] | None = None
    rows: list[list[str]] | None = None
    hidden: bool = False
    actionable: bool = False
    editable: bool = False
    depth: int = 0

    @property
    def enabled(self) -> bool:
        return "enabled" in self.states

    @property
    def path(self) -> str:
        """Index path inside its window, without the window prefix (e.g. '0.1.0.3')."""
        return self.ref.split(".", 1)[1] if "." in self.ref else ""

    def text(self) -> str:
        parts = [self.name or ""]
        if self.value:
            parts.append(self.value)
        if self.rows:
            parts.extend(" ".join(r) for r in self.rows)
        return " ".join(p for p in parts if p)


@dataclass
class Window:
    index: int
    title: str
    kind: str
    modal: bool
    bounds: tuple[int, int, int, int]
    nodes: list[Node]


@dataclass
class Observation:
    windows: list[Window]
    control: dict[str, Any] = field(default_factory=dict)

    def nodes(self) -> list[Node]:
        return [n for w in self.windows for n in w.nodes]

    def node(self, ref: str) -> Node | None:
        for n in self.nodes():
            if n.ref == ref:
                return n
        return None

    @property
    def top_window(self) -> Window | None:
        modals = [w for w in self.windows if w.modal]
        if modals:
            return modals[-1]
        return self.windows[0] if self.windows else None

    def containers(self) -> list[str]:
        return sorted({n.name for n in self.nodes() if n.role == "internal frame" and n.name})

    def all_text(self) -> str:
        chunks = [w.title for w in self.windows]
        chunks += [n.text() for n in self.nodes() if not n.hidden]
        return "\n".join(c for c in chunks if c)

    def fingerprint(self) -> str:
        """Cheap change detector used to decide when the UI has settled."""
        sig = [(w.title, [(n.ref, n.name, n.value, tuple(n.states), n.rows and len(n.rows)) for n in w.nodes])
               for w in self.windows]
        return hashlib.sha1(json.dumps(sig, default=str).encode()).hexdigest()

    @staticmethod
    def from_json(d: dict[str, Any]) -> "Observation":
        windows = []
        for w in d.get("windows", []):
            nodes = []
            for n in w.get("nodes", []):
                b = n.get("bounds")
                nodes.append(Node(
                    ref=n["ref"], role=n.get("role", ""), name=n.get("name", ""), value=n.get("value"),
                    states=n.get("states", []), bounds=tuple(b) if b else None, window=w.get("title", ""),
                    window_index=w.get("index", 0), container=n.get("container"),
                    labeled_by=n.get("labeled_by") or None, options=n.get("options"),
                    columns=n.get("columns"), rows=n.get("rows"), hidden=bool(n.get("hidden_menu_item")),
                    actionable=bool(n.get("actionable")), editable=bool(n.get("editable_text")),
                    depth=n.get("depth", 0)))
            windows.append(Window(index=w.get("index", 0), title=w.get("title", ""), kind=w.get("kind", ""),
                                  modal=bool(w.get("modal")), bounds=tuple(w.get("bounds", (0, 0, 0, 0))),
                                  nodes=nodes))
        obs = Observation(windows=windows, control=d.get("control", {}))
        infer_groups(obs)
        infer_labels(obs)
        return obs


GROUP_ROLES = {"panel", "scroll pane"}


def infer_groups(obs: Observation) -> None:
    """Attach each node to its nearest named panel ancestor (titled borders name their panel)."""
    for w in obs.windows:
        named = {n.ref: n.name for n in w.nodes if n.role in GROUP_ROLES and n.name}
        for n in w.nodes:
            ref = n.ref
            while "." in ref:
                ref = ref.rsplit(".", 1)[0]
                if ref in named:
                    n.group = named[ref]
                    break


def infer_labels(obs: Observation) -> None:
    """Give unlabeled inputs a label from the nearest label on the same row to their left.

    Legacy apps rarely wire label relations, so geometry is the fallback a human uses too.
    Works identically on bounds that come from OCR.
    """
    for w in obs.windows:
        labels = [n for n in w.nodes if n.role == "label" and n.name and n.bounds]
        for n in w.nodes:
            if n.labeled_by:
                n.label = n.labeled_by
                continue
            if n.role not in INPUT_ROLES or not n.bounds:
                continue
            x, y, bw, bh = n.bounds
            cy = y + bh / 2
            best, best_dx = None, 10_000
            for lab in labels:
                lx, ly, lw, lh = lab.bounds
                if lab.container != n.container:
                    continue
                if abs((ly + lh / 2) - cy) <= max(8, bh / 2) and lx + lw <= x + 2:
                    dx = x - (lx + lw)
                    if dx < best_dx and dx < 300:
                        best, best_dx = lab, dx
            if best is not None:
                n.label = best.name


class Surface(Protocol):
    """What every surface driver must provide."""

    kind: str

    def observe(self) -> Observation: ...

    def screenshot(self) -> tuple[bytes, tuple[int, int]]:
        """PNG bytes and the screen origin of the image (for mapping node bounds)."""
        ...

    def act(self, op: str, **kwargs: Any) -> None: ...

    def settle(self, timeout_s: float = 3.0) -> Observation: ...

    def set_control(self, mode: str, holder: str = "") -> dict[str, Any]: ...

    def events(self, since: int = 0) -> tuple[list[dict[str, Any]], int]: ...
