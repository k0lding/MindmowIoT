"""
Shared helpers for fetching and normalizing Gears of War achievement data via OpenXBL.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://xbl.io/api/v2"
DATA_DIR = Path(__file__).resolve().parent / "data"
GAMES_CONFIG = Path(__file__).resolve().parent / "gears_games.json"
NAME_MAP_FILE = Path(__file__).resolve().parent / "gears_name_map.json"

GAMERTAG_MAPPING = {
    "K0LDING": "Troels",
    "RadioFrag": "Frank",
    "CarstenPH": "Carsten",
    "peripkiss": "Per",
    "Killer SBN": "Sandie",
    "ClioDK": "Jan",
    "Bjarkov2000": "Bjarke",
    "Cimber63": "Kaj",
}

# Known full totals (achievement count, gamerscore) including DLC where applicable.
EXPECTED_TOTALS: dict[str, dict[str, int | str]] = {
    "1297287125": {"name": "Gears of War", "count": 34, "gamerscore": 700},
    "1297287213": {"name": "Gears of War 2", "count": 70, "gamerscore": 1465},
    "1297287339": {"name": "Gears of War 3", "count": 82, "gamerscore": 2000},
    "1297287718": {"name": "Gears of War: Judgment", "count": 70, "gamerscore": 1500},
    "1475571605": {"name": "Gears of War: Ultimate Edition", "count": 56, "gamerscore": 1250},
    "552499398": {"name": "Gears of War 4", "count": 176, "gamerscore": 4000},
    "374923716": {"name": "Gears 5", "count": 181, "gamerscore": 2500},
    "1616046491": {"name": "Gears Tactics", "count": 61, "gamerscore": 1400},
    "1829869520": {"name": "Gears of War: Reloaded", "count": 56, "gamerscore": 1250},
}

TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "campaign": (
        "campaign",
        "act ",
        "chapter",
        "story",
        "arcade",
        "declassified",
        "collectible",
        "cog tag",
        "ironman",
        "difficulty",
        "insane",
        "hardcore",
        "casual",
        "inconceivable",
    ),
    "pve": (
        "horde",
        "escape",
        "beast",
        "survival",
        "overrun",
        "wave",
        "class",
        "skill card",
        "engineer",
        "soldier",
        "sniper",
        "heavy",
        "scout",
        "tactics",
        "mission",
        "side mission",
        "scavenger",
        "sabotage",
    ),
    "pvp": (
        "versus",
        "ranked",
        "competitive",
        "team deathmatch",
        "domination",
        "free for all",
        "wingman",
        "2v2",
        "ffa",
        "multiplayer match",
        "playlist",
        "ribbon",
    ),
}


class RateLimitedClient:
    def __init__(self, api_key: str, min_interval_s: float = 0.35) -> None:
        self.api_key = api_key
        self.min_interval_s = min_interval_s
        self.requests_made = 0
        self._last_request_at = 0.0

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = f"?{urlencode(params)}" if params else ""
        url = f"{API_BASE}{path}{query}"
        req = Request(url, headers={"X-Authorization": self.api_key, "Accept": "application/json"})
        self._throttle()
        try:
            with urlopen(req, timeout=60) as resp:
                self.requests_made += 1
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenXBL {path} failed ({exc.code}): {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"OpenXBL {path} failed: {exc}") from exc

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)
        self._last_request_at = time.monotonic()


def load_api_key() -> str:
    key = os.environ.get("OPENXBL_API_KEY", "").strip()
    if key:
        return key
    key_file = Path(__file__).resolve().parent / ".openxbl_api_key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    raise RuntimeError("Set OPENXBL_API_KEY or create .openxbl_api_key next to xbl_gears_lib.py")


def load_name_map() -> dict[str, str]:
    if NAME_MAP_FILE.exists():
        return json.loads(NAME_MAP_FILE.read_text(encoding="utf-8"))
    return dict(GAMERTAG_MAPPING)


def load_gears_games() -> list[dict[str, Any]]:
    games = json.loads(GAMES_CONFIG.read_text(encoding="utf-8"))
    for game in games:
        if not game.get("titleId") and game.get("envTitleId"):
            env_val = os.environ.get(game["envTitleId"], "").strip()
            if env_val:
                game["titleId"] = env_val
    return games


def achievement_output_path(gamertag: str) -> Path:
    safe = re.sub(r"[^\w\- ]+", "", gamertag).strip().replace(" ", "_")
    return DATA_DIR / f"{safe}_achievements_gearsofwar.json"


def gamertag_from_account(account: dict[str, Any]) -> tuple[str, str, int]:
    profile = account.get("profileUsers", [{}])[0]
    settings = profile.get("settings", [])
    gamertag = next((s["value"] for s in settings if s.get("id") == "Gamertag"), "")
    xuid = str(profile.get("id", ""))
    gamerscore = int(next((s["value"] for s in settings if s.get("id") == "Gamerscore"), 0))
    if not gamertag or not xuid:
        raise RuntimeError("Could not read gamertag/xuid from /account response")
    return gamertag, xuid, gamerscore


def lookup_player(client: RateLimitedClient, gamertag: str) -> dict[str, Any]:
    data = client.get_json("/friends/search", params={"gt": gamertag})
    people = data.get("people") or data.get("profileUsers") or []
    if not people:
        raise RuntimeError(f"Gamertag not found: {gamertag}")
    person = people[0]
    xuid = str(person.get("xuid") or person.get("id") or "")
    gt = person.get("gamertag") or person.get("displayName") or gamertag
    gs = int(person.get("gamerscore") or person.get("gamerScore") or 0)
    if not xuid:
        raise RuntimeError(f"No XUID for gamertag: {gamertag}")
    return {"xuid": xuid, "gamertag": gt, "gamerscore": gs}


def _achievement_path(endpoint: str, xuid: str, title_id: str) -> str:
    if endpoint == "x360":
        return f"/achievements/x360/{xuid}/title/{title_id}"
    return f"/achievements/player/{xuid}/title/{title_id}"


def _title_schema_path(title_id: str) -> str:
    return f"/achievements/title/{title_id}"


def _extract_continuation_token(payload: dict[str, Any]) -> str | None:
    paging = payload.get("pagingInfo") or {}
    token = paging.get("continuationToken")
    if token:
        return str(token)
    top = payload.get("continuationToken")
    if top:
        return str(top)
    return None


def _extract_achievements(payload: dict[str, Any]) -> list[dict[str, Any]]:
    achievements = payload.get("achievements")
    if isinstance(achievements, list):
        return achievements
    if isinstance(payload.get("achievements"), dict):
        return payload["achievements"].get("achievements", [])
    return []


def fetch_paginated(client: RateLimitedClient, base_path: str) -> list[dict[str, Any]]:
    """Fetch every page for an OpenXBL achievements endpoint."""
    all_rows: list[dict[str, Any]] = []
    path = base_path
    params: dict[str, Any] | None = None
    seen_tokens: set[str] = set()

    while True:
        payload = client.get_json(path, params=params)
        all_rows.extend(_extract_achievements(payload))
        token = _extract_continuation_token(payload)
        if not token or token in seen_tokens:
            break
        seen_tokens.add(token)
        # Player endpoints paginate via query param on the same route.
        path = base_path
        params = {"continuationToken": token}

    return all_rows


def fetch_title_achievements(client: RateLimitedClient, endpoint: str, xuid: str, title_id: str) -> list[dict[str, Any]]:
    base = _achievement_path(endpoint, xuid, title_id)
    player_rows = fetch_paginated(client, base)
    if player_rows:
        return player_rows

    # Fallback for endpoints that only expose title schema for the signed-in account.
    schema_rows = fetch_paginated(client, _title_schema_path(title_id))
    return schema_rows


def _reward_gamerscore(achievement: dict[str, Any]) -> int:
    for reward in achievement.get("rewards", []):
        if reward.get("type") == "Gamerscore":
            try:
                return int(reward.get("value", 0))
            except (TypeError, ValueError):
                return 0
    return int(achievement.get("gamerscore") or achievement.get("achievementGamerscore") or 0)


def _achievement_id(achievement: dict[str, Any]) -> str:
    for key in ("id", "achievementId", "legacyAchievementId"):
        if achievement.get(key) is not None:
            return str(achievement[key])
    return str(achievement.get("name", ""))


def _achievement_name(achievement: dict[str, Any]) -> str:
    return str(achievement.get("name") or achievement.get("title") or "Unknown")


def _achievement_description(achievement: dict[str, Any]) -> str:
    return str(achievement.get("description") or achievement.get("lockedDescription") or "").strip()


def _progress_percentage(achievement: dict[str, Any]) -> float | None:
    progression = achievement.get("progression") or {}
    requirements = progression.get("requirements") or achievement.get("requirements") or []
    if requirements:
        values = []
        for req in requirements:
            current = req.get("current")
            target = req.get("target")
            if current is not None and target:
                try:
                    values.append((float(current) / float(target)) * 100.0)
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
        if values:
            return max(values)
    if achievement.get("percentComplete") is not None:
        try:
            return float(achievement["percentComplete"])
        except (TypeError, ValueError):
            return None
    return None


def _normalize_status(achievement: dict[str, Any]) -> dict[str, Any]:
    state = achievement.get("progressState") or achievement.get("achievementState") or "NotStarted"
    unlocked = state == "Achieved"
    unlocked_at = achievement.get("progression", {}).get("timeUnlocked") or achievement.get("dateUnlocked")
    if isinstance(unlocked_at, dict):
        unlocked_at = unlocked_at.get("date")
    if not unlocked_at:
        unlocked_at = "0001-01-01T00:00:00.0000000Z"
    return {
        "status": "unlocked" if unlocked else "locked",
        "progressState": state,
        "unlocked": unlocked,
        "unlockedAt": unlocked_at,
        "progressPercentage": _progress_percentage(achievement),
    }


def _normalize_rarity(achievement: dict[str, Any]) -> dict[str, Any]:
    rarity = achievement.get("rarity") or {}
    current = rarity.get("currentPercentage")
    if current is None:
        current = achievement.get("rarityCurrentPercentage")
    try:
        pct = float(current) if current is not None else None
    except (TypeError, ValueError):
        pct = None
    category = rarity.get("currentCategory") or achievement.get("rarityCurrentCategory") or "Unknown"
    return {"currentCategory": category, "currentPercentage": pct}


def infer_types(description: str) -> list[str]:
    text = description.lower()
    types = [name for name, keywords in TYPE_KEYWORDS.items() if any(k in text for k in keywords)]
    return types or ["campaign"]


def normalize_achievement(raw: dict[str, Any]) -> dict[str, Any]:
    description = _achievement_description(raw)
    return {
        "id": _achievement_id(raw),
        "name": _achievement_name(raw),
        "gamerscore": _reward_gamerscore(raw),
        "types": infer_types(description),
        "status": _normalize_status(raw),
        "description": description,
        "isSecret": bool(raw.get("isSecret")),
        "rarity": _normalize_rarity(raw),
    }


def _merge_player_and_schema(player_rows: list[dict[str, Any]], schema_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure achievements present in the title schema but missing from player progress appear as locked."""
    by_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}

    for row in player_rows:
        norm = normalize_achievement(row)
        by_id[norm["id"]] = norm
        by_name[norm["name"].casefold()] = norm

    for row in schema_rows:
        norm = normalize_achievement(row)
        if norm["id"] in by_id or norm["name"].casefold() in by_name:
            continue
        norm["status"] = {
            "status": "locked",
            "progressState": "NotStarted",
            "unlocked": False,
            "unlockedAt": "0001-01-01T00:00:00.0000000Z",
            "progressPercentage": 0.0,
        }
        by_id[norm["id"]] = norm

    return sorted(by_id.values(), key=lambda a: (a["name"].casefold(), a["id"]))


def build_game_summary(achievements: list[dict[str, Any]], title_id: str | None) -> dict[str, Any]:
    unlocked = [a for a in achievements if a["status"]["unlocked"]]
    locked = [a for a in achievements if not a["status"]["unlocked"]]
    summary = {
        "total": len(achievements),
        "unlocked": len(unlocked),
        "locked": len(locked),
        "gamerscoreUnlocked": sum(a["gamerscore"] for a in unlocked),
        "gamerscoreTotal": sum(a["gamerscore"] for a in achievements),
    }
    if title_id and title_id in EXPECTED_TOTALS:
        expected = EXPECTED_TOTALS[title_id]
        summary["expectedTotal"] = expected["count"]
        summary["expectedGamerscore"] = expected["gamerscore"]
        summary["possiblyIncomplete"] = len(achievements) < int(expected["count"])
        summary["complete"] = (
            len(achievements) >= int(expected["count"])
            and len(locked) == 0
            and summary["gamerscoreTotal"] >= int(expected["gamerscore"])
        )
    return summary


def fetch_game_achievements(client: RateLimitedClient, xuid: str, game: dict[str, Any]) -> dict[str, Any]:
    title_id = game.get("titleId")
    if not title_id:
        return {
            "titleId": None,
            "name": game["name"],
            "endpoint": game.get("endpoint", "modern"),
            "released": False,
            "fetchStatus": "skipped",
            "reason": game.get(
                "reason",
                "No Xbox title ID until the game ships and appears on Xbox Live. "
                "Set titleId here after launch, or export OPENXBL_EDAY_TITLE_ID.",
            ),
            "summary": {
                "total": 0,
                "unlocked": 0,
                "locked": 0,
                "gamerscoreUnlocked": 0,
                "gamerscoreTotal": 0,
            },
            "achievements": [],
        }

    endpoint = game.get("endpoint", "modern")
    player_rows = fetch_paginated(client, _achievement_path(endpoint, xuid, title_id))
    schema_rows = fetch_paginated(client, _title_schema_path(title_id))
    achievements = _merge_player_and_schema(player_rows, schema_rows)
    summary = build_game_summary(achievements, title_id)

    result: dict[str, Any] = {
        "titleId": title_id,
        "name": game["name"],
        "endpoint": endpoint,
        "summary": summary,
        "achievements": achievements,
        "released": True,
        "fetchStatus": "ok",
    }
    if summary.get("possiblyIncomplete"):
        expected = EXPECTED_TOTALS.get(title_id, {})
        result["fetchWarning"] = (
            f"Fetched {summary['total']} achievements; expected {expected.get('count', '?')} "
            f"for {game['name']}. Pagination or title schema merge may still be incomplete."
        )
    return result


def fetch_player_gears_data(client: RateLimitedClient, xuid: str, games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [fetch_game_achievements(client, xuid, game) for game in games]


def build_player_payload(
    *,
    first_name: str | None,
    gamertag: str,
    xuid: str,
    total_gamerscore: int,
    is_api_owner: bool,
    games_out: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "metadata": {
            "firstName": first_name,
            "gamertag": gamertag,
            "xuid": xuid,
            "totalGamerscore": total_gamerscore,
            "isApiOwner": is_api_owner,
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "source": "OpenXBL",
            "apiBase": API_BASE,
            "typeInference": "Inferred from achievement descriptions; types are multi-select: campaign, pve, pvp.",
        },
        "games": games_out,
    }


def build_summary_entry(
    *,
    first_name: str | None,
    gamertag: str,
    xuid: str,
    total_gamerscore: int,
    is_api_owner: bool,
    output_file: str,
    games_out: list[dict[str, Any]],
    fetch_status: str,
) -> dict[str, Any]:
    warnings = [g["fetchWarning"] for g in games_out if g.get("fetchWarning")]
    incomplete = [
        {
            "game": g["name"],
            "fetched": g["summary"]["total"],
            "expected": g["summary"].get("expectedTotal"),
            "unlocked": g["summary"]["unlocked"],
            "locked": g["summary"]["locked"],
        }
        for g in games_out
        if g.get("summary", {}).get("possiblyIncomplete")
    ]
    return {
        "firstName": first_name,
        "gamertag": gamertag,
        "xuid": xuid,
        "totalGamerscore": total_gamerscore,
        "isApiOwner": is_api_owner,
        "outputFile": output_file,
        "fetchStatus": fetch_status,
        "gameCount": len(games_out),
        "possiblyIncompleteGames": incomplete,
        "warnings": warnings,
    }
