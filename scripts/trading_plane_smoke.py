from __future__ import annotations

import argparse
import json
import sys

from forven.trading_smoke import collect_trading_plane_smoke, resolve_effective_testnet


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Forven trading-plane smoke against HyperLiquid."
    )
    parser.add_argument(
        "--place-test-order",
        action="store_true",
        help="Run an explicit testnet open/close/reconcile order smoke.",
    )
    parser.add_argument(
        "--asset",
        type=str,
        default=None,
        help="Preferred asset for the active smoke order. Defaults to a safe candidate selection.",
    )
    parser.add_argument(
        "--usd-notional",
        type=float,
        default=15.0,
        help="Approximate USD notional for the active smoke order.",
    )
    parser.add_argument(
        "--direction",
        choices=("long", "short"),
        default="long",
        help="Direction to use for the active smoke order.",
    )
    parser.add_argument(
        "--strategy-id",
        type=str,
        default="SOAK_HL_SMOKE",
        help="Strategy ID label recorded on the smoke trade row.",
    )
    parser.add_argument(
        "--mainnet",
        action="store_true",
        help=(
            "Request mainnet instead of testnet. NOTE: the network is ultimately resolved "
            "from the configured HyperLiquid credentials (USE_TESTNET), so omitting this "
            "does NOT guarantee testnet. Active orders remain blocked unless --allow-mainnet "
            "is also set."
        ),
    )
    parser.add_argument(
        "--allow-mainnet",
        action="store_true",
        help="Allow the active smoke order to run on mainnet when --mainnet is set.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation level for output.",
    )
    args = parser.parse_args()

    # MAINNET-GUARD-1: --mainnet is only a REQUEST. get_exchange resolves the
    # endpoint from the credentials (USE_TESTNET), so a plain invocation can still
    # land on mainnet — which is exactly how an "it's only testnet" smoke would
    # have opened a real position. Say so on stderr before anything is placed;
    # collect_trading_plane_smoke gates the active order on the resolved value.
    requested_testnet = not bool(args.mainnet)
    effective_testnet = resolve_effective_testnet(requested_testnet)
    if effective_testnet != requested_testnet:
        print(
            f"WARNING: requested testnet={requested_testnet} but the configured HyperLiquid "
            f"credentials resolve to testnet={effective_testnet}. This smoke will run against "
            f"{'TESTNET' if effective_testnet else 'MAINNET (REAL FUNDS)'}.",
            file=sys.stderr,
        )

    report = collect_trading_plane_smoke(
        testnet=requested_testnet,
        place_test_order=bool(args.place_test_order),
        allow_mainnet=bool(args.allow_mainnet),
        asset=args.asset,
        usd_notional=float(args.usd_notional),
        direction=str(args.direction),
        strategy_id=str(args.strategy_id),
    )
    print(json.dumps(report, indent=max(int(args.indent), 0), sort_keys=False))

    status = str(report.get("status") or "fail").lower()
    if status == "ok":
        return 0
    if status == "warn":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
