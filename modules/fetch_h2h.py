import requests
from datetime import date


NBA_API_BASE = "https://www.balldontlie.io/api/v1"

# How many seasons back to look for H2H history (current + 1 previous)
H2H_SEASONS = 2


def get_h2h_records(games: list[dict]) -> dict:
    """Return {game_id: h2h_dict} for each matchup today.

    Uses a single API call for all team IDs across recent seasons,
    then filters per matchup pair locally.

    h2h_dict keys:
        h2h_games       int   — total H2H games found in window
        home_wins       int   — how many times home team won H2H
        visitor_wins    int   — how many times visitor team won H2H
        avg_margin_home float — avg point diff (home - visitor), + = home dominated
    """
    if not games:
        return {}

    team_ids = set()
    for game in games:
        team_ids.add(game["home_team"]["id"])
        team_ids.add(game["visitor_team"]["id"])

    seasons = _recent_seasons()
    all_h2h_games = _fetch_games_bulk(list(team_ids), seasons)

    result = {}
    for game in games:
        home_id = game["home_team"]["id"]
        visitor_id = game["visitor_team"]["id"]
        matchup_pair = {home_id, visitor_id}

        # Filter only games that were between these exact two teams
        h2h = [
            g for g in all_h2h_games
            if {g["home_team"]["id"], g["visitor_team"]["id"]} == matchup_pair
        ]

        result[game["id"]] = _compute_h2h(home_id, visitor_id, h2h)

    return result


def _recent_seasons() -> list[int]:
    """Return current season + H2H_SEASONS-1 previous seasons."""
    today = date.today()
    current = today.year if today.month >= 10 else today.year - 1
    return [current - i for i in range(H2H_SEASONS)]


def _fetch_games_bulk(team_ids: list[int], seasons: list[int]) -> list[dict]:
    """Single API call: all finished games involving any of today's teams in recent seasons."""
    url = f"{NBA_API_BASE}/games"
    params = [("per_page", 100), ("postseason", "false")]
    for tid in team_ids:
        params.append(("team_ids[]", tid))
    for season in seasons:
        params.append(("seasons[]", season))

    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        games = response.json().get("data", [])
        # Only games with final scores
        return [g for g in games if g.get("home_team_score") and g.get("visitor_team_score")]
    except Exception as e:
        print(f"Error fetching H2H games: {e}")
        return []


def _compute_h2h(home_id: int, visitor_id: int, h2h_games: list[dict]) -> dict:
    """Compute H2H stats from a list of games between exactly these two teams."""
    if not h2h_games:
        return {"h2h_games": 0, "home_wins": 0, "visitor_wins": 0, "avg_margin_home": 0.0}

    home_wins = 0
    visitor_wins = 0
    margins = []

    for game in h2h_games:
        if game["home_team"]["id"] == home_id:
            # Today's home team was also home in this H2H game
            margin = game["home_team_score"] - game["visitor_team_score"]
        else:
            # Today's home team was the visitor in this H2H game
            margin = game["visitor_team_score"] - game["home_team_score"]

        margins.append(margin)
        if margin > 0:
            home_wins += 1
        else:
            visitor_wins += 1

    avg_margin = sum(margins) / len(margins)

    return {
        "h2h_games": len(h2h_games),
        "home_wins": home_wins,
        "visitor_wins": visitor_wins,
        "avg_margin_home": avg_margin,  # positive = today's home team dominated H2H
    }
