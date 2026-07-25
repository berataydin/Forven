"""One-shot data repair: normalize S06895's malformed registered symbol.

S06895 (BTC_FUNDING_TREND_ALIGN, DSR 0.806, composite 100, 14.4mo evidence)
was imported with the timeframe baked into its symbol — ``BTC-USDT-4H`` — an
old importer bug shared by 39 strategies. The current data layer refuses it
("binance does not have market symbol BTC/USDT-4H ... fix the strategy's
symbol and re-register"), which failed its 2026-07-22 audit re-adjudication
at quick_screen (non-merit, its 10th non-merit death).

Repair: symbol -> ``BTC/USDT`` per the strategy's own params
(``_asset: "BTC"``, ``_data_requirements: [{asset: BTC}]``); the timeframe
stays ``4h`` in its own column. Guarded (aborts unless the symbol is exactly
the malformed string) and writes a strategy_event for the audit trail.

After running, revive with:
  python scripts/revive_wrongly_archived_2026_07_22.py --only S06895 --execute --backend-restarted
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from forven.db import get_db

BAD = "BTC-USDT-4H"
GOOD = "BTC/USDT"


def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT symbol, stage FROM strategies WHERE id='S06895'"
        ).fetchone()
        if not row:
            print("S06895 not found — aborting")
            return 1
        if row["symbol"] != BAD:
            print(f"symbol is {row['symbol']!r}, expected {BAD!r} — nothing to repair, aborting")
            return 1
        conn.execute(
            "UPDATE strategies SET symbol=?, updated_at=? WHERE id='S06895'",
            (GOOD, now),
        )
        conn.execute(
            "INSERT INTO strategy_events (strategy_id, from_state, to_state, actor, reason, details_json, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "S06895", row["stage"], row["stage"], "ui",
                "Operator repair: registered symbol was the malformed import string "
                f"'{BAD}' (timeframe baked into the symbol) — the data layer refuses it, "
                "which failed the 2026-07-22 audit re-adjudication at quick_screen "
                f"(non-merit). Normalized to {GOOD} per the strategy's own "
                "_data_requirements (asset=BTC); timeframe stays 4h in its own column. "
                "See outputs/archived-strategy-audit-2026-07-22.md.",
                json.dumps({"old_symbol": BAD, "new_symbol": GOOD, "motion": "data_repair"}),
                now,
            ),
        )
    print(f"S06895 symbol repaired: {BAD} -> {GOOD} (audit event written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
