"""Smoke tests — verify all core modules import without error."""


def test_config_loads():
    from copilot.config import settings
    assert isinstance(settings.log_level, str)


def test_tools_importable():
    from copilot.agent import tools  # noqa: F401


def test_api_importable():
    from copilot import api  # noqa: F401


def test_agent_importable():
    from copilot.agent import agent  # noqa: F401
