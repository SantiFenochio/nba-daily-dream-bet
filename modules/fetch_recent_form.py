import requests
from datetime import date, timedelta


NBA_API_BASE = "https://www.balldontlie.io/api/v1"

# How many days back to look for recent games (~15-20 games per team)
RECENT_DAYS = 60
# Max games per team used for rolling stats
MAX_RECENT_GAMES = 20


def get_recent_form(games: list[dict]) -> dict:
    """Return {team_id: recent_form_dict} for all teams playing today.

    Uses a single API call for all teams, then processes locally.
    Recent form blended 60/40 with season averages in analyzer.
    """
    team_ids = set()
    for game in games:
        team_ids.add(game["home_team"]["id"])
        team_ids.add(game["visitor_team"]["id"])

    if not team_ids:
        return {}

    today = date.today()
    start = (today - timedelta(days=RECENT_DAYS)).isoformat()
    end = (today - timedelta(days=1)).isoformat()

    recent_games = _fetch_recent_games(list(team_ids), start, end)
    return _compute_form(team_ids, recent_games)


def _fetch_recent_games(team_ids: list[int], start: str, end: str) -> list[dict]:
    """Single API call: fetch all finished games for all today's teams in the date window."""
    url = f"{NBA_API_BASE}/games"
    params = [("start_date", start), ("end_date", end), ("per_page", 100)]
    for tid in team_ids:
        params.append(("team_ids[]", tid))

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        games = response.json().get("data", [])
        # Only keep finished games (have scores)
        return [g for g in games if g.get("home_team_score") and g.get("visitor_team_score")]
    except Exception as e:
        print(f"Error fetching recent games: {e}")
        return []


def _compute_form(team_ids: set[int], recent_games: list[dict]) -> dict:
    """For each team, take the last MAX_RECENT_GAMES finished games and compute rolling stats."""
    # Group games per team, sorted by date ascending
    team_games: dict[int, list[dict]] = {tid: [] for tid in team_ids}

    for game in recent_games:
        home_id = game["home_team"]["id"]
        visitor_id = game["visitor_team"]["id"]
        game_date = game["date"][:10]

        if home_id in team_ids:
            team_games[home_id].append({
                "date": game_date,
                "pts_for": game["home_team_score"],
                "pts_against": game["visitor_team_score"],
                "won": game["home_team_score"] > game["visitor_team_score"],
            })
        if visitor_id in team_ids:
            team_games[visitor_id].append({
                "date": game_date,
                "pts_for": game["visitor_team_score"],
                "pts_against": game["home_team_score"],
                "won": game["visitor_team_score"] > game["home_team_score"],
            })

    result = {}
    for tid in team_ids:
        games = sorted(team_games[tid], key=lambda g: g["date"])
        games = games[-MAX_RECENT_GAMES:]  # keep only the most recent N
        result[tid] = _stats_from_games(games)

    return result


def _stats_from_games(games: list[dict]) -> dict:
    """Compute rolling stats from a list of {pts_for, pts_against, won} dicts."""
    if not games:
        return {"recent_pts": 0.0, "recent_pts_allowed": 0.0, "recent_margin": 0.0,
                "recent_win_pct": 0.5, "games_count": 0, "streak": 0}

    pts_for = [g["pts_for"] for g in games]
    pts_against = [g["pts_against"] for g in games]
    wins = [g["won"] for g in games]

    avg_pts = sum(pts_for) / len(pts_for)
    avg_allowed = sum(pts_against) / len(pts_against)
    win_pct = sum(wins) / len(wins)

    # Streak: positive = win streak, negative = losing streak
    streak = 0
    for won in reversed(wins):
        if streak == 0:
            streak = 1 if won else -1
        elif (streak > 0 and won) or (streak < 0 and not won):
            streak += (1 if won else -1)
        else:
            break

    return {
        "recent_pts": avg_pts,
        "recent_pts_allowed": avg_allowed,
        "recent_margin": avg_pts - avg_allowed,
        "recent_win_pct": win_pct,
        "games_count": len(games),
        "streak": streak,
    }
