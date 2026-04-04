import requests
from datetime import date, timedelta


NBA_API_BASE = "https://www.balldontlie.io/api/v1"


def get_rest_days(games: list[dict]) -> dict:
    """Returns {team_id: {"rest_days": int, "is_back_to_back": bool}} for all teams playing today.
    Uses a single API call with all team IDs instead of one call per team.
    """
    team_ids = set()
    for game in games:
        team_ids.add(game["home_team"]["id"])
        team_ids.add(game["visitor_team"]["id"])

    if not team_ids:
        return {}

    today = date.today()
    start = (today - timedelta(days=3)).isoformat()
    end = (today - timedelta(days=1)).isoformat()

    recent_games = _fetch_recent_games_all_teams(list(team_ids), start, end)
    return _compute_rest_per_team(team_ids, recent_games, today)


def _fetch_recent_games_all_teams(team_ids: list[int], start: str, end: str) -> list[dict]:
    """Single API call returning all recent games for every team involved today."""
    url = f"{NBA_API_BASE}/games"
    # Ball Don't Lie supports multiple team_ids[] in one request
    params = [("start_date", start), ("end_date", end), ("per_page", 100)]
    for tid in team_ids:
        params.append(("team_ids[]", tid))

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json().get("data", [])
    except Exception as e:
        print(f"Error fetching recent schedule: {e}")
        return []


def _compute_rest_per_team(
    team_ids: set[int], recent_games: list[dict], today: date
) -> dict:
    """From the fetched games, find the most recent game date per team."""
    last_played: dict[int, date] = {}

    for game in recent_games:
        game_date = date.fromisoformat(game["date"][:10])
        for side in ("home_team", "visitor_team"):
            tid = game[side]["id"]
            if tid in team_ids:
                if tid not in last_played or game_date > last_played[tid]:
                    last_played[tid] = game_date

    result = {}
    for tid in team_ids:
        if tid in last_played:
            rest_days = (today - last_played[tid]).days
        else:
            rest_days = 3  # no recent game found → treat as well-rested
        result[tid] = {"rest_days": rest_days, "is_back_to_back": rest_days == 1}

    return result
