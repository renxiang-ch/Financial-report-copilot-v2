"""Shared plumbing for the v2 tool library.

Three things every tool uses:

* ``ToolError`` -- a typed exception. Tools ``raise`` it; the graph wires
  ``ToolRetryMiddleware`` (retries when ``retryable``) + ``ToolErrorMiddleware``
  (turns the rest into a model-visible ``ToolMessage``). Replaces v1's habit of
  returning ``{"found": false, "recoverable": true, ...}`` dicts.
* ``content_and_artifact`` return shape -- ``(text_for_model, full_dict)``. The
  text is a one-line summary the model reasons over; the dict rides along as
  ``ToolMessage.artifact`` (out of the model's context) for the runner to rebuild
  v1-shaped ``steps`` / citations / provenance.
* ``financial_tool`` -- ``@tool`` with that response format, an ``args_schema``,
  and an optional process-local memo (the read-only tools are pure within a DB
  snapshot; LangChain has no native tool-result cache).
"""

from __future__ import annotations

import enum
import json
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from langchain_core.tools import tool


class ToolErrorKind(enum.StrEnum):
    UNKNOWN_TICKER = "unknown_ticker"          # typo / not in DB -> retry with a real one
    WRONG_RELATION_SIDE = "wrong_relation_side"  # e.g. graph_query(supplier=<a customer>)
    NOT_FOUND = "not_found"                    # valid args, no such row -- terminal
    UNSUPPORTED = "unsupported"                # e.g. quarterly figures -- terminal
    BAD_ARGUMENT = "bad_argument"              # malformed / missing arg -> retry
    BAD_EXPRESSION = "bad_expression"          # compute: unparseable / unsafe -- terminal


# Kinds the model can fix by calling again with corrected arguments.
_RETRYABLE = {
    ToolErrorKind.UNKNOWN_TICKER,
    ToolErrorKind.WRONG_RELATION_SIDE,
    ToolErrorKind.BAD_ARGUMENT,
}


class ToolError(Exception):
    """A tool failure with a machine-readable kind and a hint written for the model.

    ``retryable`` defaults from ``kind`` (see ``_RETRYABLE``) but can be forced.
    ``data`` carries structured context (``did_you_mean``, ``asked_for``, ...)
    that the error middleware may fold into its message.
    """

    def __init__(self, kind: ToolErrorKind, hint: str, *,
                 retryable: bool | None = None, data: dict[str, Any] | None = None):
        self.kind = kind
        self.hint = hint
        self.retryable = _RETRYABLE.__contains__(kind) if retryable is None else retryable
        self.data = data or {}
        super().__init__(f"{kind.value}: {hint}")


def pack(content: str, artifact: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The ``content_and_artifact`` two-tuple. One call site so the shape is fixed."""
    return content, artifact


class _Memo:
    """Tiny bounded memo keyed on a JSON dump of the call's kwargs.

    Not an ``lru_cache``: tool calls arrive as kwargs and some values (``compute``'s
    ``variables``) are dicts, so hash the serialized form instead. Bounded FIFO.
    """

    def __init__(self, maxsize: int = 256):
        self._d: OrderedDict[str, Any] = OrderedDict()
        self._max = maxsize

    def get_or_call(self, fn: Callable, kwargs: dict) -> Any:
        try:
            key = json.dumps(kwargs, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return fn(**kwargs)  # unhashable-ish input: skip the cache
        if key in self._d:
            self._d.move_to_end(key)
            return self._d[key]
        val = fn(**kwargs)
        self._d[key] = val
        if len(self._d) > self._max:
            self._d.popitem(last=False)
        return val


def financial_tool(name: str, *, args_schema, description: str | None = None,
                   cache: bool = False):
    """``@tool`` for this library: content_and_artifact + args_schema + opt. memo.

    The wrapped function takes the schema's fields as kwargs (plus an optional
    ``runtime: ToolRuntime``) and returns ``pack(text, artifact)`` or raises
    ``ToolError``.
    """
    def deco(fn: Callable) -> Any:
        inner = fn
        if cache:
            memo = _Memo()

            def cached(**kwargs):
                # runtime is injected per-call and not part of the cache key;
                # tools that need it should not set cache=True.
                return memo.get_or_call(fn, kwargs)

            cached.__name__ = fn.__name__
            cached.__doc__ = fn.__doc__
            inner = cached
        return tool(name, description=description, args_schema=args_schema,
                    response_format="content_and_artifact")(inner)

    return deco
