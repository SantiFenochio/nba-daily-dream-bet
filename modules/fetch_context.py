"""
fetch_context.py — Team pace + DEF_RATING context

stats.nba.com (used by nba_api) is blocked on GitHub Actions cloud IPs.
This module returns {} immediately so the analyzer falls back to league averages
(pace=99.5, DEF_RATING=113.5) which is acceptable for pick generation.

TODO: Replace with an alternative source (ESPN hidden API or BDL-derived stats)
      when stat-specific DVP adjustments become critical.
"""


def get_team_context() -> dict[str, dict]:
    """
    Returns per-team context dict {abbr: {pace, def_rating, opp_pts, ...}}.
    Currently returns {} — analyzer uses league averages as fallback.
    stats.nba.com is inaccessible from GitHub Actions (cloud IP block).
    """
    print("[context] Skipping team context (stats.nba.com blocked on cloud runners) — using league averages")
    return {}
