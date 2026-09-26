"""Redaction for anything that leaves process memory: logs, evidence, artifacts, and LLM prompts.

Three layers, applied together:
1. Pattern rules for regulated identifiers (SSN, DOB-like dates, card/account numbers, money).
2. Label rules: values shown next to labels the app profile marks as PII ("SSN:", "Address:").
3. Run-scoped literals: values of inputs/outputs declared sensitive, registered at run start,
   so a member number typed into a field never appears verbatim in a log line.

Sensitive values are replaced by typed placeholders. When evidence must prove a value was read
correctly without storing it, use fingerprint(): a salted-free short hash a reviewer can check
against a known test value.
"""
from __future__ import annotations

import hashlib
import io
import re
from typing import Any, Iterable

from PIL import Image, ImageDraw

from .surface.base import Node, Observation

PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])/(19|20)\d{2}\b"), "[DATE]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[CARD_OR_ACCOUNT]"),
    (re.compile(r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\$\s?\d+(?:\.\d{2})?"), "[AMOUNT]"),
]


def fingerprint(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:12]


class Redactor:
    def __init__(self, pii_labels: Iterable[str] = (), mask_amounts: bool = True):
        self.pii_labels = {l.strip().rstrip(":").lower() for l in pii_labels}
        self.literals: dict[str, str] = {}
        self.mask_amounts = mask_amounts

    def register(self, value: Any, tag: str) -> None:
        s = str(value).strip() if value is not None else ""
        if len(s) >= 3:
            self.literals[s] = f"[{tag.upper()}]"

    def text(self, s: str | None) -> str | None:
        if not s:
            return s
        for lit, tag in sorted(self.literals.items(), key=lambda kv: -len(kv[0])):
            s = s.replace(lit, tag)
        for pat, tag in PATTERNS:
            if tag == "[AMOUNT]" and not self.mask_amounts:
                continue
            s = pat.sub(tag, s)
        return s

    def obj(self, o: Any) -> Any:
        """Deep-redact strings inside JSON-like structures."""
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: ("[SECRET]" if k in {"password", "secret", "token"} else self.obj(v)) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o

    # -- observation-level
    def learn(self, obs: Observation) -> None:
        """Remember the concrete PII values on screen (e.g. the value next to 'Name:') as run-scoped literals,
        so they are masked wherever they reappear later: diffs, review screens, dialog text, log lines."""
        sensitive = self.sensitive_nodes(obs)
        for n in obs.nodes():
            if n.ref not in sensitive:
                continue
            for t in (n.name, n.value):
                t = (t or "").strip()
                if len(t) >= 3 and "[" not in t and self.text(t) == t:  # skip text patterns already mask
                    self.literals[t] = "[PII]"

    def sensitive_nodes(self, obs: Observation) -> set[str]:
        """Refs of nodes whose content is PII: pattern hits, registered literals, or values next to PII labels."""
        refs: set[str] = set()
        for w in obs.windows:
            labels = [n for n in w.nodes if n.role == "label" and n.bounds and
                      n.name.strip().rstrip(":").lower() in self.pii_labels]
            for n in w.nodes:
                if n.hidden:
                    continue
                t = n.text()
                if t and self.text(t) != t:
                    refs.add(n.ref)
                if n.label and n.label.strip().rstrip(":").lower() in self.pii_labels:
                    refs.add(n.ref)
                if n.role == "label" and n.bounds and labels:
                    for lab in labels:
                        if lab.ref != n.ref and _same_row_right_of(n, lab):
                            refs.add(n.ref)
        return refs

    def node_text(self, n: Node, sensitive: set[str]) -> tuple[str, str | None]:
        """Redacted (name, value) for rendering a node to logs or the model."""
        if n.ref in sensitive and n.role == "label":
            return "[PII]", None
        name = self.text(n.name) or ""
        value = self.text(n.value) if n.value is not None else None
        if n.ref in sensitive and n.value:
            value = "[PII]"
        return name, value

    def screenshot(self, png: bytes, origin: tuple[int, int], obs: Observation) -> bytes:
        """Black out the screen regions of sensitive nodes, using accessibility bounds."""
        img = Image.open(io.BytesIO(png)).convert("RGB")
        draw = ImageDraw.Draw(img)
        ox, oy = origin
        for n in obs.nodes():
            if n.ref in self.sensitive_nodes(obs) and n.bounds:
                x, y, w, h = n.bounds
                draw.rectangle([x - ox, y - oy, x - ox + w, y - oy + h], fill=(0, 0, 0))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()


def _same_row_right_of(n: Node, lab: Node) -> bool:
    x, y, w, h = n.bounds  # type: ignore[misc]
    lx, ly, lw, lh = lab.bounds  # type: ignore[misc]
    return abs((y + h / 2) - (ly + lh / 2)) <= 8 and x >= lx + lw - 2 and x - (lx + lw) < 400 \
        and n.container == lab.container
