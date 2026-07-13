#!/usr/bin/env python3
"""
Refresh Gears of War Xbox achievement data for all configured gamertags.

Edit INCLUDE_GAMERTAGS below when the roster changes.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from xbl_gears_lib import (
    DATA_DIR,
    GAMERTAG_MAPPING,
    RATE_LIMIT_PER_HOUR,
    RateLimitedClient,
    achievement_output_path,
    build_player_payload,
    build_summary_entry,
    fetch_player_gears_data,
    gamertag_from_account,
    load_api_key,
    load_gears_games,
    load_name_map,
    lookup_player,
)

# --- Configure which gamertags to refresh ---
INCLUDE_GAMERTAGS = [
    "K0LDING",       # Troels — API owner
    "RadioFrag",     # Frank
    "CarstenPH",     # Carsten
    "peripkiss",     # Per
    "Killer SBN",    # Sandie
    "ClioDK",        # Jan
    "Bjarkov2000",   # Bjarke
    "Cimber63",      # Kaj
]

DEFAULT_SUMMARY = DATA_DIR / "gears_refresh_summary.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh Gears achievement JSON for all gamertags in INCLUDE_GAMERTAGS."
    )
    parser.add_argument(
        "--gamertag",
        action="append",
        dest="gamertags",
        help="Refresh only these gamertag(s). Overrides INCLUDE_GAMERTAGS.",
    )
    parser.add_argument(
        "--summary",
        default=str(DEFAULT_SUMMARY),
        help=f"Combined summary JSON path (default: {DEFAULT_SUMMARY.name}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List targets only; do not call the API.",
    )
    parser.add_argument(
        "--no-auto-wait",
        action="store_true",
        help="Do not pause for hourly rate-limit reset (fail/retry on 429 instead).",
    )
    return parser.parse_args()


def target_gamertags(args: argparse.Namespace) -> list[str]:
    if args.gamertags:
        return args.gamertags
    return list(INCLUDE_GAMERTAGS)


def estimate_requests(count: int, game_count: int) -> int:
    # /account + per-player lookup + ~2 paginated calls per game (player + title schema)
    per_player = 1 + game_count * 2
    return 1 + count * per_player


def main() -> None:
    args = parse_args()
    gamertags = target_gamertags(args)
    games = load_gears_games()
    name_map = load_name_map()

    if not gamertags:
        raise SystemExit("No gamertags configured. Edit INCLUDE_GAMERTAGS in this script.")

    print(f"Targets ({len(gamertags)}):")
    for gt in gamertags:
        first = name_map.get(gt, "?")
        owner = " (API owner)" if gt.upper() == "K0LDING" else ""
        print(f"  - {gt} ({first}){owner}")

    est = estimate_requests(len(gamertags), len(games))
    windows = max(1, (est + RATE_LIMIT_PER_HOUR - 1) // RATE_LIMIT_PER_HOUR)
    print(f"\nEstimated API requests: up to {est} (limit {RATE_LIMIT_PER_HOUR}/hour)")
    if windows > 1:
        print(
            f"Full roster may need ~{windows} hourly windows. "
            "Auto-wait is ON by default — the script will pause and resume when quota resets."
        )
    elif est > RATE_LIMIT_PER_HOUR - 10:
        print("Warning: close to hourly limit.")

    if args.dry_run:
        return

    api_key = load_api_key()
    client = RateLimitedClient(api_key, auto_wait=not args.no_auto_wait)
    if client.auto_wait:
        print("Rate-limit control: auto-wait for hourly reset is enabled.")
    else:
        print("Rate-limit control: auto-wait disabled (--no-auto-wait).")
    summary_players = []

    print("\nFetching API owner account...")
    account = client.get_json("/account")
    owner_gt, owner_xuid, owner_gs = gamertag_from_account(account)
    print(f"  Authenticated as {owner_gt} ({owner_xuid})")

    for gamertag in gamertags:
        first_name = name_map.get(gamertag)
        print(f"\n=== {gamertag} ===")

        try:
            if gamertag.lower() == owner_gt.lower():
                profile = {
                    "xuid": owner_xuid,
                    "gamertag": owner_gt,
                    "gamerscore": owner_gs,
                }
                is_api_owner = True
                print(f"  Using linked account · total GS {owner_gs}")
            else:
                profile = lookup_player(client, gamertag)
                is_api_owner = False
                print(f"  XUID {profile['xuid']} · total GS {profile['gamerscore']}")

            games_out = fetch_player_gears_data(client, profile["xuid"], games)
            payload = build_player_payload(
                first_name=first_name,
                gamertag=profile["gamertag"],
                xuid=profile["xuid"],
                total_gamerscore=profile["gamerscore"],
                is_api_owner=is_api_owner,
                games_out=games_out,
            )

            out_path = achievement_output_path(profile["gamertag"])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"  Wrote {out_path.name}")

            incomplete = [
                g for g in games_out if g.get("summary", {}).get("possiblyIncomplete")
            ]
            for game in incomplete:
                s = game["summary"]
                print(
                    f"  Warning: {game['name']} fetched {s['total']}/"
                    f"{s.get('expectedTotal', '?')} achievements "
                    f"({s['unlocked']} unlocked, {s['locked']} locked)"
                )

            summary_players.append(
                build_summary_entry(
                    first_name=first_name,
                    gamertag=profile["gamertag"],
                    xuid=profile["xuid"],
                    total_gamerscore=profile["gamerscore"],
                    is_api_owner=is_api_owner,
                    output_file=out_path.name,
                    games_out=games_out,
                    fetch_status="ok",
                )
            )
        except Exception as exc:  # noqa: BLE001 — collect and continue
            print(f"  Failed: {exc}")
            summary_players.append(
                {
                    "firstName": first_name,
                    "gamertag": gamertag,
                    "fetchStatus": "error",
                    "error": str(exc),
                }
            )

    summary = {
        "metadata": {
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "source": "OpenXBL",
            "playerCount": len(summary_players),
            "apiRequestsUsed": client.requests_made,
            "rateLimitWaits": client.rate_limit_waits,
            "rateLimitWaitSeconds": round(client.rate_limit_wait_seconds),
            "autoWaitEnabled": client.auto_wait,
            "includeGamertags": gamertags,
            "notes": (
                "Per-player files: data/{Gamertag}_achievements_gearsofwar.json. "
                "Edit INCLUDE_GAMERTAGS in refresh_gears_achievements.py to change roster. "
                "E-Day fetches when titleId is set in gears_games.json. "
                "Achievement fetches paginate OpenXBL and merge title schema so DLC entries "
                "are not dropped when a page limit is hit."
            ),
        },
        "players": sorted(summary_players, key=lambda p: p.get("gamertag", "").lower()),
    }

    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    ok = sum(1 for p in summary_players if p.get("fetchStatus") == "ok")
    print(f"\nWrote summary: {summary_path}")
    print(f"Completed: {ok}/{len(summary_players)} players")
    print(f"Total API requests: {client.requests_made}")
    if client.rate_limit_waits:
        mins, secs = divmod(int(client.rate_limit_wait_seconds), 60)
        print(
            f"Rate-limit pauses: {client.rate_limit_waits} "
            f"(waited {mins}m {secs}s total for quota reset)"
        )


if __name__ == "__main__":
    main()
