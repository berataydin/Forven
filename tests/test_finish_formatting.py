"""ARCH-10, finished: the blank-line properties the first pass scoped out.

The first ARCH-10 pass collapsed 1,957 runs of >=2 blank lines *inside function
bodies* in ``forven/strategies/backtest.py``. Three things were left, and this
module is the gate for all three.

1. **Runs of THREE OR MORE blank lines between top-level definitions.** The
   original brief left these alone "because PEP 8 wants two there". PEP 8 wants
   *exactly* two; a run of eight is not style, it is the same scenery problem in
   a different place. 70 such runs, 404 excess lines.

2. **Blank lines between a ``def`` signature and its first statement.** 62 of
   them. The convention picked here is ZERO, applied uniformly, because:
     * ``ruff format`` *deletes* a 2-blank gap there but never *inserts* one, so
       0 is a fixed point under the formatter and 1 is merely tolerated;
     * 0 is strictly stronger than E303 (whose nested maximum is 1), so this
       rule can never end up fighting the linter.

3. **Blank lines inside brackets** — 863 runs, 1,082 lines, e.g. two blank lines
   between *every single name* in a ``from ... import (...)`` list. Not in the
   original brief, and E303 structurally CANNOT see them (pycodestyle works on
   logical lines, so a blank line inside a continuation is invisible to it).
   ``ruff format`` deletes them; that is the convention adopted here.

Together with the EOF tail (26 blank lines) that is 1,635 lines, 9,545 -> 7,910.

Neutrality was proven five ways before the edit was applied — identical
non-blank line sequence, identical ``ast.dump(include_attributes=False)``,
identical compiled opcode+operand stream, byte-identical docstrings, and
byte-identical comments in order. That harness is not theatre: it caught a real
defect in the first draft of the transform, which rewrote a whitespace-only line
*inside* ``_kill_executor_processes``'s docstring to an empty one. A blank line
in a docstring is payload. Hence ``_string_lines`` below, and hence the rule
that surviving blank lines are sliced from the original, never re-synthesised.

WHY THIS FILE EXISTS INSTEAD OF A ``select = [..., "E303"]`` LINE
----------------------------------------------------------------
E303 is the right rule and it is measured here for real (see the ruff subprocess
tests at the bottom). It is not in pyproject's ``select`` because, on ruff
0.15.18:

  * E303 is preview-gated and ruff has no per-rule preview opt-in. Selecting it
    without ``preview = true`` is a FAKE gate — ruff warns "Selection `E303` has
    no effect because preview is not enabled" and exits 0.
  * ``preview = true`` switches ``--output-format concise`` from rule CODES to
    rule NAMES. Two tests shell out to ruff and grep its output for a code:
    ``test_harden_infra.py::test_test9_api_core_blocking_sleep_stays_a_single_known_instance``
    would find 0 hits and fail its ``== 1`` pin, and
    ``test_arch_api_core.py::test_api_core_has_no_unused_locals_left`` would
    match nothing and stay green while checking NOTHING.

So it is a three-file atomic change and the ARCH-10 group owns neither test
file. Running the real rule in a scoped subprocess here gets the enforcement
without leaking preview mode into the tree-wide ``ruff check`` CI gate.
See the long comment in pyproject.toml's ``[tool.ruff.lint]``.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The execution engine. This is the file ARCH-10 exists for, so it carries the
# strict structural properties; the rest of the tree gets the E303 baseline.
ENGINE = "forven/strategies/backtest.py"


# ---------------------------------------------------------------------------
# Shared line-classification helpers
# ---------------------------------------------------------------------------


def _blank_runs(lines: list[str]) -> list[tuple[int, int]]:
    """Maximal runs of blank lines as inclusive 1-indexed ``(start, end)``.

    "Blank" is ``.strip()``-empty, so a whitespace-only line counts. That
    matters: whitespace-only lines occur *inside* docstrings, where they are
    payload — see ``_string_lines``.
    """
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


def _token_map(path: Path) -> tuple[set[int], set[int], set[int]]:
    """``(string_lines, bracket_lines, def_header_end_lines)``, all 1-indexed.

    * ``string_lines`` — spanned by a string/f-string token. Untouchable.
    * ``bracket_lines`` — lie at bracket depth > 0, i.e. inside a continuation.
      Computed from the gaps BETWEEN tokens, because a blank line carries no
      token of its own; the depth in force across the gap is what places it.
    * ``def_header_end_lines`` — the row of the NEWLINE that closes a ``def``
      header. Taken from tokens rather than from ``node.body[0].lineno - 1`` so
      that multi-line signatures and a comment sitting between the signature and
      the first statement both land in the right place.
    """
    interesting = {tokenize.STRING}
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        tok_type = getattr(tokenize, name, None)
        if tok_type is not None:
            interesting.add(tok_type)

    strings: set[int] = set()
    brackets: set[int] = set()
    def_ends: set[int] = set()

    depth = 0
    prev_end: int | None = None
    seen_def = False
    with path.open("rb") as handle:
        for tok in tokenize.tokenize(handle.readline):
            if tok.type in interesting:
                strings.update(range(tok.start[0], tok.end[0] + 1))
            if depth > 0 and prev_end is not None:
                brackets.update(range(prev_end, tok.start[0] + 1))
            if tok.type == tokenize.OP:
                if tok.string in "([{":
                    depth += 1
                elif tok.string in ")]}":
                    depth -= 1
            elif tok.type == tokenize.NAME and tok.string == "def":
                seen_def = True
            elif tok.type == tokenize.NEWLINE and seen_def:
                def_ends.add(tok.start[0])
                seen_def = False
            prev_end = tok.end[0]
    return strings, brackets, def_ends


def _engine_lines() -> tuple[Path, list[str], set[int], set[int], set[int]]:
    path = REPO_ROOT / ENGINE
    lines = path.read_text(encoding="utf-8").splitlines()
    strings, brackets, def_ends = _token_map(path)
    return path, lines, strings, brackets, def_ends


def _next_code_indent(lines: list[str], after: int) -> int | None:
    """Indent of the next non-blank NON-COMMENT line after 1-indexed ``after``.

    E303 attaches to the following *logical* line, so a comment block parked at
    column 0 inside an indented body must not make the gap look top-level.
    """
    fallback: int | None = None
    for index in range(after, len(lines)):
        stripped = lines[index].strip()
        if not stripped:
            continue
        indent = len(lines[index]) - len(lines[index].lstrip())
        if fallback is None:
            fallback = indent
        if not stripped.startswith("#"):
            return indent
    return fallback


# ---------------------------------------------------------------------------
# Property 1 — at most two blank lines between top-level definitions
# ---------------------------------------------------------------------------


def test_at_most_two_blank_lines_between_top_level_definitions() -> None:
    """PEP 8 wants exactly two between top-level defs, not eight."""
    _, lines, strings, brackets, _ = _engine_lines()

    offenders = []
    for start, end in _blank_runs(lines):
        size = end - start + 1
        if size <= 2:
            continue
        run = range(start, end + 1)
        if any(line in strings for line in run):
            continue  # docstring payload
        if any(line in brackets for line in run):
            continue  # property 3's territory, reported there
        if _next_code_indent(lines, end) != 0:
            continue  # nested; the ARCH-10 in-body rule already covers it
        offenders.append(f"{ENGINE}:{start} ({size} blank lines)")

    assert not offenders, (
        f"{len(offenders)} run(s) of MORE THAN TWO blank lines between top-level "
        "definitions. PEP 8 wants exactly two; the execution engine had 58 runs of "
        "eight. Collapse each to two.\n" + "\n".join(offenders[:10])
    )


# ---------------------------------------------------------------------------
# Property 2 — no blank line between a def signature and its body
# ---------------------------------------------------------------------------


def test_no_blank_line_between_def_signature_and_first_statement() -> None:
    """Convention: ZERO, uniformly.

    Chosen over "one" because ``ruff format`` deletes a 2-blank gap here but
    never inserts a 1-blank one, so zero is a fixed point under the formatter,
    and because zero is strictly stronger than E303's nested maximum of one and
    therefore can never conflict with it.
    """
    _, lines, strings, _, def_ends = _engine_lines()

    offenders = []
    for start, end in _blank_runs(lines):
        if (start - 1) not in def_ends:
            continue
        run = range(start, end + 1)
        if any(line in strings for line in run):
            continue
        signature = lines[start - 2].strip()
        offenders.append(
            f"{ENGINE}:{start} ({end - start + 1} blank) after {signature[:70]!r}"
        )

    assert not offenders, (
        f"{len(offenders)} def signature(s) separated from their first statement by "
        "a blank line. The convention is zero — see this module's docstring.\n"
        + "\n".join(offenders[:10])
    )


# ---------------------------------------------------------------------------
# Property 3 — no blank lines inside brackets (E303 cannot see these)
# ---------------------------------------------------------------------------


def test_no_blank_lines_inside_brackets() -> None:
    """Two blank lines between every name in an import list is not formatting.

    pycodestyle works on logical lines, so E303 is structurally blind to blank
    lines inside a continuation — this property is the reason the AST guard is
    kept alongside the linter rather than replaced by it.
    """
    _, lines, strings, brackets, _ = _engine_lines()

    offenders = []
    for start, end in _blank_runs(lines):
        run = range(start, end + 1)
        if any(line in strings for line in run):
            continue  # a blank line inside a triple-quoted literal is payload
        if not any(line in brackets for line in run):
            continue
        offenders.append(f"{ENGINE}:{start} ({end - start + 1} blank lines)")

    assert not offenders, (
        f"{len(offenders)} blank run(s) inside a bracketed continuation. The engine "
        "carried 1,082 such lines — two blanks between every name of a "
        "`from ... import (...)` list. ruff format deletes these; so does this "
        "gate.\n" + "\n".join(offenders[:10])
    )


# ---------------------------------------------------------------------------
# Property 4 — no blank tail
# ---------------------------------------------------------------------------


def test_engine_does_not_end_in_a_blank_tail() -> None:
    """The engine ended in 26 blank lines. Nothing legitimate does."""
    _, lines, _, _, _ = _engine_lines()
    assert lines and lines[-1].strip(), (
        f"{ENGINE} ends in a run of blank lines. The file must end with its last "
        "statement and a single newline."
    )


# ---------------------------------------------------------------------------
# Structural neutrality — the properties above must never be "fixed" by
# deleting code
# ---------------------------------------------------------------------------


def test_engine_still_parses_and_kept_its_comments() -> None:
    """Cheap tripwire: a whitespace gate must never tempt anyone into deleting.

    Blank-line collapse is only safe because it is provably content-preserving.
    If a future pass "satisfies" the properties above by removing code or
    comments, the counts here move and this fails.
    """
    path, lines, _, _, _ = _engine_lines()
    source = path.read_text(encoding="utf-8")

    ast.parse(source, str(path))  # must still be valid Python

    with path.open("rb") as handle:
        comments = [
            tok.string
            for tok in tokenize.tokenize(handle.readline)
            if tok.type == tokenize.COMMENT
        ]
    assert len(comments) >= 590, (
        f"{ENGINE} has {len(comments)} comments, down from the 600 the ARCH-10 "
        "collapse preserved verbatim. This codebase's incident history lives in "
        "its comments — a formatting change must never remove one."
    )

    non_blank = sum(1 for line in lines if line.strip())
    assert non_blank >= 5800, (
        f"{ENGINE} has {non_blank} non-blank lines, down from the 5,837 that the "
        "collapse left byte-identical. Whitespace work does not delete code."
    )


# ---------------------------------------------------------------------------
# The real linter — pycodestyle E303, run for real
# ---------------------------------------------------------------------------

# Pre-existing E303 offenders, frozen FILE-BY-FILE so the rule gates NEW code.
# 35 hits across these 14 files at the time of writing.
#
# This list may ONLY SHRINK. Adding a path here to make a red test green is
# precisely the failure ARCH-10 exists to prevent. Every hit is auto-fixable, so
# retiring an entry is one command by whoever owns the file:
#
#     python -m ruff check --preview --select E303 --fix <path>
#
# then delete the line. Counts inside a listed file are deliberately not pinned:
# four groups edit this tree in parallel and a count ratchet on files this group
# does not own would go red for other people's unrelated work.
#
# forven/strategies/backtest.py is deliberately ABSENT and is asserted to be
# exactly zero below — it is the file this rule exists for (131 hits -> 0).
E303_BASELINE = frozenset(
    {
        "forven/agents/tools_backtesting.py",
        "forven/api_domains/data.py",
        "forven/api_domains/paper.py",
        "forven/bot.py",
        "forven/brain.py",
        "forven/control_plane/status.py",
        "forven/health_monitor.py",
        "forven/policy.py",
        "forven/providers/discovery.py",
        "forven/regime.py",
        "forven/scanner.py",
        "forven/strategies/params.py",
        "tests/test_daemon_supervisor_decline.py",
        "tests/test_rate_limiting.py",
    }
)


def _e303_hits(*targets: str) -> dict[str, int]:
    """``{repo-relative path: hit count}`` for pycodestyle E303.

    Runs the REAL rule in its own subprocess with ``--preview --select E303``,
    so preview mode never leaks into the tree-wide ``ruff check forven tests``
    gate that CI runs (which greps ruff's output for rule CODES — preview
    renders NAMES instead and would blind it).

    JSON output is parsed rather than the concise text for the same reason: the
    JSON payload keeps a stable ``code`` field in both modes.
    """
    result = subprocess.run(
        [
            sys.executable, "-m", "ruff", "check",
            "--no-cache",
            "--preview",
            "--select", "E303",
            "--output-format", "json",
            *targets,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    # 0 = clean, 1 = violations found. Anything else is ruff itself failing,
    # which must be loud: a silently broken gate is the thing this replaces.
    assert result.returncode in (0, 1), (
        "ruff could not run the E303 gate — a lint rule that cannot execute is "
        f"not a gate.\nexit={result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    hits: dict[str, int] = {}
    for record in json.loads(result.stdout or "[]"):
        assert record["code"] == "E303", record
        rel = Path(record["filename"]).resolve().relative_to(REPO_ROOT).as_posix()
        hits[rel] = hits.get(rel, 0) + 1
    return hits


def test_e303_gate_actually_fires() -> None:
    """Functional proof, not config introspection.

    E303 is preview-gated; selected without ``--preview`` ruff prints a warning
    and exits 0. Without this test the two gates below could silently pass
    forever while measuring nothing.
    """
    result = subprocess.run(
        [
            sys.executable, "-m", "ruff", "check",
            "--no-cache", "--preview", "--select", "E303",
            "--output-format", "json",
            "--stdin-filename", "e303_probe.py", "-",
        ],
        cwd=str(REPO_ROOT),
        input="x = 1\n\n\n\n\ny = 2\n",
        capture_output=True,
        text=True,
        timeout=120,
    )
    records = json.loads(result.stdout or "[]")
    assert [record["code"] for record in records] == ["E303"], (
        "E303 did not fire on a source with four blank lines between two "
        "top-level statements. The gate is not running the rule it claims to — "
        "most likely `--preview` stopped enabling it.\n"
        f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )


def test_execution_engine_is_e303_clean() -> None:
    """The engine itself: zero, no baseline, no exceptions."""
    hits = _e303_hits(ENGINE)
    assert hits == {}, (
        f"{ENGINE} has E303 violations again ({hits}). It went 131 -> 0 in the "
        "ARCH-10 finish and is the one file with no baseline allowance: this is "
        "the module whose 51.9% blank ratio hid a whole dead execution loop."
    )


def test_e303_offender_set_may_only_shrink() -> None:
    """No file outside the frozen baseline may start tripping E303."""
    hits = _e303_hits("forven", "tests")

    new_offenders = sorted(set(hits) - E303_BASELINE)
    assert not new_offenders, (
        "these files newly trip pycodestyle E303 (too many blank lines):\n  "
        + "\n  ".join(f"{path} ({hits[path]} hit(s))" for path in new_offenders)
        + "\n\nFix them rather than extending E303_BASELINE — the baseline may "
        "only shrink:\n    python -m ruff check --preview --select E303 --fix "
        + " ".join(new_offenders)
    )


@pytest.mark.parametrize("path", sorted(E303_BASELINE))
def test_e303_baseline_entries_are_not_stale(path: str) -> None:
    """A baseline entry for a file that no longer trips is dead weight."""
    assert (REPO_ROOT / path).exists(), (
        f"{path} is on the E303 baseline but does not exist — delete the entry"
    )
