"""compute -- arithmetic over named variables in an AST-whitelisted sandbox.

The sandbox (``_alias_non_identifier_vars`` / ``_SAFE_NODES`` / ``_MAX_EXPONENT``
/ ``_reject_unsafe`` / ``_depth`` / ``_levels``) is copied verbatim from
``copilot.agent.tools`` -- it is security-critical and the measured reasoning in
those docstrings still applies. Only the signature and the return/​error shape
change: ``(content, artifact)`` and ``ToolError`` instead of ``{"ok": ...}``.

The LLM never does arithmetic itself; numbers come from ``query_financials``.
"""

from __future__ import annotations

import ast
import re

from copilot.v2.tools.base import ToolError, ToolErrorKind, financial_tool, pack
from copilot.v2.tools.schemas import ComputeArgs


def _alias_non_identifier_vars(expression: str, allowed: dict) -> tuple[str, dict]:
    """Rewrite variable names that are not valid Python identifiers (`R&D`, `PP&E`,
    `D&A`, `D&A_Component`) into ones that are. Longest-key-first, boundary-guarded.
    The alias is internal; the returned artifact reports the caller's originals.
    """
    namespace: dict = {}
    rewritten = expression
    for i, key in enumerate(sorted(allowed, key=len, reverse=True)):
        if key.isidentifier():
            namespace[key] = allowed[key]
            continue
        alias = f"_v{i}_"
        pattern = rf"(?<![\w&]){re.escape(key)}(?![\w&])"
        new_text, n = re.subn(pattern, alias, rewritten)
        if n:
            rewritten = new_text
            namespace[alias] = allowed[key]
        else:
            namespace[key] = allowed[key]
    return rewritten, namespace


_SAFE_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd,
)
_MAX_EXPONENT = 64


def _reject_unsafe(expression: str, namespace: dict) -> str | None:
    """Return a reason to refuse, or None if the expression is plain arithmetic."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        return f"could not parse expression: {e}"
    for node in ast.walk(tree):
        if not isinstance(node, _SAFE_NODES):
            return (f"{type(node).__name__} is not allowed here; compute evaluates "
                    f"arithmetic over the supplied variables only")
    powers = [(_depth(tree, n), n) for n in ast.walk(tree)
              if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow)]
    for _, node in sorted(powers, key=lambda p: -p[0]):
        try:
            value = eval(compile(ast.Expression(node.right), "<exp>", "eval"),  # noqa: S307
                         {"__builtins__": {}}, namespace)
        except Exception as e:  # noqa: BLE001
            return f"exponent could not be evaluated: {e}"
        if not isinstance(value, (int, float)) or abs(value) > _MAX_EXPONENT:
            return (f"exponent {value} exceeds the limit of {_MAX_EXPONENT}; a "
                    f"financial ratio, root or CAGR does not need one that large")
    return None


def _depth(tree: ast.AST, target: ast.AST) -> int:
    for depth, level in enumerate(_levels(tree)):
        if any(n is target for n in level):
            return depth
    return 0


def _levels(tree: ast.AST):
    level = [tree]
    while level:
        yield level
        level = [c for n in level for c in ast.iter_child_nodes(n)]


@financial_tool("compute", args_schema=ComputeArgs,
                description="Evaluate an arithmetic expression over named variables. "
                "Numbers must come from query_financials, never inline literals.")
def compute(expression: str, variables: dict):
    allowed = {k: v for k, v in variables.items() if isinstance(v, (int, float))}
    evaluated, namespace = _alias_non_identifier_vars(expression, allowed)
    rejected = _reject_unsafe(evaluated, namespace)
    if rejected:
        raise ToolError(ToolErrorKind.BAD_EXPRESSION, rejected, retryable=False,
                        data={"expression": expression})
    try:
        result = float(eval(evaluated, {"__builtins__": {}}, namespace))  # noqa: S307
    except Exception as e:  # noqa: BLE001
        raise ToolError(ToolErrorKind.BAD_EXPRESSION, str(e), retryable=False,
                        data={"expression": expression}) from e
    artifact = {"ok": True, "result": result, "expression": expression,
                "variables": variables}
    return pack(f"{expression} = {result:,}", artifact)
