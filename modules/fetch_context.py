"""
fetch_context.py — Mejoras 5 (Pace) y 7 (DVPOP / DEF_RATING)

Fetches per-team pace and defensive rating from stats.nba.com via nba_api.
Returns a dict keyed by team abbreviation:
    {
        "MIA": {"pace": 101.3, "def_rating": 110.2, "off_rating": 115.1},
        "DEN": {"pace": 98.7,  "def_rating": 112.4, "off_rating": 118.3},
        ...
    }
"""

import time
from datetime import date

from nba_api.stats.endpoints import leaguedashteamstats
from nba_api.stats.static import teams as nba_teams


def _current_season() -> str:
    today = date.today()
    year = today.year if today.month >= 10 else today.year - 1
    return f"{year}-{str(year + 1)[-2:]}"


def _build_team_id_to_abbr() -> dict[int, str]:
    """Maps NBA team_id → abbreviation using nba_api static data."""
    return {t["id"]: t["abbreviation"] for t in nba_teams.get_teams()}


def get_team_context() -> dict[str, dict]:
    """
    Returns {team_abbr: {pace, def_rating, off_rating}} for the current season.
    Falls back to empty dict on any error (caller uses league averages as default).
    """
    season = _current_season()
    print(f"[context] Fetching team pace + DEF_RATING for season {season}...")

    try:
        time.sleep(0.7)
        stats = leaguedashteamstats.LeagueDashTeamStats(
            season=season,
            season_type_all_star="Regular Season",
            measure_type_detailed_defense="Advanced",
            per_mode_detailed="PerGame",
        )
        df = stats.get_data_frames()[0]

        if df.empty:
            print("[context] No data returned from leaguedashteamstats")
            return {}

        id_to_abbr = _build_team_id_to_abbr()

        context: dict[str, dict] = {}
        for _, row in df.iterrows():
            team_id = int(row.get("TEAM_ID", 0))
            abbr = id_to_abbr.get(team_id, "")
            if not abbr:
                continue
            context[abbr] = {
                "pace":       float(row.get("PACE", 99.5)   or 99.5),
                "def_rating": float(row.get("DEF_RATING", 113.5) or 113.5),
                "off_rating": float(row.get("OFF_RATING", 113.5) or 113.5),
            }

        print(f"[context] Team context loaded for {len(context)} teams")
        sample = list(context.items())[:4]
        for abbr, vals in sample:
            print(
                f"[context]   {abbr}: pace={vals['pace']:.1f}, "
                f"DEF={vals['def_rating']:.1f}, OFF={vals['off_rating']:.1f}"
            )
        return context

    except Exception as e:
        print(f"[context] ERROR fetching team context: {e}")
        return {}
