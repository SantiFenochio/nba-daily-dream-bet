"""
fetch_context.py — Team pace + DEF context

Primary source: SportsData.io TeamSeasonStats (already confirmed working/paid)
Fallback: empty dict → analyzer uses league averages.

SportsData fields used:
  Team        → abbreviation (e.g. "BOS")
  Points      → team PPG (offensive strength)
  Possessions → pace proxy (possessions per game)
"""

import os
import requests

SPORTSDATA_BASE  = "https://api.sportsdata.io/v3/nba/scores/json"
LEAGUE_AVG_PPG   = 114.0
LEAGUE_AVG_PACE  = 99.5


def get_team_context() -> dict[str, dict]:
    """
    Returns {abbr: {ppg, opp_pts, pace_est, def_rating}} for NBA teams.
    Falls back to {} on error — analyzer uses league averages.
    """
    key = os.environ.get("SPORTSDATA_API_KEY", "")
    if not key:
        print("[context] SPORTSDATA_API_KEY not set — using league averages")
        return {}

    try:
        ctx = _fetch_sportsdata_team_stats(key)
        if ctx:
            sample = sorted(ctx.items())[:3]
            sample_str = ", ".join(f"{a}: {v['ppg']}pts pace={v['pace_est']}" for a, v in sample)
            print(f"[context] SportsData TeamSeasonStats: {len(ctx)} teams "
                  f"(sample: {sample_str})")
            return ctx
        print("[context] SportsData TeamSeasonStats: no data — using league averages")
    except Exception as e:
        print(f"[context] SportsData TeamSeasonStats error: {e} — using league averages")
    return {}


def _fetch_sportsdata_team_stats(api_key: str) -> dict[str, dict]:
    """Fetch all team season stats from SportsData.io for the current season."""
    resp = requests.get(
        f"{SPORTSDATA_BASE}/TeamSeasonStats/2026",
        headers={"Ocp-Apim-Subscription-Key": api_key},
        timeout=12,
    )
    resp.raise_for_status()
    records = resp.json()

    if not isinstance(records, list) or not records:
        return {}

    # Validate: check that team abbreviations look real (not scrambled)
    # Real abbrs are 2-3 uppercase letters; scrambled data has fake names
    abbrs = [r.get("Team", "") for r in records if r.get("Team")]
    real = sum(1 for a in abbrs if a.isupper() and 2 <= len(a) <= 3)
    if real < 20:
        print(f"[context] SportsData: abbreviations look scrambled ({real}/30 valid) — skipping")
        return {}

    # Compute league average PPG to derive a defensive quality proxy for each team
    ppg_values = [float(r.get("Points") or 0) for r in records if r.get("Points")]
    league_avg = sum(ppg_values) / len(ppg_values) if ppg_values else LEAGUE_AVG_PPG

    context: dict[str, dict] = {}
    for r in records:
        abbr = (r.get("Team") or "").strip()
        if not abbr:
            continue

        games = float(r.get("Games") or 1)
        # SportsData returns season TOTALS — divide by games to get per-game averages
        pts_total   = float(r.get("Points")      or 0)
        poss_total  = float(r.get("Possessions") or 0)
        ppg         = pts_total  / games if games > 0 else LEAGUE_AVG_PPG
        pace_raw    = poss_total / games if games > 0 else 0
        pace_est    = pace_raw if 85 <= pace_raw <= 115 else LEAGUE_AVG_PACE

        # Defensive quality proxy:
        # Teams that score more tend to play in higher-scoring games → allow more pts.
        # Simple linear relationship: opp_pts ≈ league_avg + (ppg - league_avg) * 0.4
        # This is a rough but reasonable estimate when direct opp_pts isn't available.
        opp_pts = round(league_avg + (ppg - league_avg) * 0.40, 1)
        # Clamp to realistic range [104, 124]
        opp_pts = max(104.0, min(124.0, opp_pts))

        context[abbr] = {
            "ppg":        round(ppg, 1),
            "opp_pts":    opp_pts,
            "pace_est":   round(pace_est, 1),
            "def_rating": opp_pts,
        }

    return context
