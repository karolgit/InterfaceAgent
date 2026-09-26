"""Locator building (record time) and resolution (replay time). Surface-agnostic."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .artifact import A11yLocator, CoordLocator, GroupLocator, LabelLocator, Locator, PathLocator, Scope, Target
from .surface.base import STRUCTURAL_ROLES, Node, Observation, Window


@dataclass
class Resolution:
    node: Node | None
    strategy_index: int | None
    notes: list[str]


def rx_escape(s: str) -> str:
    """re.escape, minus escaping of spaces and hyphens, so artifacts stay readable to reviewers."""
    return re.escape(s).replace(r"\ ", " ").replace(r"\-", "-").replace(r"\#", "#")


def _rx(pattern: str | None, text: str | None) -> bool:
    if pattern is None:
        return True
    return re.search(pattern, text or "") is not None


def scope_window(obs: Observation, scope: Scope, main_window: str) -> Window | None:
    if scope.window == "main":
        for w in obs.windows:
            if w.kind == "frame" and re.search(main_window, w.title):
                return w
        return next((w for w in obs.windows if w.kind == "frame"), None)
    matches = [w for w in obs.windows if re.search(scope.window, w.title)]
    return matches[-1] if matches else None


def container_node(win: Window, scope: Scope) -> Node | None:
    if not scope.container:
        return None
    for n in win.nodes:
        if n.role == "internal frame" and _rx(scope.container, n.name):
            return n
    return None


def scoped_nodes(obs: Observation, scope: Scope, main_window: str) -> tuple[list[Node], Node | None, Window | None]:
    win = scope_window(obs, scope, main_window)
    if win is None:
        return [], None, None
    cont = container_node(win, scope)
    if scope.container and cont is None:
        return [], None, win
    nodes = [n for n in win.nodes if not scope.container or (n.container and _rx(scope.container, n.container))]
    return nodes, cont, win


def _name_matches(pattern: str, name: str, aliases: dict[str, str]) -> bool:
    candidates = [pattern] + ([aliases[pattern]] if pattern in aliases else [])
    for c in candidates:
        if c.startswith("^"):
            if re.search(c, name or ""):
                return True
        elif (name or "") == c:
            return True
    return False


def _candidates(strategy: Locator, nodes: list[Node], cont: Node | None, win: Window,
                label_aliases: dict[str, str], name_aliases: dict[str, str]) -> list[Node]:
    if isinstance(strategy, A11yLocator):
        return [n for n in nodes if n.role == strategy.role and _name_matches(strategy.name, n.name, name_aliases)]
    if isinstance(strategy, LabelLocator):
        return [n for n in nodes if n.role == strategy.role and n.label and
                _name_matches(strategy.label, n.label, label_aliases)]
    if isinstance(strategy, GroupLocator):
        same = [n for n in nodes if n.role == strategy.role and n.group == strategy.group and not n.hidden]
        return [same[strategy.index]] if len(same) > strategy.index else []
    if isinstance(strategy, PathLocator):
        base = cont.ref if cont else f"w{win.index}"
        want = f"{base}.{strategy.path}" if strategy.path else base
        return [n for n in nodes if n.ref == want and n.role == strategy.role]
    if isinstance(strategy, CoordLocator):
        box = cont.bounds if cont and cont.bounds else win.bounds
        x = box[0] + strategy.rel_x * box[2]
        y = box[1] + strategy.rel_y * box[3]
        hits = [n for n in nodes if n.bounds and not n.hidden and n.role not in STRUCTURAL_ROLES and
                n.bounds[0] <= x <= n.bounds[0] + n.bounds[2] and n.bounds[1] <= y <= n.bounds[1] + n.bounds[3]]
        hits.sort(key=lambda n: n.depth, reverse=True)
        return hits[:1]
    return []


def resolve(obs: Observation, target: Target, main_window: str,
            label_aliases: dict[str, str] | None = None, name_aliases: dict[str, str] | None = None) -> Resolution:
    nodes, cont, win = scoped_nodes(obs, target.scope, main_window)
    notes: list[str] = []
    if win is None:
        return Resolution(None, None, [f"scope window '{target.scope.window}' not present"])
    if target.scope.container and cont is None:
        return Resolution(None, None, [f"container '{target.scope.container}' not open"])
    for i, strat in enumerate(target.strategies):
        c = _candidates(strat, nodes, cont, win, label_aliases or {}, name_aliases or {})
        if len(c) == 1:
            return Resolution(c[0], i, notes)
        notes.append(f"strategy {i} ({strat.kind}): {len(c)} matches")
    return Resolution(None, None, notes)


# ----------------------------------------------------------------------------- record side

def build_target(obs: Observation, node: Node, main_window: str, description: str) -> Target:
    """Create a ranked, uniqueness-checked locator set for a node seen during discovery."""
    win = next(w for w in obs.windows if w.index == node.window_index)
    is_main = win.kind == "frame" and re.search(main_window, win.title) is not None
    scope = Scope(window="main" if is_main else f"^{rx_escape(win.title)}$",
                  container=f"^{rx_escape(node.container)}$" if node.container else None)
    cont = container_node(win, scope)
    strategies: list[Locator] = []
    if node.name and node.role != "label":
        strategies.append(A11yLocator(role=node.role, name=node.name))
    if node.label:
        strategies.append(LabelLocator(role=node.role, label=node.label))
    if node.group:
        same = [n for n in win.nodes if n.role == node.role and n.group == node.group and not n.hidden
                and (not node.container or n.container == node.container)]
        if node in same:
            strategies.append(GroupLocator(role=node.role, group=node.group, index=same.index(node)))
    base = cont.ref if cont else f"w{win.index}"
    if node.ref.startswith(base + "."):
        strategies.append(PathLocator(role=node.role, path=node.ref[len(base) + 1:]))
    box = cont.bounds if cont and cont.bounds else win.bounds
    if node.bounds and box and box[2] and box[3]:
        cx = node.bounds[0] + node.bounds[2] / 2
        cy = node.bounds[1] + node.bounds[3] / 2
        strategies.append(CoordLocator(rel_x=round((cx - box[0]) / box[2], 4), rel_y=round((cy - box[1]) / box[3], 4)))
    # Uniqueness check against the observation the node came from.
    nodes, cont2, _ = scoped_nodes(obs, scope, main_window)
    checked: list[Locator] = []
    for s in strategies:
        c = _candidates(s, nodes, cont2, win, {}, {})
        s.unique_at_record = len(c) == 1 and c[0].ref == node.ref
        checked.append(s)
    # Unique strategies first, preserving robustness order within each group.
    checked.sort(key=lambda s: 0 if s.unique_at_record else 1)
    return Target(description=description, scope=scope, strategies=checked)
