import os
import time
import unicodedata
import requests
from datetime import date

from nba_api.stats.static import players as nba_players
from nba_api.stats.endpoints import playergamelogs

# Maps Odds API market keys to nba_api DataFrame column names
STAT_COL = {
    "player_points": "PTS",
    "player_rebounds": "REB",
    "player_assists": "AST",
    "player_threes": "FG3M",
    "player_steals": "STL",
    "player_blocks": "BLK",
    "player_turnovers": "TOV",
    "player_points_rebounds_assists": "__PRA__",
}

_PLAYER_ID_CACHE: dict[str, int | None] = {}
_ALL_NBA_PLAYERS: list[dict] = []


def _load_nba_players() -> list[dict]:
    global _ALL_NBA_PLAYERS
    if not _ALL_NBA_PLAYERS:
        _ALL_NBA_PLAYERS = nba_players.get_players()
    return _ALL_NBA_PLAYERS


def _current_season() -> str:
    today = date.today()
    year = today.year if today.month >= 10 else today.year - 1
    return f"{year}-{str(year + 1)[-2:]}"


def _normalize(text: str) -> str:
    """Lowercase + strip accents for fuzzy matching (handles Jokić → Jokic etc.)."""
    return unicodedata.normalize("NFD", text.lower().strip()).encode("ascii", "ignore").decode()


def find_player_id(name: str) -> int | None:
    if name in _PLAYER_ID_CACHE:
        return _PLAYER_ID_CACHE[name]

    all_p = _load_nba_players()
    name_norm = _normalize(name)

    # 1. Exact normalized match
    for p in all_p:
        if _normalize(p["full_name"]) == name_norm:
            _PLAYER_ID_CACHE[name] = p["id"]
            return p["id"]

    # 2. All parts present (handles middle names / suffixes)
    parts = name_norm.split()
    for p in all_p:
        full_norm = _normalize(p["full_name"])
        if all(part in full_norm for part in parts):
            _PLAYER_ID_CACHE[name] = p["id"]
            return p["id"]

    # 3. Last name + first initial (e.g. "P. Reed" style mismatches)
    if len(parts) >= 2:
        last = parts[-1]
        first_initial = parts[0][0]
        for p in all_p:
            full_norm = _normalize(p["full_name"])
            full_parts = full_norm.split()
            if len(full_parts) >= 2 and full_parts[-1] == last and full_parts[0].startswith(first_initial):
                _PLAYER_ID_CACHE[name] = p["id"]
                return p["id"]

    _PLAYER_ID_CACHE[name] = None
    return None


def get_player_logs(player_name: str, last_n: int = 20) -> list[dict]:
    """
    Fetch the last `last_n` game logs for a player via nba_api (stats.nba.com).
    Returns a list of dicts sorted newest-first, or [] on failure.
    """
    player_id = find_player_id(player_name)
    if not player_id:
        print(f"[player_stats] Not found in NBA DB: {player_name}")
        return []

    season = _current_season()
    try:
        time.sleep(0.7)  # Respect stats.nba.com rate limit
        logs = playergamelogs.PlayerGameLogs(
            player_id_nullable=str(player_id),
            season_nullable=season,
            season_type_nullable="Regular Season",
            last_n_games_nullable=last_n,
        )
        df = logs.get_data_frames()[0]
        if df.empty:
            print(f"[player_stats] No logs for {player_name} (season {season})")
            return []

        df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)
        records = df.to_dict("records")
        print(f"[player_stats] {player_name}: {len(records)} games fetched")
        return records
    except Exception as e:
        print(f"[player_stats] ERROR fetching {player_name}: {e}")
        return []


def get_stat_value(game: dict, market_key: str) -> float | None:
    """Extract the relevant stat from a game log record for a given market."""
    if market_key == "player_points_rebounds_assists":
        pts = float(game.get("PTS") or 0)
        reb = float(game.get("REB") or 0)
        ast = float(game.get("AST") or 0)
        return pts + reb + ast

    col = STAT_COL.get(market_key)
    if col and col != "__PRA__":
        val = game.get(col)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                return None
    return None


def parse_minutes(min_val) -> float:
    """Parse nba_api MIN field which can be float or 'MM:SS' string."""
    if isinstance(min_val, (int, float)):
        return float(min_val)
    if isinstance(min_val, str):
        if ":" in min_val:
            parts = min_val.split(":")
            try:
                return float(parts[0]) + float(parts[1]) / 60
            except ValueError:
                pass
        try:
            return float(min_val)
        except ValueError:
            pass
    return 0.0


def get_injury_statuses(player_names: list[str]) -> dict[str, str | None]:
    """
    Check injury status for a list of players via Tank01 RapidAPI.
    Makes 1 bulk call to get all players, then individual calls only for matched ones.
    Returns {player_name: injury_description_or_None}.
    """
    result: dict[str, str | None] = {n: None for n in player_names}
    api_key = os.getenv("RAPIDAPI_KEY")
    if not api_key:
        return result

    headers = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": "tank01-fantasy-stats.p.rapidapi.com",
    }

    try:
        print("[player_stats] Fetching Tank01 player list...")
        resp = requests.get(
            "https://tank01-fantasy-stats.p.rapidapi.com/getNBAPlayerList",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        tank_players = resp.json().get("body", [])
        print(f"[player_stats] Tank01: {len(tank_players)} players in list")

        # Build lookup: target_name → tank player_id + injury info (if in list)
        matched: dict[str, dict] = {}
        for tp in tank_players:
            long_name = (tp.get("longName") or "").lower()
            for target in player_names:
                if target.lower() in long_name or long_name in target.lower():
                    matched[target] = tp
                    break

        for name, tp in matched.items():
            injury = tp.get("injury") or {}
            if isinstance(injury, dict) and injury:
                status = injury.get("injDesc") or injury.get("injStatus") or ""
                if status and status.lower() not in ("active", "", "healthy"):
                    result[name] = status
                    print(f"[player_stats] {name} → {status}")

    except Exception as e:
        print(f"[player_stats] Tank01 error: {e}")

    return result
