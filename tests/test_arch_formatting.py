"""ARCH-10: the execution engine must not drown in whitespace again.

``forven/strategies/backtest.py`` — the single most safety-critical file in the
repo — was 51.9% blank lines (6,295 of 12,132). That is not a cosmetic
complaint. It is *why* the 167 lines of unreachable dead code that ARCH-04
removed survived for so long: ~80 lines of real code adrift in whitespace do
not read as a block, they read as scenery. A second, byte-DIVERGENT copy of the
execution loop sat behind an unconditional return and greped identically to the
live code, and nobody scrolled far enough to notice.

The collapse (2,587 blank lines removed, 51.9% -> 38.8%) was proven
behaviour-neutral four ways: identical non-blank line sequence, identical
``ast.dump(..., include_attributes=False)``, identical compiled opcode stream,
and byte-identical docstrings and comments.

These tests keep it collapsed. The invariant here deliberately covers only blank
runs INSIDE a function or method body:

  * blank lines BETWEEN top-level definitions are not this file's business — but
    they are no longer unguarded: a later pass found 70 runs of THREE OR MORE
    between top-level defs (PEP 8 wants *exactly* two, and 58 of them were runs
    of eight) and collapsed them. That property, the def-signature gap, and
    blank lines inside brackets now live in ``tests/test_finish_formatting.py``;
  * blank lines inside string literals and docstrings are untouchable — they are
    payload, not formatting, so string regions are found with ``tokenize``
    rather than a regex that cannot tell code from a triple-quoted block. This
    is not hypothetical: the finishing pass's first draft rewrote a
    whitespace-only line inside ``_kill_executor_processes``'s docstring, and
    only the AST/docstring proof caught it.

The equivalent linter rule is pycodestyle E303 (``too-many-blank-lines``). It is
now measured for real, in a scoped subprocess, by
``tests/test_finish_formatting.py`` — it is still not in pyproject's ``select``
because reaching it requires repo-wide ``preview = true``, which switches ruff's
concise output from rule CODES to rule NAMES and thereby breaks one ruff-output
gate and silently blinds another. See that module's docstring for the evidence.

These AST checks are NOT redundant with E303. E303 works on logical lines, so it
is structurally blind to blank lines inside a bracketed continuation — the very
shape that accounted for 1,082 of the engine's blank lines.
"""

from __future__ import annotations

import ast
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Files this guard owns. backtest.py is the reason it exists; scanner.py is
# measured alongside it because it is the other 8k-line module in the hot path
# (it was already clean at 8.8% blank, below the 12.8% repo median, and had ZERO
# collapsible in-body runs — it is pinned here so it stays that way).
GUARDED = (
    "forven/strategies/backtest.py",
    "forven/scanner.py",
)

# backtest.py: 51.9% blank originally, 38.8% after the in-body collapse, 26.2%
# (2,073 of 7,910) after the finishing pass took the top-level, def-signature,
# in-bracket and EOF runs. The ceiling is ratcheted down with it — a ceiling
# with 19 points of slack gates nothing. 0.35 still leaves room for ~1,000 new
# blank lines of docstrings and comments before it trips.
#
# Ratchet these DOWN as the files improve; never up. If a legitimate change
# needs more headroom, that is a conversation, not a one-line edit.
MAX_BLANK_RATIO = {"forven/strategies/backtest.py": 0.35, "forven/scanner.py": 0.15}


def _function_body_lines(tree: ast.AST) -> set[int]:
    """1-indexed physical lines that lie inside some function/method body.

    A body spans its first statement through its last, so blank lines before the
    first statement (the def-signature gap) and after the last one (the gap to
    the next definition) are correctly excluded.
    """
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            inside.update(range(node.body[0].lineno, node.body[-1].end_lineno + 1))
    return inside


def _string_lines(path: Path) -> set[int]:
    """1-indexed physical lines spanned by a string/f-string token.

    Blank lines inside a docstring belong to the docstring. Never collapse them,
    and never let this guard *demand* they be collapsed.
    """
    interesting = {tokenize.STRING}
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        tok_type = getattr(tokenize, name, None)
        if tok_type is not None:
            interesting.add(tok_type)

    protected: set[int] = set()
    with path.open("rb") as handle:
        for tok in tokenize.tokenize(handle.readline):
            if tok.type in interesting:
                protected.update(range(tok.start[0], tok.end[0] + 1))
    return protected


def _blank_runs(lines: list[str]) -> list[tuple[int, int]]:
    """Maximal runs of blank lines as inclusive 1-indexed ``(start, end)``."""
    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        if lines[index].strip():
            index += 1
            continue
        end = index
        while end + 1 < len(lines) and not lines[end + 1].strip():
            end += 1
        runs.append((index + 1, end + 1))
        index = end + 1
    return runs


@pytest.mark.parametrize("rel_path", GUARDED)
def test_no_consecutive_blank_lines_inside_function_bodies(rel_path: str) -> None:
    """No run of 2+ blank lines may sit inside a function or method body."""
    path = REPO_ROOT / rel_path
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()

    inside = _function_body_lines(ast.parse(source, str(path)))
    protected = _string_lines(path)

    offenders = []
    for start, end in _blank_runs(lines):
        if end == start:
            continue
        run = range(start, end + 1)
        if any(line in protected for line in run):
            continue  # inside a docstring / triple-quoted literal — payload
        if not all(line in inside for line in run):
            continue  # between definitions — PEP 8's territory, not ours
        offenders.append(f"{rel_path}:{start} ({end - start + 1} blank lines)")

    assert not offenders, (
        f"{len(offenders)} run(s) of consecutive blank lines inside function bodies. "
        "Collapse each to a single blank line — padding the execution path with "
        "whitespace is how ARCH-04's dead execution loop stayed hidden. "
        f"First 10: {offenders[:10]}"
    )


@pytest.mark.parametrize("rel_path", GUARDED)
def test_blank_line_ratio_stays_under_ceiling(rel_path: str) -> None:
    """Coarse net for whitespace creep that the in-body rule alone would miss.

    Catches drift that hides between top-level definitions, where the per-run
    rule deliberately does not reach.
    """
    lines = (REPO_ROOT / rel_path).read_text(encoding="utf-8").splitlines()
    blank = sum(1 for line in lines if not line.strip())
    ratio = blank / len(lines)

    assert ratio <= MAX_BLANK_RATIO[rel_path], (
        f"{rel_path} is {ratio:.1%} blank lines ({blank}/{len(lines)}), over the "
        f"{MAX_BLANK_RATIO[rel_path]:.0%} ceiling. backtest.py was 51.9% blank when "
        "167 lines of unreachable code went unnoticed inside it."
    )
