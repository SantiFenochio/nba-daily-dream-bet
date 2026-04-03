import requests


NBA_API_BASE = "https://www.balldontlie.io/api/v1"


def get_team_stats(games: list[dict]) -> dict:
    team_ids = set()
    for game in games:
        team_ids.add(game["home_team"]["id"])
        team_ids.add(game["visitor_team"]["id"])

    print(f"[fetch_stats] Fetching season {_current_season()} averages for {len(team_ids)} teams...")
    stats = {}
    for team_id in team_ids:
        season_stats = _fetch_season_averages(team_id)
        stats[team_id] = season_stats
        pts = season_stats.get("pts", "N/A")
        fg = season_stats.get("fg_pct", "N/A")
        print(f"[fetch_stats]   • Team {team_id}: {pts} pts/g, {fg} FG%")

    return stats


def _fetch_season_averages(team_id: int) -> dict:
    url = f"{NBA_API_BASE}/season_averages"
    params = {"team_ids[]": team_id, "season": _current_season()}

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        averages = data.get("data", [])
        if averages:
            return averages[0]
        print(f"[fetch_stats]   • Team {team_id}: no averages in response.")
        return {}
    except requests.RequestException as e:
        print(f"[fetch_stats] ERROR team {team_id}: {e}")
        return {}


def _current_season() -> int:
    from datetime import date
    today = date.today()
    return today.year if today.month >= 10 else today.year - 1
