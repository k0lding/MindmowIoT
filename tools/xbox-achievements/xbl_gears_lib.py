"""Shared OpenXBL helpers for Gears of War achievement fetch scripts."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
GAMES_CONFIG = Path(__file__).resolve().parent / "gears_games.json"
GAMERTAG_MAPPING = ROOT / "gamertag-mapping.md"
DATA_DIR = ROOT / "data"

API_BASE = "https://xbl.io/api/v2"
RATE_LIMIT_PER_HOUR = 150
MIN_REQUEST_INTERVAL_SEC = 3600 / RATE_LIMIT_PER_HOUR
LOW_REMAINING_THRESHOLD = 10
LOW_REMAINING_SLEEP_SEC = 60

PVP_KEYWORDS = (
    "multiplayer", "versus", " ranked", "team deathmatch", "king of the hill",
    "annex", "warzone", "execution", "wingman", "capture the leader",
    "online match", "competitive", "re-up", "opposing team", "opponent",
    "other player", "other players", "adversarial", "matchmade", "ranked ",
    "gears 5 ranked", "control", "gridiron", "escalation", "arms race",
    "dodgeball", "team death", "gears allies", "ally in gears",
)

PVE_KEYWORDS = (
    "horde", "escape", "hivebuster", "hivebusters", "survival", "overrun",
    "beast mode", "beast ", "waves of", "wave of", "pve", "arcade horde",
    "hive master", "boss wave",
)

CAMPAIGN_KEYWORDS = (
    "campaign", " act ", "chapter", "story mode", "single player", "solo ",
    "del's", "del ", "kait ", "marcus ", "jd ", "jack's", "jack ",
    "hivebusters dlc", "act 1", "act 2", "act 3", "act 4", "act 5",
)

TACTICS_ONLY = {"Gears Tactics"}

# Full achievement counts (including DLC/title updates) for export validation.
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


class HTTPError(Exception):
    def __init__(self, url: str, code: int, body: str = "") -> None:
        self.url = url
        self.code = code
        self.body = body
        super().__init__(f"HTTP {code} for {url}")


class RateLimitedClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.requests_made = 0
        self.last_request_at = 0.0
        self.rate_limit_remaining: int | None = None

    def _wait_for_slot(self) -> None:
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < MIN_REQUEST_INTERVAL_SEC:
            time.sleep(MIN_REQUEST_INTERVAL_SEC - elapsed)
        if (
            self.rate_limit_remaining is not None
            and self.rate_limit_remaining <= LOW_REMAINING_THRESHOLD
        ):
            print(
                f"  Rate limit low ({self.rate_limit_remaining} remaining); "
                f"sleeping {LOW_REMAINING_SLEEP_SEC}s..."
            )
            time.sleep(LOW_REMAINING_SLEEP_SEC)

    def get_json(self, path: str, *, allow_errors: set[int] | None = None) -> dict:
        allow_errors = allow_errors or set()
        for attempt in range(3):
            try:
                return self._get_json_once(path, allow_errors=allow_errors)
            except HTTPError as exc:
                if exc.code in allow_errors:
                    return {"content": {}, "error": exc.code, "errorBody": exc.body}
                if exc.code == 429 and attempt < 2:
                    wait = 60 * (attempt + 1)
                    print(f"  Rate limited (429); waiting {wait}s before retry...")
                    time.sleep(wait)
                    continue
                raise
        raise HTTPError(path, 429)

    def _get_json_once(self, path: str, *, allow_errors: set[int]) -> dict:
        self._wait_for_slot()
        url = f"{API_BASE}{path}"
        proc = subprocess.run(
            [
                "curl", "-sS", "-D", "-",
                "-H", f"X-Authorization: {self.api_key}",
                "-H", "Accept: application/json",
                url,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise HTTPError(url, 0, proc.stderr.strip())

        header_blob, _, body = proc.stdout.partition("\r\n\r\n")
        if not body and "\n\n" in proc.stdout:
            header_blob, _, body = proc.stdout.partition("\n\n")

        status_line = header_blob.splitlines()[0] if header_blob else ""
        parts = status_line.split()
        status_code = int(parts[1]) if len(parts) > 1 else 0

        for line in header_blob.splitlines():
            lower = line.lower()
            if lower.startswith("x-ratelimit-remaining:"):
                self.rate_limit_remaining = _int_header(line.split(":", 1)[1].strip())

        self.last_request_at = time.monotonic()
        self.requests_made += 1
        remaining = self.rate_limit_remaining
        if remaining is not None:
            print(f"  API request #{self.requests_made} — {remaining} calls remaining this hour")

        if status_code == 429:
            raise HTTPError(url, 429, body)
        if status_code in allow_errors:
            return {"content": {}, "error": status_code, "errorBody": body}
        if status_code >= 400:
            raise HTTPError(url, status_code, body[:500])

        return json.loads(body)


def _int_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def load_api_key() -> str:
    if key := os.environ.get("OPENXBL_API_KEY"):
        return key.strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("OPENXBL_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("Missing OPENXBL_API_KEY. Set it in .env or export OPENXBL_API_KEY=...")


def load_gears_games() -> list[dict]:
    games = json.loads(GAMES_CONFIG.read_text(encoding="utf-8"))
    eday_override = os.environ.get("OPENXBL_EDAY_TITLE_ID", "").strip()
    for game in games:
        if game.get("name") == "Gears of War: E-Day" and eday_override:
            game["titleId"] = eday_override
            game["released"] = True
    return games


def load_name_map() -> dict[str, str]:
    """firstName by gamertag from gamertag-mapping.md roster table."""
    names: dict[str, str] = {}
    if not GAMERTAG_MAPPING.exists():
        return names
    for line in GAMERTAG_MAPPING.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or line.startswith("|--") or "First name" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 3 and cells[2] in {"Active", "Retired"}:
            names[cells[1]] = cells[0]
    return names


def gamertag_from_account(payload: dict) -> tuple[str, str, int]:
    users = payload.get("content", {}).get("profileUsers", [])
    if not users:
        raise SystemExit("Could not read profile from /account response")
    xuid = users[0]["id"]
    settings = {s["id"]: s["value"] for s in users[0].get("settings", [])}
    gamertag = settings.get("Gamertag") or settings.get("ModernGamertag") or "unknown"
    gamerscore = int(settings.get("Gamerscore") or 0)
    return gamertag, xuid, gamerscore


def parse_active_roster(
    *,
    include_gamertags: set[str] | None = None,
    exclude_gamertags: set[str] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    for line in GAMERTAG_MAPPING.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or line.startswith("|--") or "First name" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        first_name, gamertag, status = cells[0], cells[1], cells[2]
        if status != "Active":
            continue
        if include_gamertags and gamertag not in include_gamertags:
            continue
        if exclude_gamertags and gamertag in exclude_gamertags:
            continue
        rows.append({"firstName": first_name, "gamertag": gamertag, "status": status})
    return rows


def gamertag_output_slug(gamertag: str) -> str:
    return gamertag.replace(" ", "_")


def achievement_output_path(gamertag: str) -> Path:
    return DATA_DIR / f"{gamertag_output_slug(gamertag)}_achievements_gearsofwar.json"


def infer_types(description: str, game_name: str) -> list[str]:
    if game_name in TACTICS_ONLY:
        return ["campaign"]

    text = f" {description.lower()} "
    types: list[str] = []
    if any(k in text for k in CAMPAIGN_KEYWORDS):
        types.append("campaign")
    if any(k in text for k in PVE_KEYWORDS):
        types.append("pve")
    if any(k in text for k in PVP_KEYWORDS):
        types.append("pvp")

    if not types:
        legacy = {
            "Gears of War", "Gears of War 2", "Gears of War: Ultimate Edition",
            "Gears of War: Reloaded",
        }
        if game_name in legacy:
            types.append("pvp" if any(x in text for x in ("win ", "round", "match")) else "campaign")
        elif game_name == "Gears of War 3":
            if "horde" in text or "beast" in text:
                types.append("pve")
            elif "multiplayer" in text or "match" in text:
                types.append("pvp")
            else:
                types.append("campaign")
        elif game_name == "Gears of War: Judgment":
            if "overrun" in text or "survival" in text:
                types.append("pve")
            elif "multiplayer" in text or "match" in text:
                types.append("pvp")
            else:
                types.append("campaign")
        else:
            types.append("campaign")

    order = {"campaign": 0, "pve": 1, "pvp": 2}
    return sorted(set(types), key=lambda t: order[t])


def gamerscore_from_modern(achievement: dict) -> int:
    for reward in achievement.get("rewards", []):
        if reward.get("type") == "Gamerscore":
            return int(reward.get("value") or 0)
    return 0


def progress_from_modern(achievement: dict) -> dict:
    state = achievement.get("progressState", "NotStarted")
    progression = achievement.get("progression") or {}
    unlocked = state == "Achieved"
    unlocked_at = progression.get("timeUnlocked")
    requirements = progression.get("requirements") or []
    progress_pct = None
    if requirements:
        current = requirements[0].get("current")
        target = requirements[0].get("target")
        if current is not None and target:
            try:
                target_val = float(target)
                if target_val > 0:
                    progress_pct = round(100 * float(current) / target_val, 1)
            except (TypeError, ValueError):
                pass
    if unlocked:
        status = "unlocked"
    elif state == "InProgress" or (progress_pct and progress_pct > 0):
        status = "in_progress"
    else:
        status = "locked"
    return {
        "status": status,
        "progressState": state,
        "unlocked": unlocked,
        "unlockedAt": unlocked_at,
        "progressPercentage": progress_pct,
    }


def normalize_modern(achievement: dict, game_name: str) -> dict:
    description = achievement.get("description") or achievement.get("lockedDescription") or ""
    return {
        "id": str(achievement.get("id", "")),
        "name": achievement.get("name", ""),
        "gamerscore": gamerscore_from_modern(achievement),
        "types": infer_types(description, game_name),
        "status": progress_from_modern(achievement),
        "description": description,
        "isSecret": bool(achievement.get("isSecret")),
        "rarity": achievement.get("rarity"),
    }


def normalize_x360(achievement: dict, game_name: str) -> dict:
    description = achievement.get("description") or achievement.get("lockedDescription") or ""
    unlocked = bool(achievement.get("unlocked"))
    return {
        "id": str(achievement.get("id", "")),
        "name": achievement.get("name", ""),
        "gamerscore": int(achievement.get("gamerscore") or 0),
        "types": infer_types(description, game_name),
        "status": {
            "status": "unlocked" if unlocked else "locked",
            "progressState": "Achieved" if unlocked else "NotStarted",
            "unlocked": unlocked,
            "unlockedAt": achievement.get("timeUnlocked") if unlocked else None,
            "progressPercentage": 100.0 if unlocked else 0.0,
        },
        "description": description,
        "isSecret": bool(achievement.get("isSecret")),
        "rarity": achievement.get("rarity"),
    }


def achievement_url(xuid: str, game: dict, continuation: str | None = None) -> str:
    title_id = game["titleId"]
    if game["endpoint"] == "x360":
        path = f"/achievements/x360/{xuid}/title/{title_id}"
    else:
        path = f"/achievements/player/{xuid}/title/{title_id}"
    if continuation:
        path = f"{path}?continuationToken={quote(continuation, safe='')}"
    return path


def title_schema_url(title_id: str, continuation: str | None = None) -> str:
    path = f"/achievements/title/{title_id}"
    if continuation:
        path = f"{path}/{quote(continuation, safe='')}"
    return path


def _batch_from_payload(payload: dict) -> list[dict]:
    content = payload.get("content", {})
    return content.get("achievements") or payload.get("achievements") or []


def _continuation_from_payload(payload: dict) -> str | None:
    content = payload.get("content", {})
    paging = content.get("pagingInfo") or payload.get("pagingInfo") or {}
    token = paging.get("continuationToken")
    return str(token) if token else None


def _fetch_paginated_batches(
    client: RateLimitedClient,
    url_builder,
    *,
    allow_errors: set[int] | None = None,
) -> tuple[list[dict], dict | None]:
    batches: list[dict] = []
    continuation: str | None = None
    seen_tokens: set[str] = set()

    while True:
        path = url_builder(continuation)
        payload = client.get_json(path, allow_errors=allow_errors or set())
        if payload.get("error"):
            return [], {
                "fetchStatus": "error",
                "httpStatus": payload["error"],
                "reason": _error_reason(payload["error"]),
            }
        batches.extend(_batch_from_payload(payload))
        continuation = _continuation_from_payload(payload)
        if not continuation or continuation in seen_tokens:
            break
        seen_tokens.add(continuation)

    return batches, None


def _merge_with_title_schema(
    player_achievements: list[dict],
    schema_batches: list[dict],
    game_name: str,
    endpoint: str,
) -> list[dict]:
    normalize = normalize_x360 if endpoint == "x360" else normalize_modern
    by_id = {a["id"]: a for a in player_achievements}
    by_name = {a["name"].casefold(): a for a in player_achievements}

    for raw in schema_batches:
        norm = normalize(raw, game_name)
        if norm["id"] in by_id or norm["name"].casefold() in by_name:
            continue
        norm["status"] = {
            "status": "locked",
            "progressState": "NotStarted",
            "unlocked": False,
            "unlockedAt": None,
            "progressPercentage": 0.0,
        }
        by_id[norm["id"]] = norm

    return sorted(by_id.values(), key=lambda a: a["name"].lower())


def fetch_game_achievements(
    client: RateLimitedClient,
    xuid: str,
    game: dict,
    *,
    allow_not_found: bool = False,
) -> tuple[list[dict], dict | None]:
    if not game.get("titleId"):
        return [], {
            "fetchStatus": "skipped",
            "reason": game.get("notes") or "Title ID not configured",
        }

    allow_errors = {404, 403} if allow_not_found else set()
    endpoint = game["endpoint"]
    game_name = game["name"]
    title_id = game["titleId"]

    player_batches, error = _fetch_paginated_batches(
        client,
        lambda cont: achievement_url(xuid, game, cont),
        allow_errors=allow_errors,
    )
    if error:
        return [], error

    if endpoint == "x360":
        achievements = [normalize_x360(a, game_name) for a in player_batches]
    else:
        achievements = [normalize_modern(a, game_name) for a in player_batches]

    schema_batches, schema_error = _fetch_paginated_batches(
        client,
        lambda cont: title_schema_url(title_id, cont),
        allow_errors={404, 403},
    )
    if not schema_error and schema_batches:
        achievements = _merge_with_title_schema(achievements, schema_batches, game_name, endpoint)

    achievements.sort(key=lambda a: a["name"].lower())
    return achievements, None


def _error_reason(status: int) -> str:
    if status == 404:
        return "Game or achievements not found (may be unreleased or privacy-restricted)"
    if status == 403:
        return "Access denied (Xbox privacy settings may block this data)"
    return f"HTTP {status}"


def summarize(achievements: list[dict], title_id: str | None = None) -> dict:
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


def lookup_player(client: RateLimitedClient, gamertag: str) -> dict:
    encoded = quote(gamertag, safe="")
    payload = client.get_json(f"/search/{encoded}", allow_errors={404})
    if payload.get("error"):
        raise HTTPError(gamertag, payload["error"], payload.get("errorBody", ""))

    people = payload.get("content", {}).get("people") or []
    if not people:
        raise HTTPError(gamertag, 404, "Gamertag not found")

    exact = next((p for p in people if p.get("gamertag", "").lower() == gamertag.lower()), people[0])
    return {
        "xuid": exact["xuid"],
        "gamertag": exact.get("gamertag") or exact.get("modernGamertag") or gamertag,
        "gamerscore": int(exact.get("gamerScore") or exact.get("gamerscore") or 0),
        "realName": exact.get("realName"),
    }


def build_player_payload(
    *,
    first_name: str | None,
    gamertag: str,
    xuid: str,
    total_gamerscore: int | None,
    games_out: list[dict],
    is_api_owner: bool = False,
) -> dict:
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
    games_out: list[dict],
    fetch_status: str,
) -> dict:
    gears_gs = sum(
        g["summary"]["gamerscoreUnlocked"] for g in games_out if g.get("fetchStatus") == "ok"
    )
    warnings = [g["fetchWarning"] for g in games_out if g.get("fetchWarning")]
    return {
        "firstName": first_name,
        "gamertag": gamertag,
        "xuid": xuid,
        "totalGamerscore": total_gamerscore,
        "gearsGamerscoreUnlocked": gears_gs,
        "isApiOwner": is_api_owner,
        "fetchStatus": fetch_status,
        "outputFile": output_file,
        "warnings": warnings,
        "games": [
            {
                "name": g["name"],
                "titleId": g.get("titleId"),
                "fetchStatus": g.get("fetchStatus"),
                "unlocked": g["summary"]["unlocked"],
                "total": g["summary"]["total"],
                "expectedTotal": g["summary"].get("expectedTotal"),
                "possiblyIncomplete": g["summary"].get("possiblyIncomplete", False),
                "gamerscoreUnlocked": g["summary"]["gamerscoreUnlocked"],
                "gamerscoreTotal": g["summary"]["gamerscoreTotal"],
            }
            for g in games_out
        ],
    }


def fetch_player_gears_data(
    client: RateLimitedClient,
    xuid: str,
    games: list[dict],
) -> list[dict]:
    games_out: list[dict] = []
    for game in games:
        print(f"    {game['name']}...")
        achievements, skip_info = fetch_game_achievements(
            client, xuid, game, allow_not_found=True
        )
        entry = {
            "titleId": game.get("titleId"),
            "name": game["name"],
            "endpoint": game.get("endpoint"),
            "released": game.get("released", True),
        }
        if skip_info:
            entry.update(skip_info)
            entry["summary"] = {
                "total": 0,
                "unlocked": 0,
                "locked": 0,
                "gamerscoreUnlocked": 0,
                "gamerscoreTotal": 0,
            }
            entry["achievements"] = []
        else:
            entry["fetchStatus"] = "ok"
            entry["summary"] = summarize(achievements, game.get("titleId"))
            entry["achievements"] = achievements
            print(f"      -> {entry['summary']['unlocked']}/{entry['summary']['total']} unlocked")
            if entry["summary"].get("possiblyIncomplete"):
                print(
                    f"      !! fetched {entry['summary']['total']}/"
                    f"{entry['summary'].get('expectedTotal', '?')} achievements "
                    f"(expected for {game['name']})"
                )
                entry["fetchWarning"] = (
                    f"Fetched {entry['summary']['total']} achievements; expected "
                    f"{entry['summary'].get('expectedTotal')} for {game['name']}."
                )
        games_out.append(entry)
    return games_out
