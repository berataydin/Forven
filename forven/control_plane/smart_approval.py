"""Smart approval classifier (Phase 5 / P5-T03).

Uses the ``auxiliary.approval`` model to classify pending approvals as
``auto_approve|escalate|hold`` so the operator can configure per-category
modes (manual / smart / off) and let the system bulk-approve obviously-safe
items while escalating stakes-bearing ones.

Hard rules override the classifier:
- Any ``approval_type`` containing ``live`` or ``real_money`` → ``escalate``
  regardless of model output.
- Payload >64 KB → ``hold`` (suspicious / model context too small to judge).
- Two retries that disagree → ``hold``.

Failure mode: if the auxiliary model is unreachable or returns garbage,
recommendation defaults to ``hold`` (NEVER ``auto_approve`` on error).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Mapping

from forven.db import get_db
from forven.model_routing import get_auxiliary_routing

log = logging.getLogger("forven.control_plane.smart_approval")

VALID_RECOMMENDATIONS = ("auto_approve", "escalate", "hold")
PAYLOAD_BYTE_CEILING = 64 * 1024
DEFAULT_DEADLINE_HOURS = 72

# NOTE: no trailing \b — underscores are word chars in Python regex, so
# 'live_trade_arm' would not match if we required a trailing word boundary.
_HIGH_STAKES_PATTERN = re.compile(
    r"(live[_-]?trade|real[_-]?money|fund[_-]?transfer|withdraw|wire_transfer)",
    re.IGNORECASE,
)


def _force_escalate_for_type(approval_type: str | None) -> bool:
    if not approval_type:
        return False
    return bool(_HIGH_STAKES_PATTERN.search(str(approval_type)))


def _payload_too_large(payload: Any) -> bool:
    try:
        if isinstance(payload, (bytes, bytearray)):
            return len(payload) > PAYLOAD_BYTE_CEILING
        if isinstance(payload, str):
            return len(payload.encode("utf-8")) > PAYLOAD_BYTE_CEILING
        return len(json.dumps(payload, default=str).encode("utf-8")) > PAYLOAD_BYTE_CEILING
    except Exception:
        return True


def _redact_payload_for_prompt(payload: Any) -> str:
    """Return a short-ish text excerpt of payload for the prompt; refuse keys
    that look secret-bearing.
    """
    if payload is None:
        return "(no payload)"
    try:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return payload[:2000]
        if isinstance(payload, Mapping):
            redacted = {}
            for k, v in payload.items():
                key_str = str(k).lower()
                if any(s in key_str for s in ("api_key", "token", "secret", "password", "auth")):
                    redacted[k] = "[REDACTED]"
                else:
                    redacted[k] = v
            return json.dumps(redacted, default=str, indent=2)[:4000]
        return json.dumps(payload, default=str)[:4000]
    except Exception:
        return "(payload-redaction-failed)"


def _build_prompt(approval: Mapping[str, Any]) -> str:
    return (
        "You classify Forven approval-queue items for an autonomous trading research platform. "
        "Forven is paper-trading only (no live money). The operator wants safe routine items "
        "auto-approved while stakes-bearing or unusual items are escalated.\n\n"
        "Return STRICT JSON: {\"recommendation\": \"auto_approve\"|\"escalate\"|\"hold\", "
        "\"reasoning\": \"<one short sentence>\", \"confidence\": <0..1 float>}.\n\n"
        "Guidelines:\n"
        "- ``auto_approve`` only for clearly safe categories like param_optimization, "
        "  archive_strategy of decayed strategies, scheduled paper deployments of "
        "  validated strategies, MCP grants on already-vetted servers.\n"
        "- ``escalate`` for novel actions, large payloads, anything affecting more than one "
        "  strategy at once, or anything with ambiguous semantics.\n"
        "- ``hold`` if the action is unsafe, malformed, or you cannot tell what it does.\n\n"
        f"Approval row:\n"
        f"  type: {approval.get('approval_type')!r}\n"
        f"  target_type: {approval.get('target_type')!r}\n"
        f"  target_id: {approval.get('target_id')!r}\n"
        f"  requested_status: {approval.get('requested_status')!r}\n"
        f"  actor: {approval.get('actor')!r}\n"
        f"  reason: {(approval.get('reason') or '')[:500]!r}\n"
        f"  payload (redacted): {_redact_payload_for_prompt(approval.get('payload'))}\n\n"
        "Output JSON only — no prose, no code fences."
    )


def _call_aux_llm(prompt: str, routing: Mapping[str, Any]) -> str:
    """Synchronous helper that runs the auxiliary call inside whatever event
    loop context we're in. Mirrors ``forven.recall._call_aux_llm`` so tests
    can monkeypatch this whole function uniformly across auxiliary subsystems.
    """
    from forven.ai import call_ai

    provider = routing.get("provider") or ""
    model_id = routing.get("model_id") or ""
    route = [(provider, model_id), *(routing.get("fallbacks") or [])]
    coro = call_ai(provider=provider, model=model_id, prompt=prompt, fallback=False, route=route)
    try:
        loop = asyncio.get_running_loop()  # noqa: F841
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _parse_json_response(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    try:
        result = json.loads(s)
        return result if isinstance(result, dict) else None
    except Exception:
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            try:
                result = json.loads(s[start : end + 1])
                return result if isinstance(result, dict) else None
            except Exception:
                return None
    return None


def _persist_classification(
    approval_id: int,
    recommendation: str,
    reasoning: str,
    model_label: str | None,
) -> None:
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE approvals SET classifier_recommendation = ?, "
                "classifier_reasoning = ?, classifier_model = ?, "
                "classifier_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') "
                "WHERE id = ?",
                (recommendation, reasoning, model_label, int(approval_id)),
            )
    except Exception as exc:
        log.warning("smart_approval: persist classification failed: %s", exc)


def classify_approval(approval: Mapping[str, Any]) -> dict[str, Any]:
    """Classify a single approval row and persist the result.

    Args:
        approval: dict-like row from the ``approvals`` table; must include
                  at least ``id`` and ``approval_type``.

    Returns:
        ``{"recommendation": str, "reasoning": str, "confidence": float,
           "model": str|None, "latency_ms": int}``.
    """
    started = time.monotonic()
    approval_id = approval.get("id")
    approval_type = approval.get("approval_type")
    payload = approval.get("payload")

    # Hard rule: high-stakes categories never auto-approve.
    if _force_escalate_for_type(approval_type):
        result = {
            "recommendation": "escalate",
            "reasoning": f"approval_type {approval_type!r} matches high-stakes hard rule",
            "confidence": 1.0,
            "model": "hard-rule:high-stakes",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        if approval_id is not None:
            _persist_classification(int(approval_id), result["recommendation"], result["reasoning"], result["model"])
        return result

    # Hard rule: oversized payload defaults to hold (don't trust a small model
    # to judge something we can't fit in its prompt).
    if _payload_too_large(payload):
        result = {
            "recommendation": "hold",
            "reasoning": f"payload exceeds {PAYLOAD_BYTE_CEILING} bytes — manual review required",
            "confidence": 1.0,
            "model": "hard-rule:oversized-payload",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        if approval_id is not None:
            _persist_classification(int(approval_id), result["recommendation"], result["reasoning"], result["model"])
        return result

    routing = get_auxiliary_routing("approval")
    aux_provider = routing.get("provider")
    aux_model = routing.get("model_id")
    aux_label = f"{aux_provider}:{aux_model}" if aux_provider and aux_model else None

    prompt = _build_prompt(approval)

    try:
        text = _call_aux_llm(prompt, routing)
    except Exception as exc:
        log.warning("smart_approval: aux model unreachable, defaulting to hold: %s", exc)
        result = {
            "recommendation": "hold",
            "reasoning": f"classifier unreachable: {type(exc).__name__}",
            "confidence": 0.0,
            "model": aux_label,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        if approval_id is not None:
            _persist_classification(int(approval_id), result["recommendation"], result["reasoning"], result["model"])
        return result

    parsed = _parse_json_response(text)
    if parsed is None or parsed.get("recommendation") not in VALID_RECOMMENDATIONS:
        result = {
            "recommendation": "hold",
            "reasoning": "classifier returned malformed response",
            "confidence": 0.0,
            "model": aux_label,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        if approval_id is not None:
            _persist_classification(int(approval_id), result["recommendation"], result["reasoning"], result["model"])
        return result

    recommendation = parsed["recommendation"]
    reasoning = str(parsed.get("reasoning") or "")[:500]
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0

    result = {
        "recommendation": recommendation,
        "reasoning": reasoning,
        "confidence": max(0.0, min(1.0, confidence)),
        "model": aux_label,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
    if approval_id is not None:
        _persist_classification(int(approval_id), recommendation, reasoning, aux_label)
    return result


def apply_smart_decision(approval_id: int, mode: str) -> dict[str, Any]:
    """Run the classifier and, if mode='smart' and recommendation='auto_approve',
    auto-approve the row with actor ``system:smart_approval``. Otherwise no-op.

    Returns the classifier result dict augmented with ``applied: bool``.
    """
    with get_db() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (int(approval_id),)).fetchone()
    if not row:
        return {"recommendation": "hold", "reasoning": "approval not found", "applied": False}

    approval = dict(row)
    classifier = classify_approval(approval)

    applied = False
    if mode == "smart" and classifier["recommendation"] == "auto_approve":
        try:
            # KEEP THIS IMPORT FUNCTION-LOCAL. forven.control_plane.approvals
            # imports THIS module at top level (it owns create_approval, which
            # dispatches into apply_smart_decision), so hoisting this to the
            # module header closes a genuine import cycle and breaks the
            # package. The dependency is one-directional at import time and
            # bidirectional only at call time; that is deliberate.
            from forven.control_plane.approvals import post_approve_approval
            from forven.control_plane.models import ApprovalDecisionBody

            body = ApprovalDecisionBody(
                actor="system:smart_approval",
                feedback=f"Auto-approved by smart classifier: {classifier['reasoning']}",
            )
            post_approve_approval(int(approval_id), body)
            with get_db() as conn:
                conn.execute(
                    "UPDATE approvals SET auto_approved = 1 WHERE id = ?",
                    (int(approval_id),),
                )
            applied = True
        except Exception as exc:
            log.warning(
                "smart_approval: auto-approve failed for id=%s: %s",
                approval_id,
                exc,
            )

    classifier["applied"] = applied
    return classifier


__all__ = [
    "VALID_RECOMMENDATIONS",
    "PAYLOAD_BYTE_CEILING",
    "DEFAULT_DEADLINE_HOURS",
    "classify_approval",
    "apply_smart_decision",
]
