"""
fetch_context.py — Team pace + DEF context from ESPN public API

Uses ESPN's byteam statistics endpoint (no auth, accessible from cloud IPs).
Falls back to {} on any error — analyzer uses league averages as fallback.
"""

import requests
from datetime import datetime

ESPN_BYTEAM_URL = (
    "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"
    "/statistics/byteam"
)
ESPN_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

LEAGUE_AVG_PPG     = 114.0
LEAGUE_AVG_OPP_PTS = 114.0
LEAGUE_AVG_PACE    = 99.5


def get_team_context() -> dict[str, dict]:
    """
    Returns {abbr: {ppg, opp_pts, pace_est, def_rating}} for all NBA teams.
    Falls back to {} (analyzer uses league averages) on any error.
    """
    try:
        ctx = _fetch_espn_byteam()
        if ctx:
            print(f"[context] ESPN byteam loaded: {len(ctx)} teams "
                  f"(avg opp_pts: {sum(v['opp_pts'] for v in ctx.values())/len(ctx):.1f})")
            return ctx
        print("[context] ESPN byteam: no data parsed — using league averages")
    except Exception as e:
        print(f"[context] ESPN byteam error: {e} — using league averages")
    return {}


def _fetch_espn_byteam() -> dict[str, dict]:
    # ESPN uses the *start* year of the season (2025 for 2025-26)
    season = datetime.now().year
    if datetime.now().month < 9:   # before September → still current season started last year
        season -= 1

    resp = requests.get(
        ESPN_BYTEAM_URL,
        params={
            "region": "us", "lang": "en", "contentorigin": "espn",
            "isqualified": "true", "page": "1", "limit": "50",
            "type": "0", "sort": "offensive.avgPoints:desc",
            "season": str(season), "seasontype": "2",
        },
        headers=ESPN_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"[context] ESPN byteam HTTP 200 (season={season})")
    return _parse_byteam(data)


def _parse_byteam(data: dict) -> dict[str, dict]:
    categories = data.get("categories", [])
    if not categories:
        print("[context] ESPN byteam: no 'categories' in response")
        return {}

    # Log categories + sample labels so we can diagnose structure
    cat_names = [c.get("name", c.get("type", "?")) for c in categories]
    print(f"[context] ESPN byteam categories: {cat_names}")
    for i, cat in enumerate(categories[:3]):
        lbl_sample = (cat.get("labels") or cat.get("names") or [])[:10]
        print(f"[context]   cat[{i}] labels[:10]: {lbl_sample}")

    # ESPN byteam structure:
    #   categories[0] = own-team general (GP, GS, MIN, FGM, FGA, FG%, 3PM, 3PA, 3P%, FTM, FTA, FT%, OR, DR, REB, AST, TO, STL, BLK, PF, PTS)
    #   categories[1] = opponent general (same schema, opponent stats)
    #   categories[2] = own defensive
    #   categories[3] = opponent defensive  ...etc.
    # PTS is typically the LAST label in the general category (index 20 of 21)

    def _find_idx(cat: dict, *candidates: str) -> int | None:
        # Try labels first, then names, then displayNames
        for field in ("labels", "names", "displayNames"):
            arr = cat.get(field) or []
            if not arr:
                continue
            # Exact match (case-insensitive)
            for cand in candidates:
                for i, lbl in enumerate(arr):
                    if str(lbl).upper() == cand.upper():
                        return i
            # Partial match
            for cand in candidates:
                for i, lbl in enumerate(arr):
                    if cand.lower() in str(lbl).lower():
                        return i
        return None

    # Own team (first general category)
    own_cat = categories[0] if categories else None
    # Opponent (second general category, or first one with "opponent" in name)
    opp_cat = None
    for cat in categories:
        name = (cat.get("name") or "").lower()
        if "opponent" in name or "opp" in name:
            opp_cat = cat
            break
    if opp_cat is None and len(categories) >= 2:
        opp_cat = categories[1]

    if not own_cat:
        print("[context] ESPN byteam: no categories found")
        return {}

    own_pts_idx = _find_idx(own_cat, "PTS", "pts", "points", "avgPoints")
    opp_pts_idx = _find_idx(opp_cat, "PTS", "pts", "points", "avgPoints") if opp_cat else None
    own_fga_idx = _find_idx(own_cat, "FGA", "fga", "fieldGoalsAttempted")
    own_or_idx  = _find_idx(own_cat, "OR", "OREB", "offReb", "offensiveRebounds", "orb")
    own_to_idx  = _find_idx(own_cat, "TO", "TOV", "turnovers", "tov")
    own_fta_idx = _find_idx(own_cat, "FTA", "fta", "freeThrowsAttempted")

    print(f"[context] Label indices — own_pts:{own_pts_idx} opp_pts:{opp_pts_idx} "
          f"fga:{own_fga_idx} or:{own_or_idx} to:{own_to_idx} fta:{own_fta_idx}")

    # If we still can't find PTS, try the last label (it's usually PTS in box score order)
    if own_pts_idx is None:
        labels = own_cat.get("labels") or own_cat.get("names") or []
        if labels:
            own_pts_idx = len(labels) - 1
            print(f"[context] Falling back to last label for own_pts: idx={own_pts_idx} ({labels[-1]})")
    if opp_pts_idx is None and opp_cat:
        labels = opp_cat.get("labels") or opp_cat.get("names") or []
        if labels:
            opp_pts_idx = len(labels) - 1
            print(f"[context] Falling back to last label for opp_pts: idx={opp_pts_idx} ({labels[-1]})")

    # Build opponent lookup: teamId → values list
    opp_by_id: dict[str, list] = {}
    if opp_cat:
        for t in opp_cat.get("teams", []):
            tid = str(t.get("teamId") or t.get("id", ""))
            if tid:
                opp_by_id[tid] = t.get("values", [])

    context: dict[str, dict] = {}
    for team_data in own_cat.get("teams", []):
        team_info = team_data.get("team", {})
        abbr = team_info.get("abbreviation", "")
        if not abbr:
            continue

        vals = team_data.get("values", [])

        def safe(idx, fallback=None):
            if idx is not None and 0 <= idx < len(vals):
                try:
                    return float(vals[idx])
                except (TypeError, ValueError):
                    pass
            return fallback

        ppg     = safe(own_pts_idx, LEAGUE_AVG_PPG)
        opp_pts = LEAGUE_AVG_OPP_PTS

        tid = str(team_data.get("teamId") or team_data.get("id", ""))
        if tid and tid in opp_by_id and opp_pts_idx is not None:
            opp_vals = opp_by_id[tid]
            if opp_pts_idx < len(opp_vals):
                try:
                    opp_pts = float(opp_vals[opp_pts_idx])
                except (TypeError, ValueError):
                    pass

        # Pace estimate: possessions ≈ FGA − OR + TO + 0.44 × FTA
        pace_est = LEAGUE_AVG_PACE
        fga = safe(own_fga_idx)
        oreb = safe(own_or_idx)
        to   = safe(own_to_idx)
        fta  = safe(own_fta_idx)
        if all(v is not None for v in [fga, oreb, to, fta]):
            est = fga - oreb + to + 0.44 * fta
            if 85 <= est <= 115:   # sanity bounds
                pace_est = round(est, 1)

        context[abbr] = {
            "ppg":        round(ppg, 1),
            "opp_pts":    round(opp_pts, 1),
            "pace_est":   pace_est,
            "def_rating": round(opp_pts, 1),
        }

    return context
