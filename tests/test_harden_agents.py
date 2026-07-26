"""Agent-surface hardening regressions (audit 2026-07-25, group 'agents').

One section per finding:

* AI-01 — ``run_code`` executed LLM-authored Python with no AST guard unless the
  source happened to look like a strategy, and the sandbox child ran in the repo
  root with full network egress while the tool description promised neither.
* AI-02 — the per-task tool-category deny was inert for every task type except
  three, so ``_CONTEXT_DEFAULT_DENY`` never applied to the code-authoring
  ``generate_strategies`` flow (or anything else unlisted).
* AI-03 — ``_credentialed_chain`` appended un-configured credentialed providers,
  re-creating the cross-provider hop the no-auto-fallback stance forbids.
* AI-04 — the daily LLM ceiling could not bind for the 12 of 16 providers that
  have no pricing row: they accrued $0.00 forever.
* AI-05 — per-server MCP toolset overrides never matched the registered names.

Review round 2 (same audit) fixed four regressions the first cut introduced, and
each has a test below that FAILS against that first cut:

* AI-02 mapped only 3 authoring task types, so ``phantom_repair`` (the repair
  loop's whole purpose), ``strategy_development`` and the free-form aliases the
  Brain actually emits silently lost codegen — see the parity tests.
* AI-04 charged every unpriced pair the priciest rate in the table, so a local
  model or an OpenRouter ``:free`` route could trip the cap on $0.00 of real
  spend.
* AI-05 resolved the MCP server by prefix-matching the tool NAME, which is
  fail-open across servers (``mcp:jira`` matched ``jira_prod``'s tools).
* AI-01 left the agent no signal about what ``run_code`` is still for.
"""
from __future__ import annotations

import contextlib
import sys

import pytest

from forven import billing_guard, cost_pricing, sandbox
from forven.agents import runner
from forven.agents import tool_registry as tr
from forven.agents.mcp_client import _mcp_tool_name
from forven.agents.tool_registry import (
    _CONTEXT_DEFAULT_DENY,
    VALID_CONTEXTS,
    ToolDef,
    compute_effective_toolset,
    filter_tools_for_context,
)


# --------------------------------------------------------------------------- #
# AI-01 — run_code static guard
# --------------------------------------------------------------------------- #
def _sandbox_calls(monkeypatch) -> list[str]:
    """Record every code string that reaches the real sandbox."""
    seen: list[str] = []

    def _fake_run_code(code: str, *_a, **_k) -> dict:
        seen.append(code)
        return {"stdout": "ran", "stderr": "", "returncode": 0, "timed_out": False}

    monkeypatch.setattr(sandbox, "run_code", _fake_run_code)
    return seen


def test_run_code_scans_non_strategy_code(monkeypatch) -> None:
    """The AST guard must run even when the source has no strategy markers.

    Before the fix the guard was gated on ``"BaseStrategy" in code or
    "generate_signal" in code``, so this payload — which contains neither —
    reached the interpreter completely unvalidated.
    """
    from forven.agents.tools_backtesting import _tool_run_code

    seen = _sandbox_calls(monkeypatch)
    payload = "import os\nprint(os.environ)\n"
    assert "BaseStrategy" not in payload and "generate_signal" not in payload

    out = _tool_run_code(payload)

    assert "AST guard blocked execution" in out
    assert seen == [], "blocked code must never reach the sandbox"


def test_run_code_blocks_dynamic_exec_without_strategy_markers(monkeypatch) -> None:
    from forven.agents.tools_backtesting import _tool_run_code

    seen = _sandbox_calls(monkeypatch)
    out = _tool_run_code("data = 'cHJpbnQoMSk='\neval('1+1')\n")
    assert "AST guard blocked execution" in out
    assert seen == []


def test_run_code_still_runs_clean_analysis_code(monkeypatch) -> None:
    """The guard must not break the tool's legitimate use (allowlisted libs)."""
    from forven.agents.tools_backtesting import _tool_run_code

    seen = _sandbox_calls(monkeypatch)
    out = _tool_run_code("import pandas as pd\nprint(pd.Series([1, 2]).sum())\n")
    assert "AST guard blocked" not in out
    assert len(seen) == 1


def test_run_code_refuses_system_inspection_and_says_so(monkeypatch) -> None:
    """Recorded decision (tools_backtesting.py, ``_RUN_CODE_SCOPE_HINT``).

    The strategy-module allowlist is applied to run_code deliberately: the
    widening a diagnosis workflow would need is ``forven.db``/``sqlite3``, i.e. a
    WRITE path to strategies and trades from LLM-authored source. So run_code
    stays a numeric scratchpad — and the refusal must TELL the agent that, or it
    burns rounds retrying variations of the same blocked import.
    """
    from forven.agents.tools_backtesting import _RUN_CODE_SCOPE_HINT, _tool_run_code

    seen = _sandbox_calls(monkeypatch)
    for payload in (
        "from forven.db import get_db\nprint(get_db)\n",
        "import sqlite3\nprint(sqlite3)\n",
        "import pathlib\nprint(pathlib.Path('.').read_text)\n",
    ):
        out = _tool_run_code(payload)
        assert "AST guard blocked execution" in out
        assert _RUN_CODE_SCOPE_HINT in out
        assert "read_file" in out  # points at the tool that CAN inspect
    assert seen == []


# --------------------------------------------------------------------------- #
# AI-01 (second half) — sandbox cwd + network deny
# --------------------------------------------------------------------------- #
class _FakeProc:
    pid = 4321
    returncode = 0

    def communicate(self, timeout=None):
        return ("", "")

    def kill(self):  # pragma: no cover - timeout path not exercised here
        return None


def test_run_code_child_cwd_is_not_the_repo_root(monkeypatch) -> None:
    """AI-01: a relative open() must not resolve into the live checkout."""
    captured: dict[str, object] = {}

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(sandbox, "IS_WINDOWS", True)
    monkeypatch.setattr(sandbox, "_create_windows_job_object", lambda _mb: (None, None))
    monkeypatch.setattr(sandbox, "_close_job", lambda *_a, **_k: None)
    monkeypatch.setattr(sandbox.subprocess, "Popen", _fake_popen)

    sandbox.run_code("print('x')")

    cwd = str(captured["cwd"])
    assert cwd != str(sandbox.REPO_ROOT)
    assert "forven_sandbox_" in cwd
    # The child is launched through the network-deny bootstrap, which is handed
    # the submitted script as argv[1].
    cmd = list(captured["cmd"])
    assert cmd[0] == sandbox.PYTHON_EXE
    assert cmd[1].endswith(sandbox._SANDBOX_RUNNER_NAME)
    assert cmd[2].endswith(sandbox._SANDBOX_SCRIPT_NAME)


def test_sandbox_workdir_holds_runner_and_script() -> None:
    tmpdir, runner_path, script_path = sandbox._prepare_sandbox_workdir("print('hi')\n")
    try:
        from pathlib import Path

        assert Path(script_path).read_text(encoding="utf-8") == "print('hi')\n"
        bootstrap = Path(runner_path).read_text(encoding="utf-8")
        assert "_install_network_deny" in bootstrap
        assert "network access is disabled" in bootstrap
        assert Path(script_path).parent == Path(tmpdir.name)
    finally:
        tmpdir.cleanup()


@pytest.mark.skipif(sys.platform != "win32", reason="spawns a real interpreter; CI host is Windows")
def test_run_code_denies_outbound_sockets_for_real() -> None:
    """End-to-end: the promised 'no network access' is now enforced, not claimed.

    Loopback is used deliberately — the deny covers it too, and a bypass that
    only blocked the public internet would still reach the local control plane.
    """
    result = sandbox.run_code(
        "import socket\n"
        "socket.create_connection(('127.0.0.1', 8003), timeout=1)\n"
        "print('CONNECTED')\n"
    )
    assert "CONNECTED" not in result["stdout"]
    assert "network access is disabled" in result["stderr"]
    assert result["returncode"] != 0


@pytest.mark.skipif(sys.platform != "win32", reason="spawns a real interpreter; CI host is Windows")
def test_run_code_reports_user_line_numbers_through_the_bootstrap() -> None:
    """The bootstrap must not push the agent's traceback out of alignment."""
    result = sandbox.run_code("a = 1\nb = 2\nraise RuntimeError('boom')\n")
    assert "RuntimeError: boom" in result["stderr"]
    assert "line 3" in result["stderr"]
    assert "runpy" not in result["stderr"]


# --------------------------------------------------------------------------- #
# AI-02 — task-type → tools_context mapping
# --------------------------------------------------------------------------- #
def test_tools_context_never_none_for_any_task_type() -> None:
    """A None context makes the registry skip context resolution entirely."""
    for task_type in (
        "backtest", "post_mortem", "risk_audit", "phantom_repair",
        "some_task_type_added_next_year", "", None,
    ):
        ctx = runner._tools_context_for_task_type(task_type)
        assert ctx is not None
        assert ctx in VALID_CONTEXTS


def test_unlisted_task_types_get_the_most_restrictive_context() -> None:
    assert runner._DEFAULT_TOOLS_CONTEXT == "scheduled"
    # 'scheduled' really is the strictest of the deny sets we ship.
    scheduled_deny = _CONTEXT_DEFAULT_DENY["scheduled"]
    assert {"research", "codegen", "catastrophic"} <= set(scheduled_deny)
    for task_type in ("post_mortem", "risk_audit", "backtest", "brand_new_type"):
        assert runner._tools_context_for_task_type(task_type) == "scheduled"


def test_generate_strategies_is_a_develop_context() -> None:
    """It authors strategy code, so it needs codegen — like develop_candidate."""
    for task_type in ("develop_candidate", "code_strategy", "generate_strategies"):
        assert runner._tools_context_for_task_type(task_type) == "develop"


def test_research_and_operator_triggered_mappings() -> None:
    assert runner._tools_context_for_task_type("research") == "research"
    for task_type in ("manual", "approval_troubleshoot", "notification_repair"):
        # Operator-triggered triage keeps run_code (bot.py's documented
        # full-stack-engineer diagnosis workflow) but is now override-able.
        assert runner._tools_context_for_task_type(task_type) == "interactive"


def _codegen_is_allowed(context: str) -> bool:
    return "codegen" not in _CONTEXT_DEFAULT_DENY.get(context, frozenset())


def test_every_brain_strategy_creation_type_keeps_codegen() -> None:
    """Parity with the codebase's OWN canonical list — the anti-drift guard.

    ``brain._STRATEGY_CREATION_TASK_TYPES`` is the list the rest of the app
    treats as "this task authors a strategy". The first cut of AI-02 covered 4
    of its 5 members and dropped ``strategy_development`` on the floor, where it
    inherited 'scheduled' and lost register_strategy + run_code silently.
    """
    from forven.brain import _STRATEGY_CREATION_TASK_TYPES

    for task_type in _STRATEGY_CREATION_TASK_TYPES:
        ctx = runner._tools_context_for_task_type(task_type)
        assert ctx in VALID_CONTEXTS
        if task_type == "research":
            assert ctx == "research"  # ingest-only: codegen stays denied
            continue
        assert _codegen_is_allowed(ctx), f"{task_type!r} resolved to {ctx!r}, which denies codegen"


def test_phantom_repair_can_still_edit_strategy_code() -> None:
    """phantom_recovery.py dispatches this with "You may edit strategy code".

    It is also in ownership.py's "strategy-developer codes containers" set, along
    with code_strategy_container / coding_cycle. All four must keep codegen.
    """
    for task_type in (
        "phantom_repair",
        "code_strategy",
        "code_strategy_container",
        "coding_cycle",
    ):
        ctx = runner._tools_context_for_task_type(task_type)
        assert ctx == "develop", f"{task_type!r} resolved to {ctx!r}"


def test_free_form_authoring_aliases_the_brain_actually_emits() -> None:
    """``assign_agent_task`` takes task_type as a free-form string from the LLM.

    These four spellings are all present in the live agent_tasks table.
    """
    for task_type in ("development", "develop", "strategy", "strategy_development"):
        assert runner._tools_context_for_task_type(task_type) == "develop"


def test_analysis_keeps_the_idea_funnel_but_still_loses_codegen() -> None:
    """'analysis' is the highest-volume brain-dispatched type.

    Routing it to 'scheduled' would strip create_hypothesis (category 'research')
    and every discover_/inspect_ tool. 'research' keeps those while STILL denying
    codegen — strictly tighter than the pre-AI-02 behaviour, which gated nothing.
    """
    ctx = runner._tools_context_for_task_type("analysis")
    assert ctx == "research"
    assert not _codegen_is_allowed(ctx)
    assert "research" not in _CONTEXT_DEFAULT_DENY[ctx]
    assert "catastrophic" in _CONTEXT_DEFAULT_DENY[ctx]


def test_task_type_matching_is_case_and_whitespace_insensitive() -> None:
    assert runner._tools_context_for_task_type("  Phantom_Repair ") == "develop"
    assert runner._tools_context_for_task_type("RESEARCH") == "research"


@pytest.fixture
def codegen_registry(monkeypatch):
    async def _noop(_p):  # pragma: no cover - never invoked
        return ""

    registry = {
        name: ToolDef(
            name=name,
            description=f"test tool {name}",
            input_schema={"type": "object", "properties": {}},
            handler=_noop,
            permissions=frozenset({"*"}),
            category=category,
        )
        for name, category in (
            ("run_code", "codegen"),
            ("register_strategy", "codegen"),
            ("get_status", "general"),
        )
    }
    monkeypatch.setattr(tr, "_REGISTRY", registry)
    return registry


def test_context_deny_actually_binds_for_an_unlisted_task_type(codegen_registry) -> None:
    """The point of the fix: the deny set must now REACH ordinary task types."""
    tools = [
        {"name": n, "description": "d", "input_schema": {}}
        for n in ("run_code", "register_strategy", "get_status")
    ]
    ctx = runner._tools_context_for_task_type("post_mortem")
    names = {t["name"] for t in filter_tools_for_context(tools, None, ctx)}
    assert "run_code" not in names          # codegen denied in 'scheduled'
    assert "register_strategy" not in names
    assert "get_status" in names


def test_generate_strategies_context_keeps_codegen(codegen_registry) -> None:
    tools = [
        {"name": n, "description": "d", "input_schema": {}}
        for n in ("run_code", "register_strategy", "get_status")
    ]
    ctx = runner._tools_context_for_task_type("generate_strategies")
    names = {t["name"] for t in filter_tools_for_context(tools, None, ctx)}
    assert names == {"run_code", "register_strategy", "get_status"}


# --------------------------------------------------------------------------- #
# AI-05 — MCP per-server toolset overrides
# --------------------------------------------------------------------------- #
def _mcp_tool(server: str, tool: str, *, stamped: bool = True) -> ToolDef:
    """Build a ToolDef exactly as ``mcp_client.register_server_tools`` would.

    That registrar sets BOTH the ``mcp_<server>_<tool>`` name and
    ``permissions={f"mcp:{server}"}``; the permission entry is the authoritative
    record of which server a tool came from. ``stamped=False`` simulates a
    hand-built ToolDef that lacks it, to pin the fail-closed fallback.
    """
    async def _noop(_p):  # pragma: no cover - never invoked
        return ""

    name = _mcp_tool_name(server, tool)
    return ToolDef(
        name=name,
        description=f"mcp tool {name}",
        input_schema={"type": "object", "properties": {}},
        handler=_noop,
        permissions=frozenset({f"mcp:{server}"} if stamped else {"*"}),
        category="mcp",
    )


def test_per_server_override_matches_the_registered_name() -> None:
    """The registrar's ``mcp_<server>_<tool>`` must resolve an ``mcp:<server>`` rule.

    Before the fix the resolver split the name on ``__`` — a separator
    ``_mcp_tool_name`` never emits — so the operator's disable was inert.
    """
    jira = _mcp_tool("jira", "create_issue")
    slack = _mcp_tool("slack", "post_msg")
    overrides = {"mcp:jira": False}

    assert tr._resolve_tool_enabled(jira, overrides, "interactive") is False
    assert tr._resolve_tool_enabled(slack, overrides, "interactive") is True


def test_per_server_override_handles_underscored_server_names() -> None:
    """Server names contain underscores — each rule must hit only its own server."""
    prod = _mcp_tool("jira_prod", "create_issue")
    plain = _mcp_tool("jira", "create_issue")
    overrides = {"mcp:jira": True, "mcp:jira_prod": False}

    assert tr._resolve_tool_enabled(prod, overrides, "interactive") is False
    assert tr._resolve_tool_enabled(plain, overrides, "interactive") is True


def test_one_server_rule_cannot_re_enable_another_servers_tools() -> None:
    """THE fail-open case: re-enabling ``jira`` on top of a wildcard deny.

    ``mcp_jira_prod_create_issue`` is server ``jira_prod``, tool ``create_issue``
    — but it also starts with ``mcp_jira_``. The name-prefix resolver therefore
    matched the ``mcp:jira`` rule and turned a DIFFERENT server's entire tool
    surface back on, in the resolver whose whole purpose is per-server control.
    The server is read off the ToolDef's ``mcp:<server>`` permission now, so the
    ambiguity is gone: only the wildcard applies, and it denies.
    """
    prod = _mcp_tool("jira_prod", "create_issue")
    overrides = {"mcp:*": False, "mcp:jira": True}

    assert tr._mcp_server_of(prod) == "jira_prod"
    assert tr._mcp_server_override_key(prod, overrides) is None
    assert tr._resolve_tool_enabled(prod, overrides, "interactive") is False


def test_unstamped_mcp_tooldef_fails_closed_on_an_ambiguous_prefix() -> None:
    """Nothing in the app registers MCP tools without the stamp, but if one
    appeared, an ambiguous name must never GRANT access — only deny."""
    prod = _mcp_tool("jira_prod", "create_issue", stamped=False)
    assert tr._mcp_server_of(prod) is None

    # An enable rule for a different server is ignored → wildcard deny stands.
    assert tr._resolve_tool_enabled(prod, {"mcp:*": False, "mcp:jira": True}, "interactive") is False
    # A deny rule still binds (operator intent in the safe direction).
    assert tr._resolve_tool_enabled(prod, {"mcp:jira": False}, "interactive") is False


def test_specific_server_override_beats_wildcard() -> None:
    jira = _mcp_tool("jira", "create_issue")
    slack = _mcp_tool("slack", "post_msg")
    overrides = {"mcp:*": False, "mcp:jira": True}

    assert tr._resolve_tool_enabled(jira, overrides, "interactive") is True
    assert tr._resolve_tool_enabled(slack, overrides, "interactive") is False


def test_effective_toolset_preview_reports_the_server_rule(monkeypatch) -> None:
    """The matrix preview must agree with the runtime filter (same helper)."""
    tool = _mcp_tool("jira", "create_issue")
    monkeypatch.setattr(tr, "_REGISTRY", {tool.name: tool})
    # The agent must hold the server grant, or the base permission check drops
    # the tool before any override is consulted (agent_mcp_grants).
    monkeypatch.setattr(tr, "_permission_subjects", lambda _a: frozenset({"mcp:jira"}))
    monkeypatch.setattr(tr, "_load_toolset_overrides", lambda _a, _c: {"mcp:jira": False})

    row = next(r for r in compute_effective_toolset("agent-x", "interactive"))
    assert row["name"] == tool.name
    assert row["enabled"] is False
    assert row["source"] == "override:mcp:jira"


def test_effective_toolset_preview_agrees_on_the_cross_server_case(monkeypatch) -> None:
    """Preview and runtime must not disagree about the jira/jira_prod case."""
    tool = _mcp_tool("jira_prod", "create_issue")
    overrides = {"mcp:*": False, "mcp:jira": True}
    monkeypatch.setattr(tr, "_REGISTRY", {tool.name: tool})
    monkeypatch.setattr(tr, "_permission_subjects", lambda _a: frozenset({"mcp:jira_prod"}))
    monkeypatch.setattr(tr, "_load_toolset_overrides", lambda _a, _c: dict(overrides))

    row = next(r for r in compute_effective_toolset("agent-x", "interactive"))
    assert row["enabled"] is False
    assert row["source"] == "override:mcp:*"
    assert row["enabled"] == tr._resolve_tool_enabled(tool, overrides, "interactive")


def test_unregister_prefix_matches_the_registrar() -> None:
    """The namespace join and the unregister prefix must stay in step."""
    assert _mcp_tool_name("jira", "create_issue").startswith("mcp_jira_")


def test_unregister_does_not_deregister_a_neighbouring_server(monkeypatch) -> None:
    """Disabling ``jira`` must not silently strip ``jira_prod``'s whole toolset.

    Same prefix ambiguity as the override resolver, in the other direction. The
    unstamped entry is still removed by prefix — leaving a disabled server's
    tools callable is the direction that actually matters.
    """
    from forven.agents import mcp_client

    jira = _mcp_tool("jira", "create_issue")
    prod = _mcp_tool("jira_prod", "create_issue")
    legacy = _mcp_tool("jira", "legacy_tool", stamped=False)
    registry = {t.name: t for t in (jira, prod, legacy)}
    monkeypatch.setattr(tr, "_REGISTRY", registry)

    assert mcp_client.unregister_server_tools("jira") == 2
    assert prod.name in registry
    assert jira.name not in registry
    assert legacy.name not in registry


# --------------------------------------------------------------------------- #
# AI-03 — no auto-fallback onto un-configured providers
# --------------------------------------------------------------------------- #
def test_credentialed_chain_never_appends_an_unconfigured_provider(monkeypatch) -> None:
    """A 429 must not reroute a portfolio-bearing prompt to an unchosen vendor."""
    from forven import ai

    monkeypatch.setattr(ai, "_provider_has_credentials", lambda p: p == "anthropic")
    monkeypatch.setattr(
        ai, "get_model_routing", lambda: {"provider_priority": ["anthropic", "openai"]}
    )

    chain = ai._credentialed_chain([("openai", "gpt-5.2")], ("openai", "gpt-5.2"))

    assert [entry[0] for entry in chain] == ["openai"]
    assert all(entry[0] != "anthropic" for entry in chain)


def test_credentialed_chain_honours_the_configured_chain_verbatim(monkeypatch) -> None:
    from forven import ai

    monkeypatch.setattr(ai, "_provider_has_credentials", lambda p: p in {"openai", "minimax"})
    configured = [("openai", "gpt-5.2"), ("minimax", "MiniMax-M2.5")]

    chain = ai._credentialed_chain(list(configured), configured[0])

    assert chain == configured  # operator-configured fallback still executes


def test_credentialed_chain_drops_uncredentialed_entries(monkeypatch) -> None:
    from forven import ai

    monkeypatch.setattr(ai, "_provider_has_credentials", lambda p: p == "minimax")
    chain = ai._credentialed_chain(
        [("openai", "gpt-5.2"), ("minimax", "MiniMax-M2.5")], ("openai", "gpt-5.2")
    )
    assert chain == [("minimax", "MiniMax-M2.5")]


def test_credentialed_chain_degrades_to_the_requested_entry(monkeypatch) -> None:
    """Nothing credentialed → one attempt so the error names what was asked for."""
    from forven import ai

    monkeypatch.setattr(ai, "_provider_has_credentials", lambda _p: False)
    chain = ai._credentialed_chain([("openai", "gpt-5.2")], ("openai", "gpt-5.2"))
    assert chain == [("openai", "gpt-5.2")]


# --------------------------------------------------------------------------- #
# AI-04 — the daily spend ceiling must bind for unpriced providers
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, spent, usage_rows):
        self.spent = spent
        self.usage_rows = usage_rows

    def execute(self, sql, params=()):
        if "SUM(cost_usd)" in sql:
            return _FakeCursor([{"spent": self.spent}])
        return _FakeCursor(self.usage_rows)


def _wire_billing(monkeypatch, *, cap, spent, usage_rows):
    @contextlib.contextmanager
    def _fake_db(*_a, **_k):
        yield _FakeConn(spent, usage_rows)

    monkeypatch.setattr(billing_guard, "get_db", _fake_db)
    monkeypatch.setattr(
        billing_guard, "kv_get", lambda _k, _d=None: {"agent_daily_cost_cap_usd": cap}
    )


def _usage(provider, model_id, in_tokens, out_tokens):
    return {
        "provider": provider,
        "model_id": model_id,
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
    }


def test_cap_binds_for_a_provider_with_no_pricing_row(monkeypatch) -> None:
    """A provider with no rows at all still has to accrue against the cap.

    ``nvidia`` is routable (model_routing._SUPPORTED_PROVIDERS) and carries no
    published per-token list price we can hard-code, so it exercises the flat
    residual rate. Before AI-04 it reported "within cap: $0.00/$25.00" forever.
    """
    assert not cost_pricing.has_pricing("nvidia", "meta/llama-3.3-70b-instruct")
    assert cost_pricing.fallback_rate("nvidia", "meta/llama-3.3-70b-instruct") is None

    _wire_billing(
        monkeypatch,
        cap=25.0,
        spent=0.0,
        usage_rows=[_usage("nvidia", "meta/llama-3.3-70b-instruct", 2_000_000, 1_000_000)],
    )

    expected = (
        2 * billing_guard._UNPRICED_IN_PER_MILLION_USD
        + 1 * billing_guard._UNPRICED_OUT_PER_MILLION_USD
    )
    assert billing_guard.get_unpriced_spend_today() == pytest.approx(expected)
    allowed, reason = billing_guard.check_daily_cost_cap()
    assert allowed is False
    assert "cap reached" in reason
    assert "unpriced models" in reason


def test_anthropic_is_now_priced_for_real(monkeypatch) -> None:
    """The ROOT fix: real rows mean real cost_usd, not a blanket over-estimate.

    ``claude-sonnet-4-6`` is the shipped anthropic default in model_routing.
    """
    assert cost_pricing.has_pricing("anthropic", "claude-sonnet-4-6")
    assert cost_pricing.estimate_cost_usd(
        "anthropic", "claude-sonnet-4-6",
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
    ) == pytest.approx(3.0 + 15.0)

    _wire_billing(
        monkeypatch,
        cap=25.0,
        spent=18.0,
        usage_rows=[_usage("anthropic", "claude-sonnet-4-6", 1_000_000, 1_000_000)],
    )
    assert billing_guard.get_unpriced_spend_today() == 0.0


def test_unknown_model_on_a_known_provider_uses_that_providers_ceiling(monkeypatch) -> None:
    """A model id we don't recognise is charged its OWN provider's top rate.

    The first cut charged $15/$60 (openai o1) for every unpriced pair, so an
    unrecognised Gemini Flash id accrued ~150x its real cost.
    """
    assert not cost_pricing.has_pricing("gemini", "gemini-9-flash-unreleased")
    assert cost_pricing.fallback_rate("gemini", "gemini-9-flash-unreleased") == (2.00, 12.00)

    _wire_billing(
        monkeypatch,
        cap=25.0,
        spent=0.0,
        usage_rows=[_usage("gemini", "gemini-9-flash-unreleased", 1_000_000, 1_000_000)],
    )
    assert billing_guard.get_unpriced_spend_today() == pytest.approx(2.00 + 12.00)


def test_priced_provider_is_not_double_counted(monkeypatch) -> None:
    _wire_billing(
        monkeypatch,
        cap=25.0,
        spent=1.25,
        usage_rows=[_usage("openai", "gpt-4o", 100_000, 10_000)],
    )
    assert billing_guard.get_unpriced_spend_today() == 0.0
    allowed, reason = billing_guard.check_daily_cost_cap()
    assert allowed is True
    assert reason == "within cap: $1.25/$25.00"


@pytest.mark.parametrize(
    "provider,model_id",
    [
        ("lmstudio", "local-model"),
        # LM Studio model ids are whatever the operator typed (ai.py
        # _normalize_lmstudio_model passes them through), so the exact-pair
        # exemption only ever fired on a default config.
        ("lmstudio", "qwen/qwen3-30b-a3b"),
        ("lmstudio", "some-gguf-i-downloaded"),
        ("ollama", "llama3"),
        # The Conserve throughput preset deliberately routes to these.
        ("openrouter", "z-ai/glm-4.6:free"),
        ("openrouter", "google/gemma-3-27b-it:free"),
    ],
)
def test_genuinely_free_routes_are_never_charged(monkeypatch, provider, model_id) -> None:
    """Charging a free route can pause the whole agent loop on $0.00 of spend.

    Every pair here returned False from the first cut's ``_pair_is_priced`` and
    was billed at $15/$60 per 1M: 1M+1M tokens = $75 against a $25 cap.
    """
    assert cost_pricing.has_pricing(provider, model_id)
    assert cost_pricing.resolve_rate(provider, model_id) == (0.0, 0.0)

    _wire_billing(
        monkeypatch,
        cap=25.0,
        spent=0.0,
        usage_rows=[_usage(provider, model_id, 5_000_000, 5_000_000)],
    )
    assert billing_guard.get_unpriced_spend_today() == 0.0
    allowed, reason = billing_guard.check_daily_cost_cap()
    assert allowed is True
    assert reason == "within cap: $0.00/$25.00"


def test_openrouter_paid_variant_still_prices_off_the_base_model() -> None:
    """``:nitro``/``:floor`` are routing preferences over the SAME model."""
    assert cost_pricing.resolve_rate("openrouter", "openai/gpt-4o:nitro") == (2.50, 10.00)
    # Vendor prefixes differ from our provider keys — map them, don't guess.
    assert cost_pricing.resolve_rate("openrouter", "google/gemini-2.5-flash") == (0.30, 2.50)
    assert cost_pricing.resolve_rate("openrouter", "x-ai/grok-3-mini") == (0.30, 0.50)
    assert cost_pricing.resolve_rate("openrouter", "anthropic/claude-sonnet-4-5") == (3.00, 15.00)


def test_unknown_pair_fails_closed_when_the_pricing_probe_breaks(monkeypatch) -> None:
    """If cost_pricing internals move, unpriced-by-default keeps the cap alive."""
    monkeypatch.setitem(sys.modules, "forven.cost_pricing", object())
    _wire_billing(
        monkeypatch,
        cap=25.0,
        spent=0.0,
        usage_rows=[_usage("openai", "gpt-4o", 1_000_000, 1_000_000)],
    )
    assert billing_guard.get_unpriced_spend_today() == pytest.approx(
        billing_guard._UNPRICED_IN_PER_MILLION_USD
        + billing_guard._UNPRICED_OUT_PER_MILLION_USD
    )


def test_disabled_cap_short_circuits_before_any_estimate(monkeypatch) -> None:
    """The default-off product decision (billing_guard.py:36-40) is preserved."""
    _wire_billing(
        monkeypatch,
        cap=0.0,
        spent=0.0,
        usage_rows=[_usage("anthropic", "claude-opus-4", 9_000_000, 9_000_000)],
    )
    allowed, reason = billing_guard.check_daily_cost_cap()
    assert allowed is True
    assert reason == "no cap configured"
