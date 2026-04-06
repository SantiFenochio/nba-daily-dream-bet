"""
fetch_context.py — Mejoras 5 (Pace), 7 (DEF_RATING) y DVP por stat

Fetches per-team pace, defensive rating AND stat-specific opponent allowed
averages from stats.nba.com via nba_api.

Returns a dict keyed by team abbreviation:
    {
        "MIA": {
            "pace": 101.3, "def_rating": 110.2, "off_rating": 115.1,
            # Stat-specific DVP: how many of each stat opponents score vs this team
            "opp_pts": 110.5, "opp_reb": 43.1, "opp_ast": 25.2,
            "opp_fg3m": 13.4, "opp_stl": 7.1, "opp_blk": 4.8, "opp_tov": 13.9,
        },
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
    Returns {team_abbr: {pace, def_rating, off_rating, opp_pts, opp_reb, ...}}
    for the current season.
    Falls back to empty dict on any error (caller uses league averages as default).
    """
    season = _current_season()
    print(f"[context] Fetching team pace + DEF_RATING for season {season}...")

    try:
        id_to_abbr = _build_team_id_to_abbr()

        # ── Call 1: Advanced stats (pace, DEF_RATING, OFF_RATING) ────────────
        time.sleep(0.7)
        adv = leaguedashteamstats.LeagueDashTeamStats(
            season=season,
            season_type_all_star="Regular Season",
            measure_type_detailed_defense="Advanced",
            per_mode_detailed="PerGame",
        )
        adv_df = adv.get_data_frames()[0]

        if adv_df.empty:
            print("[context] No data returned from leaguedashteamstats (Advanced)")
            return {}

        context: dict[str, dict] = {}
        for _, row in adv_df.iterrows():
            team_id = int(row.get("TEAM_ID", 0))
            abbr = id_to_abbr.get(team_id, "")
            if not abbr:
                continue
            context[abbr] = {
                "pace":       float(row.get("PACE", 99.5)   or 99.5),
                "def_rating": float(row.get("DEF_RATING", 113.5) or 113.5),
                "off_rating": float(row.get("OFF_RATING", 113.5) or 113.5),
            }

        # ── Call 2: Opponent stats (stat-specific DVP per team) ───────────────
        # These tell us how many pts/reb/ast/3s each team ALLOWS per game,
        # enabling per-market DVP instead of relying solely on DEF_RATING.
        time.sleep(0.7)
        opp = leaguedashteamstats.LeagueDashTeamStats(
            season=season,
            season_type_all_star="Regular Season",
            measure_type_detailed_defense="Opponent",
            per_mode_detailed="PerGame",
        )
        opp_df = opp.get_data_frames()[0]

        if not opp_df.empty:
            for _, row in opp_df.iterrows():
                team_id = int(row.get("TEAM_ID", 0))
                abbr = id_to_abbr.get(team_id, "")
                if not abbr or abbr not in context:
                    continue
                context[abbr].update({
                    "opp_pts":  float(row.get("OPP_PTS",  110.0) or 110.0),
                    "opp_reb":  float(row.get("OPP_REB",  43.5)  or 43.5),
                    "opp_ast":  float(row.get("OPP_AST",  25.0)  or 25.0),
                    "opp_fg3m": float(row.get("OPP_FG3M", 13.0)  or 13.0),
                    "opp_stl":  float(row.get("OPP_STL",  7.5)   or 7.5),
                    "opp_blk":  float(row.get("OPP_BLK",  4.5)   or 4.5),
                    "opp_tov":  float(row.get("OPP_TOV",  13.5)  or 13.5),
                })
            print(f"[context] Opponent stats loaded for {len(opp_df)} teams")
        else:
            print("[context] No opponent stats returned — DVP will use DEF_RATING fallback")

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
