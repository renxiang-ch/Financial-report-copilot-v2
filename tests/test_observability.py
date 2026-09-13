"""LangSmith env bridge -- the two silent failures it exists to prevent.

Both are regression tests for real traps, not hypotheticals: `.env` values are
invisible to the langsmith SDK (pydantic-settings does not export them), and the
SDK caches its env lookups, so bridging alone does not take effect.
"""

from __future__ import annotations

import logging
import os

import pytest
from langsmith import utils as ls_utils

from copilot.v2 import observability

_KEYS = observability._BRIDGED_KEYS


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Fresh process state per test: no LANGSMITH_* in the environment, an empty
    `.env`, an un-run bridge, and a cold SDK cache.

    Teardown restores the environment **by hand** rather than through monkeypatch,
    because monkeypatch can only undo writes it made itself -- and the whole job of
    the code under test is to write to ``os.environ`` directly. Leaving that to
    monkeypatch leaks ``LANGSMITH_TRACING=true`` into every later test, whose tool
    calls then get POSTed to LangSmith with this file's fake key.
    """
    saved = {key: os.environ.get(key) for key in _KEYS}
    for key in _KEYS:
        os.environ.pop(key, None)

    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr("copilot.config._ENV_FILE", env_file)
    monkeypatch.setattr(observability, "_bridged", False)
    ls_utils.get_env_var.cache_clear()

    yield env_file

    for key, val in saved.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    ls_utils.get_env_var.cache_clear()


def _write(env_file, **pairs):
    env_file.write_text("\n".join(f"{k}={v}" for k, v in pairs.items()) + "\n")


def test_off_by_default(_isolate):
    """No configuration at all -> tracing stays off, nothing raises."""
    assert observability.enable_tracing() is False


def test_bridges_dotenv_into_environ(_isolate):
    """The whole point: values reachable only via `.env` must reach os.environ,
    because that is the only place the langsmith SDK looks."""
    _write(_isolate, LANGSMITH_TRACING="true", LANGSMITH_API_KEY="lsv2_test",
           LANGSMITH_ENDPOINT="https://eu.api.smith.langchain.com")

    assert observability.enable_tracing() is True
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_test"
    assert os.environ["LANGSMITH_ENDPOINT"] == "https://eu.api.smith.langchain.com"


def test_stale_sdk_cache_is_cleared(_isolate):
    """The trap that makes bridging alone insufficient.

    `langsmith.utils.get_env_var` is lru_cached. Anything that asks the SDK
    whether tracing is on before the bridge runs caches "off" for the life of the
    process -- so enabling tracing later would silently do nothing.
    """
    # Something asks first, priming the cache with "off".
    assert ls_utils.tracing_is_enabled() is False

    _write(_isolate, LANGSMITH_TRACING="true", LANGSMITH_API_KEY="lsv2_test")

    assert observability.enable_tracing() is True, "stale lru_cache not cleared"


def test_shell_export_wins_over_dotenv(_isolate, monkeypatch):
    """An explicit `export` is the more specific signal; the file must not
    clobber it."""
    monkeypatch.setenv("LANGSMITH_API_KEY", "from_shell")
    _write(_isolate, LANGSMITH_TRACING="true", LANGSMITH_API_KEY="from_dotenv")

    observability.enable_tracing()
    assert os.environ["LANGSMITH_API_KEY"] == "from_shell"


def test_tracing_on_without_key_warns_and_reports_false(_isolate, caplog):
    """`TRACING=true` with no key is exactly the silent failure this module
    exists to prevent: the SDK reports enabled, nothing ever arrives."""
    _write(_isolate, LANGSMITH_TRACING="true")

    with caplog.at_level(logging.WARNING):
        assert observability.enable_tracing() is False
    assert "LANGSMITH_API_KEY" in caplog.text


def test_tracing_project_is_a_noop_when_off(_isolate):
    """Eval code wraps unconditionally, so the disabled path must not raise and
    must not require langsmith to be configured."""
    with observability.tracing_project("frc-eval-graph", item="t1_aapl_revenue"):
        pass


def test_tracing_project_scopes_project_and_metadata(_isolate):
    """When on, the block runs inside a tracing context carrying the project and
    the item id -- the stamp that makes one run findable in the UI later."""
    from langsmith.run_helpers import get_tracing_context

    _write(_isolate, LANGSMITH_TRACING="true", LANGSMITH_API_KEY="lsv2_test")

    with observability.tracing_project("frc-eval-graph", item="ret_glw_segments",
                                       tier=None):
        ctx = get_tracing_context()

    assert ctx["project_name"] == "frc-eval-graph"
    assert ctx["metadata"]["item"] == "ret_glw_segments"
    assert "tier" not in ctx["metadata"], "None-valued metadata should be dropped"


def test_bridge_reads_dotenv_once(_isolate, monkeypatch):
    """Called per `build_agent()` and per eval item, so it must not re-read the
    file every time."""
    import dotenv

    calls = []
    original = dotenv.dotenv_values

    def counting(*a, **kw):
        calls.append(1)
        return original(*a, **kw)

    monkeypatch.setattr(dotenv, "dotenv_values", counting)

    observability.enable_tracing()
    observability.enable_tracing()
    observability.enable_tracing()

    assert len(calls) == 1
