"""Tool surface offered to the discovery model. Deliberately small and ref-based.

The model never produces coordinates or raw selectors: it points at refs from the current
accessibility outline, and the compiler turns each pointed-at element into ranked locators.
The model also never sees or types sensitive values: inputs are referenced by parameter name,
extracted values are captured harness-side and hidden from the transcript.
"""
from __future__ import annotations

WHY = {"type": "string", "description": "One short sentence: why this action moves toward the goal."}

TOOLS = [
    {
        "name": "click",
        "description": "Activate a button, menu item, tab, or other actionable element by ref. Menu items listed "
                       "under a menu can be clicked directly without opening the menu.",
        "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}, "why": WHY},
                         "required": ["ref", "why"], "additionalProperties": False},
    },
    {
        "name": "type_text",
        "description": "Replace the contents of a text field. For declared goal inputs you MUST pass `param` (the "
                       "input name) instead of `text`; the harness supplies the value. Use `text` only for fixed "
                       "values that are not inputs.",
        "input_schema": {"type": "object", "properties": {
            "ref": {"type": "string"}, "param": {"type": "string"}, "text": {"type": "string"}, "why": WHY},
            "required": ["ref", "why"], "additionalProperties": False},
    },
    {
        "name": "select_option",
        "description": "Choose an option in a combo box / drop-down. Pass `param` if the choice is a declared input.",
        "input_schema": {"type": "object", "properties": {
            "ref": {"type": "string"}, "option": {"type": "string"}, "param": {"type": "string"}, "why": WHY},
            "required": ["ref", "why"], "additionalProperties": False},
    },
    {
        "name": "press_key",
        "description": "Press a key in the active window: ENTER, ESCAPE, TAB, or a function key like F2.",
        "input_schema": {"type": "object", "properties": {"key": {"type": "string"}, "why": WHY},
                         "required": ["key", "why"], "additionalProperties": False},
    },
    {
        "name": "extract",
        "description": "Capture a declared output from an element. For a table, give row_column + row_contains to "
                       "pick the row and column for the cell. The captured value is hidden from you on purpose.",
        "input_schema": {"type": "object", "properties": {
            "ref": {"type": "string"}, "output": {"type": "string"},
            "row_column": {"type": "string"}, "row_contains": {"type": "string"}, "column": {"type": "string"},
            "pattern": {"type": "string", "description": "Optional regex with one capture group to isolate the "
                                                         "value inside longer text, e.g. 'CONF(?:IRMATION)? #: (\S+)'"},
            "why": WHY},
            "required": ["ref", "output", "why"], "additionalProperties": False},
    },
    {
        "name": "wait",
        "description": "Wait for the application (e.g. while it shows PROCESSING...).",
        "input_schema": {"type": "object", "properties": {"seconds": {"type": "number"}, "why": WHY},
                         "required": ["seconds", "why"], "additionalProperties": False},
    },
    {
        "name": "ask_human",
        "description": "Pause and hand the live session to a human operator. Use when stuck, when a sign-on or "
                       "supervisor credential is needed, or when an action is blocked by policy and needs approval.",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}},
                         "required": ["reason"], "additionalProperties": False},
    },
    {
        "name": "finish",
        "description": "End the run. success=true only if the goal is reached and every declared output was "
                       "extracted. proof_text: the exact on-screen text that proves success (e.g. a status line).",
        "input_schema": {"type": "object", "properties": {
            "success": {"type": "boolean"}, "summary": {"type": "string"}, "proof_text": {"type": "string"}},
            "required": ["success", "summary"], "additionalProperties": False},
    },
]

SYSTEM_PROMPT = """You are the discovery agent in a computer-use automation system for credit-union back-office \
software. You operate a live legacy desktop application for a human bank employee. What you do once is \
compiled into a deterministic, reusable automation, so prefer the path a careful operator would take \
every time: named buttons, labeled fields, menu items, function keys.

Each turn you receive the current screen as an outline of UI elements (with refs) and a screenshot. \
Personal and financial data is masked ([PII], [SSN], [AMOUNT], [MEMBER_NUMBER]); that is intentional and \
you never need the raw values.

How to work:
- Take one action per turn and look at the result before the next one.
- Goal inputs are referenced by name: call type_text / select_option with `param`, never with the literal value.
- When a declared output is visible, call extract on the element that holds it.
- The session is already signed on. Never try to enter or guess credentials; ask_human if a sign-on or \
supervisor credential is requested.
- Some actions are irreversible (posting money, submitting). Policy gates them; if an action is blocked or \
needs approval, the harness will get a human decision. Never look for a way around the policy.
- If an error message, unexpected dialog, or dead end appears that you cannot resolve safely, call ask_human.
- When the goal is reached and all outputs are extracted, call finish with success=true and the proof_text \
that shows it. If the goal turns out to be impossible (for example the record does not exist), call finish \
with success=false and explain."""
