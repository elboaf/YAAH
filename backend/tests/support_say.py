"""Shared say-test helpers (#311).

One implementation each of:

- ``restored_config`` / ``restored_config_ctx`` — the config-restore
  protocol (conftest writes config.json once per session; a test that
  flips config MUST restore it or the mutation poisons every later test,
  including the prompt-manifest drift guard);
- ``config_with_voice`` — a merged config dict with voice overrides,
  built WITHOUT mutating the shared DEFAULTS (``load_config`` returns a
  shallow copy, so ``cfg["voice"]["say_emissions"] = ...`` would flip the
  module-level dict for every later test in the process — PR #150's
  cfg-fixture lesson) — and ``save_config_with_voice``, which additionally
  saves it in place;
- ``say_section`` — the spoken-briefing section, sliced between its
  opening line and the next section (ask-user).
"""
import os
from contextlib import contextmanager

import pytest


@contextmanager
def restored_config_ctx():
    path = os.environ["YAAH_CONFIG_PATH"]
    with open(path, "rb") as f:
        saved = f.read()
    try:
        yield
    finally:
        with open(path, "wb") as f:
            f.write(saved)


@pytest.fixture
def restored_config():
    """Context-manager form for pytest: ``def test_x(restored_config):``."""
    with restored_config_ctx():
        yield


def config_with_voice(**overrides):
    """A config dict with voice overrides merged in (NOT saved)."""
    from backend.agent.config import load_config

    voice = {**(load_config().get("voice") or {}), **overrides}
    return {**load_config(), "voice": voice}


def save_config_with_voice(**overrides):
    """Merge voice overrides and save config.json in place."""
    from backend.agent.config import save_config

    save_config(config_with_voice(**overrides))


def say_section(prompt: str) -> str:
    section = prompt.split("Spoken briefing (voice read-aloud):\n", 1)[1]
    return section.split("\nInterview the user", 1)[0]
