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

    # First pass: compute raw per-game PPG values for each team
    raw_ppg: dict[str, float] = {}
    raw_poss: dict[str, float] = {}
    for r in records:
        abbr = (r.get("Team") or "").strip()
        if not abbr:
            continue
        games = float(r.get("Games") or 1)
        if games <= 0:
            continue
        pts   = float(r.get("Points")      or 0)
        poss  = float(r.get("Possessions") or 0)
        raw_ppg[abbr]  = pts  / games
        raw_poss[abbr] = poss / games

    if not raw_ppg:
        return {}

    # SportsData returns season stats that may be stored at a different scale
    # (e.g. per-100-possessions, per-half, or cumulative with roster multiplier).
    # Normalize so the league average maps to LEAGUE_AVG_PPG (114.0 for 2025-26).
    raw_league_avg = sum(raw_ppg.values()) / len(raw_ppg)
    scale = LEAGUE_AVG_PPG / raw_league_avg if raw_league_avg > 0 else 1.0

    # Same normalization for pace
    raw_pace_avg = sum(raw_poss.values()) / len(raw_poss) if raw_poss else 0
    pace_scale   = LEAGUE_AVG_PACE / raw_pace_avg if raw_pace_avg > 5 else 0.0

    print(f"[context] SportsData scale factor: {scale:.3f} "
          f"(raw_avg={raw_league_avg:.1f} → normalized to {LEAGUE_AVG_PPG})")

    context: dict[str, dict] = {}
    for abbr, ppg_raw in raw_ppg.items():
        ppg = round(ppg_raw * scale, 1)

        pace_est = LEAGUE_AVG_PACE
        if pace_scale > 0 and abbr in raw_poss:
            normalized_pace = raw_poss[abbr] * pace_scale
            if 85 <= normalized_pace <= 115:
                pace_est = round(normalized_pace, 1)

        # Defensive quality proxy: teams that outscore league avg tend to allow more pts
        # Linear model: opp_pts ≈ league_avg + (ppg - league_avg) * 0.40
        opp_pts = round(LEAGUE_AVG_PPG + (ppg - LEAGUE_AVG_PPG) * 0.40, 1)
        opp_pts = max(104.0, min(124.0, opp_pts))   # clamp to realistic range

        context[abbr] = {
            "ppg":        ppg,
            "opp_pts":    opp_pts,
            "pace_est":   pace_est,
            "def_rating": opp_pts,
        }

    return context
