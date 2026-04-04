import requests
from datetime import date, timedelta


NBA_API_BASE = "https://www.balldontlie.io/api/v1"


def get_rest_days(games: list[dict]) -> dict:
    """Returns {team_id: {"rest_days": int, "is_back_to_back": bool}} for all teams playing today."""
    team_ids = set()
    for game in games:
        team_ids.add(game["home_team"]["id"])
        team_ids.add(game["visitor_team"]["id"])

    today = date.today()
    start = (today - timedelta(days=3)).isoformat()
    end = (today - timedelta(days=1)).isoformat()

    result = {}
    for team_id in team_ids:
        result[team_id] = _fetch_last_game_rest(team_id, start, end, today)
    return result


def _fetch_last_game_rest(team_id: int, start: str, end: str, today: date) -> dict:
    url = f"{NBA_API_BASE}/games"
    params = {
        "team_ids[]": team_id,
        "start_date": start,
        "end_date": end,
        "per_page": 5,
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        games = response.json().get("data", [])
        if not games:
            return {"rest_days": 3, "is_back_to_back": False}
        # dates come as "2024-01-15T00:00:00.000Z" or "2024-01-15"
        last_date_str = games[-1]["date"][:10]
        last_game_date = date.fromisoformat(last_date_str)
        rest_days = (today - last_game_date).days
        return {"rest_days": rest_days, "is_back_to_back": rest_days == 1}
    except Exception as e:
        print(f"Error fetching schedule for team {team_id}: {e}")
        return {"rest_days": 3, "is_back_to_back": False}
