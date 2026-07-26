"""ARCH-06 step 4: pins the backtest_api extraction AND the seams it rides on.

``forven.backtest_api`` was carved out of ``forven.api_core`` (10,606 lines).
Two things have to stay true for that to be safe, and neither is visible in a
normal green test run:

1.  **The shim is complete.** Every name defined in ``forven.backtest_api`` is
    still reachable — and is the SAME OBJECT — as ``forven.api_core.<name>``.
    ~10 test modules plus forven/strategies/backtest.py, forven/robustness/
    engine.py, forven/evolution.py, forven/agents/tools_backtesting.py and
    forven/api_domains/{analytics,jobs}.py import these from api_core by name.

2.  **The monkeypatch seams are LIVE.** This region is where the suite's
    patching is densest and every patch targets ``forven.api_core.<name>``. If
    moved code calls such a name through its own module globals, the patch
    still rebinds the api_core attribute but stops affecting the code under
    test — the test passes while testing nothing. That is a silent failure with
    no symptom, which is exactly why step 4 was deferred once.

    ``backtest_api`` therefore reaches every patched dependency through its
    ``core`` proxy. ``test_no_patched_api_core_name_is_called_by_its_bare_name``
    below re-derives the patched-name set FROM THE TEST SUITE ITSELF on every
    run, so the day someone adds ``monkeypatch.setattr(api_core, "_foo", ...)``
    for a name backtest_api calls directly, this fails instead of quietly
    neutering their test.

The per-seam tests are not redundant with the AST guard: the guard proves the
call sites are shaped right, these prove the indirection actually resolves.

Section 4 covers the OTHER half of this work item: ``_apply_settings_section``.
It was NOT restructured into a declarative field table — see the comment above
``test_the_api_02_schema_matches_what_the_chain_actually_handles`` for the
evidence. What is pinned instead is the property that item was really after:
the API-02 known-key schema and the if/elif chain must not drift apart, in
either direction.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import sqlite3

import pytest

import forven.api_core as core
import forven.backtest_api as bt


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(REPO_ROOT, "tests")

# Names backtest_api defines for its own plumbing — not part of the moved API.
_MODULE_PRIVATE = {"_ApiCoreProxy", "_API_CORE", "core", "log"}


def _module_level_names(module) -> set[str]:
    """Top-level names a module DEFINES (not the ones it imports)."""
    source = io.open(module.__file__, encoding="utf-8-sig").read()
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _moved_names() -> set[str]:
    return _module_level_names(bt) - _MODULE_PRIVATE


# ---------------------------------------------------------------------------
# 1. The re-export shim
# ---------------------------------------------------------------------------

def test_every_moved_name_is_still_reachable_from_api_core():
    missing = sorted(name for name in _moved_names() if not hasattr(core, name))
    assert not missing, (
        "ARCH-06 step 4 moved these out of forven.api_core without re-exporting "
        "them. Every consumer still imports them from api_core, so the shim is "
        "what makes the split safe — add them to the "
        "`from forven.backtest_api import (…)` block:\n  " + "\n  ".join(missing)
    )


def test_the_shim_re_exports_the_same_object_not_a_copy():
    """Identity matters: `_BACKTEST_DISPLAY_EQUITY` and the functions are shared."""
    split = sorted(
        name for name in _moved_names()
        if getattr(core, name, None) is not getattr(bt, name)
    )
    assert not split, f"api_core and backtest_api hold different objects for: {split}"


def test_moved_names_are_not_redefined_back_inside_api_core():
    """A later edit that re-adds a def here would shadow the shim silently."""
    redefined = sorted(_moved_names() & _module_level_names(core))
    assert not redefined, (
        "these are defined in BOTH forven/api_core.py and forven/backtest_api.py; "
        f"api_core's definition wins and the extraction is undone for: {redefined}"
    )


# ---------------------------------------------------------------------------
# 2. The monkeypatch seams
# ---------------------------------------------------------------------------

_PATCH_PATTERNS = (
    re.compile(r'setattr\(\s*(?:[A-Za-z_][A-Za-z_0-9]*)\s*,\s*"([A-Za-z_][A-Za-z_0-9]*)"'),
    re.compile(r'"forven\.api_core\.([A-Za-z_][A-Za-z_0-9]*)"'),
)


def _api_core_names_the_suite_patches() -> set[str]:
    """Every api_core attribute any test in tests/ rebinds.

    Deliberately over-broad on the first pattern (it does not check which module
    object the setattr targets) — intersecting with api_core's own attributes
    below filters it, and erring toward MORE names only makes the guard stricter.
    """
    found: set[str] = set()
    for entry in sorted(os.listdir(TESTS_DIR)):
        if not entry.endswith(".py"):
            continue
        source = io.open(os.path.join(TESTS_DIR, entry), encoding="utf-8-sig", errors="replace").read()
        if "api_core" not in source:
            continue
        for pattern in _PATCH_PATTERNS:
            found.update(pattern.findall(source))
    return {name for name in found if hasattr(core, name)}


def _bare_module_level_reads(module) -> dict[str, set[str]]:
    """name -> enclosing functions that read it as a BARE global (not core.X)."""
    source = io.open(module.__file__, encoding="utf-8-sig").read()
    tree = ast.parse(source)
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bound = {a.arg for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs}
        if node.args.vararg:
            bound.add(node.args.vararg.arg)
        if node.args.kwarg:
            bound.add(node.args.kwarg.arg)
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, (ast.Store, ast.Del)):
                bound.add(inner.id)
            elif isinstance(inner, (ast.Import, ast.ImportFrom)):
                bound.update((a.asname or a.name).split(".")[0] for a in inner.names)
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load) and inner.id not in bound:
                out.setdefault(inner.id, set()).add(node.name)
    return out


def test_no_patched_api_core_name_is_called_by_its_bare_name():
    """THE guard. See the module docstring — a violation here is a silent one.

    Re-derived from the suite on every run, so it also catches the reverse
    decay: a future test that starts patching a name backtest_api calls
    directly would otherwise pass while exercising the unpatched code.
    """
    patched = _api_core_names_the_suite_patches()
    reads = _bare_module_level_reads(bt)
    violations = {
        name: sorted(reads[name])
        for name in sorted(patched)
        if name in reads
    }
    assert not violations, (
        "forven/backtest_api.py calls these by their bare module-local name, but "
        "the test suite monkeypatches them on forven.api_core. The patch would "
        "rebind api_core's attribute and this code would keep using its own — "
        "the tests would pass while testing nothing. Route each through the "
        "`core` proxy (`core.<name>(...)`):\n  "
        + "\n  ".join(f"{name}  (called from: {', '.join(fns)})" for name, fns in violations.items())
    )


def test_the_core_proxy_resolves_live_not_at_import_time():
    sentinel = object()
    original = core._result_data_dirs
    try:
        core._result_data_dirs = sentinel
        assert bt.core._result_data_dirs is sentinel
    finally:
        core._result_data_dirs = original
    assert bt.core._result_data_dirs is original


def test_patching_result_data_dirs_on_api_core_redirects_artifact_reads(tmp_path, monkeypatch):
    """tests/test_backtest_chart_context.py patches exactly this."""
    monkeypatch.setattr(core, "_result_data_dirs", lambda: [str(tmp_path)])
    (tmp_path / "seam-rid_equity.json").write_text(json.dumps([{"timestamp": "t", "equity": 1}]), encoding="utf-8")

    payload, path = bt._load_result_json_artifact("seam-rid", {}, "backtest", "equity")

    assert payload == [{"timestamp": "t", "equity": 1}]
    assert path is not None and str(tmp_path) in path


def test_patching_ensure_result_data_dir_on_api_core_redirects_artifact_writes(tmp_path, monkeypatch):
    """The write half of the same seam (_write_backtest_result_artifacts' too)."""
    monkeypatch.setattr(core, "_ensure_result_data_dir", lambda: str(tmp_path))

    bt._write_backtest_chart_artifacts("seam-rid", "seam-job", {"bars": [], "strategy_name": "S"})

    written = json.loads((tmp_path / "seam-rid_chart.json").read_text(encoding="utf-8"))
    assert written["result_id"] == "seam-rid"
    assert (tmp_path / "seam-job_chart.json").exists()


def test_patching_get_db_on_api_core_redirects_the_row_writers(monkeypatch):
    executed: list[tuple] = []

    class _FakeConn:
        def execute(self, sql, params=()):
            executed.append((sql, params))

    class _FakeDb:
        def __enter__(self):
            return _FakeConn()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(core, "get_db", lambda: _FakeDb())

    bt._update_optimization_result_row(result_id="seam-rid", metrics={"a": 1}, config={"b": 2})

    assert executed, "api_core.get_db was patched but backtest_api used a different binding"
    sql, params = executed[0]
    assert "UPDATE backtest_results" in sql
    assert params[-1] == "seam-rid"


def test_patching_now_on_api_core_redirects_the_trash_writer(monkeypatch):
    monkeypatch.setattr(core, "_now", lambda: "2099-01-01T00:00:00Z")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        bt._set_backtest_result_trash(conn, "seam-rid")
        row = conn.execute("SELECT deleted_at FROM backtest_result_trash WHERE result_id = ?", ("seam-rid",)).fetchone()
    finally:
        conn.close()

    assert row is not None and row["deleted_at"] == "2099-01-01T00:00:00Z"


def test_patching_coerce_optional_float_on_api_core_reaches_the_summary_coercer(monkeypatch):
    monkeypatch.setattr(core, "_coerce_optional_float", lambda value: 42.5)

    payload = bt._coerce_backtest_summary_payload({"id": "seam-rid", "monthly_return_pct": "ignored"})

    assert payload is not None
    assert payload["monthly_return_pct"] == 42.5
    assert payload["annualized_return_pct"] == 42.5


def test_patching_infer_strategy_type_from_name_on_api_core_reaches_the_summary_normalizer(monkeypatch):
    seen: list[str] = []

    def _fake(value):
        seen.append(str(value))
        return "ema_cross"

    monkeypatch.setattr(core, "_infer_strategy_type_from_name", _fake)

    summary = bt._normalize_backtest_summary({"id": "seam-rid", "metadata": {"strategy_name": "mystery"}})

    assert seen, "api_core._infer_strategy_type_from_name was patched but never consulted"
    assert summary["description"] == bt._describe_strategy("ema_cross", {})


# ---------------------------------------------------------------------------
# 3. Behaviour parity spot-checks on the moved code
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1,5", 15.0),
        ("52.1%", 52.1),
        ("-0.536 to -1.463", -0.9995),
    ],
)
def test_legacy_metadata_float_salvage_survived_the_move(raw, expected):
    assert bt._coerce_legacy_metadata_float(raw) == pytest.approx(expected)


def test_artifact_key_sanitizer_still_kills_traversal():
    """SECURITY (audit 2026-06-22, L4) — the comment on the function says why."""
    assert bt._safe_result_artifact_key("../../secrets") == "..-..-secrets"
    assert bt._safe_result_artifact_key("..\\..\\secrets") == "..-..-secrets"
    # the read side sanitizes too, so a traversal id can never reach os.path.join
    candidates = bt._result_artifact_candidate_ids("..\\..\\secrets", {}, "backtest")
    assert candidates == ["..-..-secrets"]
    assert not any(os.sep in c or "/" in c for c in candidates)


# ---------------------------------------------------------------------------
# 4. _apply_settings_section — the API-02 schema must track the if/elif chain
# ---------------------------------------------------------------------------
#
# Why the 876-line chain was NOT replaced by a declarative field table:
#
#   * The motivation given ("it silently DROPS an unknown key") is already
#     closed for the sections that carry risk. API-02 added
#     _validate_settings_section_payload, which 422s an unknown key for
#     initial-capital / trading-mode / risk BEFORE the first mutation. What is
#     missing there is a table, not a restructure — see the list below.
#   * The chain is not uniformly declarative, and the non-declarative parts are
#     the money-bearing ones: multi-alias payload keys (hyperliquid takes 3
#     names for the wallet, 3 for the private key); cross-field twin syncs whose
#     result depends on which OTHER keys are in the same payload (risk_pct <->
#     position_pct, daily_loss <-> daily_loss_pct against initial_capital);
#     writes that REFUSE and log instead of applying (the direction-book address
#     guards while book-routed positions are open); writes that land in a
#     different store (the private key and the Discord webhook go to the
#     encrypted secrets blob, the bot token into config.json); a coercion that
#     can raise and abort the whole write (trading-mode -> set_execution_mode,
#     MODE-SPLIT-1); an order-dependent post-pass (agents clears backup_ai_model
#     after both conditional branches); and three different default sources
#     (updates[k], updates.get(k, literal), and a nested re-coercion of the
#     stored value). research/data-engine do not take a key set at all — they
#     deep-merge an arbitrary nested blob.
#   * The suggested proof ("assert every key the old chain handled is still
#     handled") only pins the KEY SET. It cannot show that each key kept its
#     coercer, its default source, its guard, its ordering and its side effect —
#     which is exactly where a silently mis-applied risk limit would come from.
#
# So the chain stays. What is pinned here is the invariant the item was really
# chasing, and it is provable: the declared schema and the chain must agree.

_SECTIONS_WITHOUT_A_KEY_SCHEMA = frozenset({
    # Free-form nested blobs — they deep-merge whatever they are given, so a
    # flat key set is structurally inapplicable.
    "research", "data-engine", "data_engine",
    # Alias-based or secret-writing branches; a schema here needs the alias
    # spellings enumerated too, which is a separate, deliberate change.
    "hyperliquid", "agent-model-keys", "notifications",
    # Plain sections that simply have not been given a schema yet. Adding one
    # turns a silently-dropped key into a 422, which is a behaviour change for
    # every existing caller — do it with the callers in hand, not in passing.
    "exchange", "strategy", "agents", "bot-operations", "health-checks",
    "backtesting-defaults", "ui",
})


def _payload_keys_handled(branch_body) -> set[str]:
    """Payload keys a section branch reads. Mirrors the shapes the chain uses."""
    keys: set[str] = set()
    module = ast.Module(body=branch_body, type_ignores=[])
    for node in ast.walk(module):
        # `"k" in payload`
        if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.In):
            target = node.comparators[0]
            if isinstance(target, ast.Name) and target.id == "payload":
                if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                    keys.add(node.left.value)
        # `payload.get("k")`
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "payload" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    keys.add(first.value)
        # `for k in ("a", "b"):` and `for k, default in (("a", 1), ("b", 2)):`
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            if "'payload'" in ast.dump(ast.Module(body=node.body, type_ignores=[])):
                for element in node.iter.elts:
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        keys.add(element.value)
                    elif isinstance(element, (ast.Tuple, ast.List)) and element.elts:
                        head = element.elts[0]
                        if isinstance(head, ast.Constant) and isinstance(head.value, str):
                            keys.add(head.value)
    return keys


def _chain_sections() -> dict[str, set[str]]:
    """section name -> payload keys its branch handles, read off the AST."""
    source = io.open(core.__file__, encoding="utf-8-sig").read()
    fn = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_settings_section"
    )
    branch = next(
        node for node in fn.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.ops[0], ast.Eq)
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "exchange"
    )
    out: dict[str, set[str]] = {}
    while branch is not None:
        test = branch.test
        names: list[str] = []
        if isinstance(test, ast.Compare):
            target = test.comparators[0]
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                names = [target.value]
            elif isinstance(target, (ast.Set, ast.Tuple, ast.List)):
                names = [e.value for e in target.elts if isinstance(e, ast.Constant)]
        handled = _payload_keys_handled(branch.body)
        for name in names:
            out[name] = handled
        tail = branch.orelse
        branch = tail[0] if len(tail) == 1 and isinstance(tail[0], ast.If) else None
    return out


def test_the_section_chain_is_still_shaped_the_way_this_guard_reads_it():
    """Fail loudly if the AST derivation stopped seeing the chain, not silently."""
    sections = _chain_sections()
    assert "risk" in sections and "ui" in sections, (
        "the if/elif chain in _apply_settings_section no longer parses the way "
        "_chain_sections() reads it — the schema-drift guard below would pass "
        "vacuously. Teach the derivation the new shape."
    )
    assert len(sections["risk"]) > 50


def test_the_api_02_schema_matches_what_the_chain_actually_handles():
    """API-02's known-key schema must be exactly the branch's handled keys.

    Both directions are real bugs, and both are the "the setting does not
    stick" class this guard exists for:

      * a key in the CHAIN but not the SCHEMA is rejected with a 422 even
        though a handler for it exists;
      * a key in the SCHEMA but not the CHAIN passes validation, gets audited
        as accepted, and is then dropped on the floor — the operator is told
        the write landed and enforcement never changes.
    """
    from forven.settings_apply import _SETTINGS_SECTION_KNOWN_KEYS

    sections = _chain_sections()
    problems: list[str] = []
    for section, known in sorted(_SETTINGS_SECTION_KNOWN_KEYS.items()):
        handled = sections.get(section)
        assert handled is not None, f"schema declares a section the chain has no branch for: {section}"
        accepted_then_dropped = sorted(set(known) - handled)
        handled_but_refused = sorted(handled - set(known))
        if accepted_then_dropped:
            problems.append(f"{section}: validated but never applied -> {accepted_then_dropped}")
        if handled_but_refused:
            problems.append(f"{section}: applied by the chain but 422'd by the schema -> {handled_but_refused}")
    assert not problems, "\n".join(problems)


def test_every_settings_section_either_has_a_schema_or_is_a_declared_exception():
    """A NEW section must choose: schema it, or say out loud that it does not."""
    from forven.settings_apply import _SETTINGS_SECTION_KNOWN_KEYS

    sections = set(_chain_sections())
    unaccounted = sorted(sections - set(_SETTINGS_SECTION_KNOWN_KEYS) - _SECTIONS_WITHOUT_A_KEY_SCHEMA)
    assert not unaccounted, (
        "these settings sections silently drop unknown keys and are not on the "
        "declared-exception list. Give them an entry in "
        "_SETTINGS_SECTION_KNOWN_KEYS (so a typo'd key 422s instead of vanishing) "
        f"or add them to _SECTIONS_WITHOUT_A_KEY_SCHEMA with a reason: {unaccounted}"
    )
    stale = sorted(_SECTIONS_WITHOUT_A_KEY_SCHEMA & set(_SETTINGS_SECTION_KNOWN_KEYS))
    assert not stale, f"these now HAVE a schema — drop them from the exception list: {stale}"
    gone = sorted(_SECTIONS_WITHOUT_A_KEY_SCHEMA - sections)
    assert not gone, f"exception list names sections the chain no longer has: {gone}"
