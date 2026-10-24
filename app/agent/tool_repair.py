"""tool_repair — port of OpenClaw's tool-call-repair layer.

When the MiniMax-M3 model returns text only (no Anthropic tool_use
blocks), it has often narrated the call in prose. This module
extracts those narrations and converts them to real ToolCall
objects so the agent loop can actually execute the tool.

Patterns supported (all best-effort, never raise):
  1. [tool:NAME]\n{json}\n[/NAME]              (OpenClaw bracketed)
  2. [NAME]\n{json}                            (OpenClaw bracketed, no prefix)
  3. <function=NAME>\n<parameter=k>v</parameter>\n</function>
                                               (OpenClaw XML)
  4. <|channel|>commentary to=NAME code\n{json}  (OpenClaw Harmony)
  5. NAME(args)                                  (NEW: Python-style)

See payload-C252NdEX.js in OpenClaw dist for the upstream reference.
"""
from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass
from typing import Any


log = logging.getLogger(__name__)


@dataclass
class RepairedToolCall:
    name: str
    arguments: dict
    raw_text: str  # the matched text, useful for stripping from response.text


# ----------------------------------------------------------------------------
# 1. [tool:NAME]{json}[/NAME]  OR  [NAME]{json}
# ----------------------------------------------------------------------------

_BRACKET_OPEN_RE = re.compile(
    r"\s*(?:\[tool:([A-Za-z0-9_-]+)\]|\[([A-Za-z0-9_-]+)\])"
)


def _parse_bracket(text: str, start: int, allowed: set[str], tool_schemas: dict | None = None) -> RepairedToolCall | None:
    m = _BRACKET_OPEN_RE.match(text, start)
    if not m:
        return None
    name = m.group(1) or m.group(2)
    if name not in allowed:
        return None
    pos = m.end()
    # The name "[memory_recall]" variant expects {json} on its own line
    # (or directly after). The "tool:" variant expects a JSON object after.
    # Try to grab the next JSON object from pos.
    # Skip leading whitespace / newlines.
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    obj_match = re.match(r"\{", text[pos:])
    if not obj_match:
        return None
    # Find matching closing brace with simple string/escape awareness.
    obj_end = _find_json_object_end(text, pos)
    if obj_end is None:
        return None
    raw_json = text[pos:obj_end]
    try:
        args = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(args, dict):
        return None
    # Look for closing [/name]
    close_pat = f"\[/\s*{re.escape(name)}\s*\]"
    close_m = re.match(close_pat, text[obj_end:])
    end = obj_end + (close_m.end() if close_m else 0)
    raw = text[m.start():end] if close_m else text[m.start():obj_end]
    return RepairedToolCall(name=name, arguments=args, raw_text=raw)


def _find_json_object_end(text: str, start: int) -> int | None:
    """Find the index just past the closing '}' of the JSON object at start.
    Handles strings (with escaped quotes) and nested objects/arrays."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
    return None


# ----------------------------------------------------------------------------
# 2. <function=NAME>...<parameter=k>v</parameter>...</function>
# ----------------------------------------------------------------------------

_XMLISH_OPEN_RE = re.compile(
    r"\s*<function=([A-Za-z0-9_.:-]{1,120})>\s*",
    re.IGNORECASE,
)
_XMLISH_PARAM_RE = re.compile(
    r"<parameter=([A-Za-z0-9_.:-]{1,120})>(.*?)</parameter>",
    re.IGNORECASE | re.DOTALL,
)
_XMLISH_CLOSE_RE = re.compile(
    r"\s*</function>\s*",
    re.IGNORECASE,
)


def _parse_xmlish(text: str, start: int, allowed: set[str], tool_schemas: dict | None = None) -> RepairedToolCall | None:
    m = _XMLISH_OPEN_RE.match(text, start)
    if not m:
        return None
    name = m.group(1)
    if name not in allowed:
        return None
    pos = m.end()
    args: dict = {}
    while True:
        # Skip whitespace between parameters (e.g. \n).
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        pm = _XMLISH_PARAM_RE.match(text, pos)
        if not pm:
            break
        key, raw_value = pm.group(1), pm.group(2).strip()
        # Try to parse the value as JSON; fall back to string.
        try:
            value = json.loads(raw_value)
        except (json.JSONDecodeError, ValueError):
            value = _strip_xmlish_value(raw_value)
        args[key] = value
        pos = pm.end()
    # Skip whitespace before the closing tag too.
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    cm = _XMLISH_CLOSE_RE.match(text, pos)
    if not cm:
        return None
    end = cm.end()
    return RepairedToolCall(
        name=name,
        arguments=args,
        raw_text=text[m.start():end],
    )


def _strip_xmlish_value(s: str) -> str:
    """Strip surrounding whitespace and a leading newline from XML
    parameter values. Mirrors the upstream behaviour."""
    s = s.strip("\n")
    return s.strip()


# ----------------------------------------------------------------------------
# 3. <|channel|>commentary to=NAME code\n{json}
# ----------------------------------------------------------------------------

_HARMONY_OPEN_RE = re.compile(
    r"\s*<\|channel\|>(commentary|analysis|final)\s+to=([A-Za-z0-9_-]+)\s+code",
    re.IGNORECASE,
)


def _parse_harmony(text: str, start: int, allowed: set[str], tool_schemas: dict | None = None) -> RepairedToolCall | None:
    m = _HARMONY_OPEN_RE.match(text, start)
    if not m:
        return None
    name = m.group(2)
    if name not in allowed:
        return None
    pos = m.end()
    # The next non-whitespace should be a JSON object.
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    if pos >= len(text) or text[pos] != "{":
        return None
    obj_end = _find_json_object_end(text, pos)
    if obj_end is None:
        return None
    raw_json = text[pos:obj_end]
    try:
        args = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(args, dict):
        return None
    return RepairedToolCall(
        name=name,
        arguments=args,
        raw_text=text[m.start():obj_end],
    )


# ----------------------------------------------------------------------------
# 4. NAME(args) — Python-style, what our model is producing
# ----------------------------------------------------------------------------

_PYTHON_CALL_RE = re.compile(
    r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(",
)


def _parse_python_call(
    text: str,
    start: int,
    allowed: set[str],
    tool_schemas: dict[str, list[str]] | None = None,
) -> RepairedToolCall | None:
    """Parse `name(arg1, key=value, key2='string')` style tool calls.

    Uses ast.literal_eval on the inside of the parens to handle
    Python literals (strings, numbers, bool, None, lists, dicts).
    Positional args are mapped to the tool's parameter names in
    declaration order using `tool_schemas` (when provided). When
    positional args exceed the declared count, the extras are
    dropped (with a debug log).
    """
    m = _PYTHON_CALL_RE.match(text, start)
    if not m:
        return None
    name = m.group(1)
    if name not in allowed:
        return None
    pos = m.end()  # just past the opening paren
    # Find the matching closing paren, respecting strings.
    depth = 1
    in_str = False
    quote = ""
    escape = False
    i = pos
    while i < len(text):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == quote:
                in_str = False
                quote = ""
        else:
            if c in ('"', "\'") and not in_str:
                in_str = True
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
        i += 1
    if depth != 0 or in_str:
        return None
    args_str = text[pos:i].strip()
    end = i + 1  # past the closing paren
    # Parse the args. Use a permissive approach: if literal_eval fails,
    # try a simpler split on commas.
    parsed = _parse_python_args(args_str)
    if parsed is None:
        return None
    # Merge positional args with kwargs. Positional args are mapped to
    # the tool's declared parameter names in order; kwargs override.
    positional, kwargs = parsed
    out_args: dict = {}
    if tool_schemas:
        param_names = tool_schemas.get(name) or []
        for idx, val in enumerate(positional):
            if idx < len(param_names):
                out_args[param_names[idx]] = val
            else:
                log.debug(
                    "tool_repair: %s has %d declared params but %d positional args; dropping extras",
                    name, len(param_names), len(positional),
                )
    out_args.update(kwargs)
    return RepairedToolCall(
        name=name,
        arguments=out_args,
        raw_text=text[m.start():end],
    )


def _parse_python_args(s: str) -> tuple[list, dict] | None:
    """Parse Python-style positional + keyword args.

    Returns (positional_list, kwargs_dict). The caller is responsible
    for mapping positional args to the tool's declared parameter names
    (using tool_schemas). If the input can't be parsed, returns None.
    """
    if not s:
        return [], {}
    try:
        tree = ast.parse("f(" + s + ")", mode="eval")
    except SyntaxError:
        return None
    call = tree.body
    if not isinstance(call, ast.Call):
        return None
    positional: list = [_ast_to_python(a) for a in call.args]
    kwargs: dict = {}
    for kw in call.keywords:
        if kw.arg is None:
            # **kwargs expansion — not supported in our tools
            return None
        kwargs[kw.arg] = _ast_to_python(kw.value)
    return positional, kwargs


def _ast_to_python(node: ast.AST) -> Any:
    """Convert a parsed AST literal to its Python value."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id == "True":
        return True
    if isinstance(node, ast.Name) and node.id == "False":
        return False
    if isinstance(node, ast.Name) and node.id == "None":
        return None
    if isinstance(node, ast.List):
        return [_ast_to_python(x) for x in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_ast_to_python(x) for x in node.elts)
    if isinstance(node, ast.Dict):
        return {_ast_to_python(k): _ast_to_python(v) for k, v in zip(node.keys, node.values)}
    # Fall back to literal_eval
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None


# ----------------------------------------------------------------------------
# 5. Main entry point: extract a list of RepairedToolCall from text
# ----------------------------------------------------------------------------

def repair_text_to_tool_calls(
    text: str,
    allowed_tool_names: set[str] | list[str],
    tool_schemas: dict[str, list[str]] | None = None,
) -> list[RepairedToolCall]:
    """Run the text through all parsers and return any tool calls found.

    Each successful parse removes the matched text from the input
    via a single forward cursor; the parsers are tried in order at
    each cursor position. The first one that matches wins. We
    advance by one character when no parser matches (rather than
    skipping to next whitespace) so we don't accidentally skip past
    tool calls embedded in prose / backticks / markdown.

    `tool_schemas` maps tool name -> ordered list of parameter
    names. Used by the Python parser to map positional args
    (e.g. `name("foo", top_k=5)`) to their declared names
    (e.g. `{"query": "foo", "top_k": 5}`).
    """
    if not text:
        return []
    allowed = set(allowed_tool_names) if not isinstance(allowed_tool_names, set) else allowed_tool_names
    out: list[RepairedToolCall] = []
    cursor = 0
    # Skip leading whitespace
    while cursor < len(text) and text[cursor] in " \t\r\n":
        cursor += 1
    while cursor < len(text):
        # Try each parser in order; the first hit wins.
        for parser in (_parse_bracket, _parse_xmlish, _parse_harmony, _parse_python_call):
            try:
                result = parser(text, cursor, allowed, tool_schemas)
            except Exception as e:
                log.debug("tool_repair: parser %s failed: %s", parser.__name__, e)
                result = None
            if result is not None:
                out.append(result)
                cursor = _advance_past(text, cursor, result.raw_text)
                break
        else:
            # No parser matched at this position. Advance by one char
            # so we search the whole text for tool calls (rather than
            # only at whitespace boundaries, which would skip past
            # tool calls embedded in prose like backticks or brackets).
            cursor += 1
        while cursor < len(text) and text[cursor] in " \t\r\n":
            cursor += 1
    return out


def _advance_past(text: str, start: int, raw: str) -> int:
    """Advance cursor past the matched raw text. If raw isn't found at
    start, return start (caller will skip a char)."""
    if not raw:
        return start
    # raw might span across newlines; do a substring search from start
    idx = text.find(raw, start)
    if idx == -1:
        return start + len(raw)
    return idx + len(raw)


# ----------------------------------------------------------------------------
# 6. Strip the matched raw text from response.text for user-visible output
# ----------------------------------------------------------------------------

def strip_repaired_blocks(text: str, blocks: list[RepairedToolCall]) -> str:
    """Return response.text with the repaired blocks' raw_text removed.
    If no blocks were matched, returns text unchanged."""
    if not blocks or not text:
        return text
    out = text
    for b in blocks:
        if b.raw_text and b.raw_text in out:
            out = out.replace(b.raw_text, "", 1)
    # Collapse runs of blank lines left behind by the strip
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()
