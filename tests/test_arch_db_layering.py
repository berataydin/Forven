"""Architecture guards for the storage/control-plane boundary.

`forven/db.py` is the most depended-upon module in the system (fan-in ~154).
It used to run the approval WORKFLOW from inside the storage layer: a db
function computed the approval deadline, ran the smart-approval classifier and
invoked `post_approve_approval` — the HTTP-layer handler — via function-local
`from forven.control_plane...` imports. Those imports were function-local purely
so the package stayed importable; they were still real dependency edges, and
they are what put the storage layer inside the 157-module mutually-importing
cluster.

The workflow now lives in `forven.control_plane.approvals.create_approval`,
which writes through the `forven.db.insert_approval` storage primitive.
`forven.db.create_approval` survives only as a deprecated delegating shim.

These tests are cheap and structural on purpose: they fail loudly the moment
someone re-introduces workflow logic into storage.
"""

import ast
from pathlib import Path

import pytest

import forven.control_plane.approvals as cp_approvals
import forven.db as db_mod

DB_SOURCE_PATH = Path(db_mod.__file__)


def _db_tree() -> ast.Module:
    return ast.parse(DB_SOURCE_PATH.read_text(encoding="utf-8"))


def _forven_imports_in(node: ast.AST) -> set[str]:
    """Every `forven.*` module imported anywhere under ``node`` (any nesting)."""
    modules: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.ImportFrom) and (sub.module or "").startswith("forven"):
            modules.add(sub.module or "")
        elif isinstance(sub, ast.Import):
            for alias in sub.names:
                if alias.name.startswith("forven"):
                    modules.add(alias.name)
    return modules


def _function_def(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"forven/db.py no longer defines {name}()")


# --------------------------------------------------------------------------
# Layering: storage must not reach up into the approval workflow
# --------------------------------------------------------------------------

# The approval workflow modules. db.py must not depend on any of these except
# `control_plane.approvals`, and that single edge exists only inside the
# deprecated create_approval shim (see test_only_shim_may_import_control_plane).
FORBIDDEN_IN_DB = {
    "forven.control_plane.approval_modes",
    "forven.control_plane.smart_approval",
    "forven.control_plane.models",
    # The scanner strategy registry: pulled in by the dead strategy_candidates
    # CRUD block that was removed with this wave. Storage has no business
    # importing the scanner.
    "forven.scanner",
}


def test_db_does_not_import_approval_workflow_modules():
    imported = _forven_imports_in(_db_tree())
    leaked = sorted(FORBIDDEN_IN_DB & imported)
    assert not leaked, (
        "forven/db.py imports approval-workflow modules again: "
        f"{leaked}. Storage records approval decisions; it must not make them. "
        "Put the workflow in forven/control_plane/approvals.py, which is allowed "
        "to import forven.db."
    )


def test_only_shim_may_import_control_plane():
    """`control_plane.approvals` may appear in db.py in exactly one place."""
    tree = _db_tree()
    offenders = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and "forven.control_plane.approvals" in _forven_imports_in(node)
        and node.name != "create_approval"
    ]
    assert not offenders, (
        "these forven/db.py functions import forven.control_plane.approvals: "
        f"{offenders}. Only the deprecated create_approval shim is allowed to; "
        "everything else should call the control plane from above."
    )

    module_level = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("forven.control_plane")
    }
    assert not module_level, (
        f"forven/db.py imports {sorted(module_level)} at module level — that is a "
        "hard import cycle, not just a layering smell."
    )


def test_insert_approval_is_pure_storage():
    """The storage primitive must have no forven dependencies at all."""
    node = _function_def(_db_tree(), "insert_approval")
    assert not _forven_imports_in(node), (
        "insert_approval() imports "
        f"{sorted(_forven_imports_in(node))}; it is meant to be a pure INSERT. "
        "Anything policy-shaped belongs in control_plane.approvals.create_approval."
    )


def test_workflow_lives_in_the_control_plane():
    assert callable(getattr(cp_approvals, "create_approval", None)), (
        "forven.control_plane.approvals.create_approval is the approval workflow "
        "entry point; it must exist for forven.db.create_approval to delegate to."
    )
    assert callable(getattr(db_mod, "insert_approval", None))
    assert callable(getattr(db_mod, "mark_approval_auto_approved", None))


@pytest.mark.parametrize(
    "dead_name",
    [
        # ARCH-07: strategy_candidates CRUD — no callers anywhere in the repo.
        "list_candidates",
        "create_candidate",
        "get_candidate",
        "update_candidate",
        "delete_candidate",
        "batch_action_candidates",
        "reconcile_core_candidates",
        # ARCH-07: the ghost-integrity block. check_id_gap in particular reads as
        # active protection against container-ID gaps but has never been called
        # since it was added on 2026-03-12.
        "verify_strategy_exists",
        "check_id_gap",
        "reconcile_strategy_list",
        "sync_container_counters",
        "pipeline_completion_verify",
        "log_pipeline_event",
    ],
)
def test_removed_dead_code_stays_removed(dead_name):
    assert not hasattr(db_mod, dead_name), (
        f"forven.db.{dead_name} was deleted as verified-dead code. If it is back, "
        "it needs a real caller and a test — dead protection code that looks live "
        "is worse than no protection code."
    )


# --------------------------------------------------------------------------
# Behaviour parity: the shim and the control-plane entry point agree
# --------------------------------------------------------------------------


def test_shim_and_control_plane_produce_identical_rows(forven_db):
    from forven.control_plane.approval_modes import save_settings

    save_settings({"modes": {"param_optimization": "manual"}})

    via_shim = db_mod.create_approval(
        "  Param_Optimization  ",
        target_type="Strategy",
        target_id="S00042",
        owner="risk-manager",
        payload={"note": "shim"},
    )
    via_control_plane = cp_approvals.create_approval(
        "  Param_Optimization  ",
        target_type="Strategy",
        target_id="S00042",
        owner="risk-manager",
        payload={"note": "control plane"},
    )

    left = db_mod.get_approval(via_shim)
    right = db_mod.get_approval(via_control_plane)
    assert left is not None and right is not None

    # Normalisation (lower-cased type/target_type, owner allowlist) and Phase 5
    # deadline stamping must be identical through both doors.
    expected = {
        "approval_type": "param_optimization",
        "target_type": "strategy",
        "target_id": "S00042",
        "status": "pending_approval",
        "owner": "risk-manager",
    }
    for field, want in expected.items():
        assert left[field] == right[field] == want
    assert left["expires_at"], "Phase 5 expires_at must still be stamped via the shim"
    assert right["expires_at"], "Phase 5 expires_at must be stamped by the control plane"


def test_insert_approval_records_the_deadline_it_is_given(forven_db):
    """Storage records the deadline; it does not compute one."""
    approval_id = db_mod.insert_approval(
        "param_optimization",
        target_id="S00042",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    row = db_mod.get_approval(approval_id)
    assert row is not None
    assert row["expires_at"] == "2099-01-01T00:00:00+00:00"

    # And with no deadline handed down, storage invents none.
    bare_id = db_mod.insert_approval("param_optimization", target_id="S00043")
    bare = db_mod.get_approval(bare_id)
    assert bare is not None
    assert bare["expires_at"] is None


def test_insert_approval_never_runs_mode_application(forven_db, monkeypatch):
    """The off-mode auto-approve is workflow, not storage."""
    from forven.control_plane.approval_modes import save_settings

    save_settings({"modes": {"param_optimization": "off"}})

    calls: list[int] = []
    monkeypatch.setattr(
        cp_approvals,
        "post_approve_approval",
        lambda approval_id, body: calls.append(approval_id),
    )

    approval_id = db_mod.insert_approval("param_optimization", target_id="S00042")
    row = db_mod.get_approval(approval_id)
    assert row is not None
    assert row["status"] == "pending_approval"
    assert not calls, "insert_approval() must never dispatch an approval decision"

    # The same category, created through the workflow, does auto-approve.
    cp_approvals.create_approval("param_optimization", target_id="S00042")
    assert calls, "control_plane.create_approval must still apply mode='off'"


def test_mark_approval_auto_approved_is_storage_only(forven_db):
    approval_id = db_mod.insert_approval("param_optimization", target_id="S00042")
    assert db_mod.get_approval(approval_id)["auto_approved"] in (0, None)

    db_mod.mark_approval_auto_approved(approval_id)
    assert db_mod.get_approval(approval_id)["auto_approved"] == 1
