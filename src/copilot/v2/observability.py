"""LangSmith tracing for the v2 graph -- env bridge, project scoping, metadata.

``create_agent`` traces to LangSmith with no code changes *provided the SDK can
see its environment variables*. In this project it cannot, for two reasons that
both fail **silently** -- nothing raises, traces just never arrive:

1. ``copilot.config`` reads ``.env`` through pydantic-settings, which does not
   export to ``os.environ``, while the ``langsmith`` SDK reads ``os.environ``
   directly. Same root cause as ``init_chat_model("openai:...")`` not finding the
   OpenAI key (devlog 004 D1). ``LANGSMITH_*`` is not in ``Settings`` either --
   ``config.py`` is on the shared/frozen list (devlog 007 D3) -- so ``settings``
   cannot supply it.
2. ``langsmith.utils.get_env_var`` is ``@lru_cache``d. Anything that asks the SDK
   whether tracing is on *before* the bridge runs caches "off" for the life of
   the process, so bridging alone is not enough: the cache must be cleared.

``enable_tracing()`` closes both and is idempotent.

**Off by default** (``LANGSMITH_TRACING=false``), per devlog 007 D1: eval sweeps
must stay reproducible and must not acquire a network dependency. Only the
``LANGSMITH_*`` keys are bridged -- loading all of ``.env`` into the environment
would silently change how other libraries resolve credentials.

What tracing buys that this codebase cannot get otherwise: ``runner.py``
reconstructs ``steps`` by pairing ``AIMessage.tool_calls`` with ``ToolMessage``
after the fact, which is enough to score but carries no attribution -- no
per-hook timing, no "which retry happened where", no token split across the
middleware stack. Those are spans, and only the tracer emits them.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator

log = logging.getLogger(__name__)

# Bridged from `.env` when absent from the environment. ENDPOINT matters for
# non-US accounts: without it the key is not recognised and auth fails.
_BRIDGED_KEYS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGSMITH_ENDPOINT",
)

_bridged = False


def enable_tracing() -> bool:
    """Bridge ``.env`` -> ``os.environ``, then report whether tracing is live.

    Idempotent: the file is read once per process, the status re-checked on every
    call. Returns ``False`` (with a warning) when tracing is switched on but no
    API key is reachable -- precisely the silent failure this module exists to
    prevent.
    """
    global _bridged
    from langsmith import utils

    if not _bridged:
        from dotenv import dotenv_values

        # Reuse config's already-resolved path instead of recomputing the depth
        # from __file__: getting that wrong is how ab_compare's _REPO_ROOT broke.
        from copilot.config import _ENV_FILE

        values = dotenv_values(_ENV_FILE) if _ENV_FILE.exists() else {}
        for key in _BRIDGED_KEYS:
            val = (values.get(key) or "").strip()
            # An explicit shell `export` wins over the file.
            if val and not os.environ.get(key):
                os.environ[key] = val
        utils.get_env_var.cache_clear()  # docstring, reason 2
        _bridged = True

    if not utils.tracing_is_enabled():
        return False
    if not os.environ.get("LANGSMITH_API_KEY"):
        log.warning(
            "LANGSMITH_TRACING is on but LANGSMITH_API_KEY is empty -- runs will "
            "not reach LangSmith. Set it in .env (and LANGSMITH_ENDPOINT too, if "
            "the account is not in the US region)."
        )
        return False
    return True


@contextlib.contextmanager
def tracing_project(project: str, **metadata) -> Iterator[None]:
    """Send traces from this block to ``project``, stamped with ``metadata``.

    A no-op when tracing is off, so callers wrap unconditionally.

    Two jobs: keep eval sweeps -- hundreds of runs per pass -- out of the project
    used for interactive debugging, and stamp each run with its dataset item id so
    one specific result can be found in the UI afterwards. The second is the point
    for this project: judge scores that flap between runs (2.57 <-> 2.43) are
    currently diagnosed by re-running and counting, because there is no way to
    open *the* trace for ``ret_glw_business_segments`` and look.
    """
    if not enable_tracing():
        yield
        return

    import langsmith as ls

    clean = {k: v for k, v in metadata.items() if v is not None}
    with ls.tracing_context(project_name=project, enabled=True,
                            metadata=clean or None):
        yield
