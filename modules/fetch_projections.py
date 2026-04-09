"""
fetch_projections.py — SportsData.io player game projections

Endpoint: GET /v3/nba/projections/json/PlayerGameProjectionStatsByDate/{date}
Auth header: Ocp-Apim-Subscription-Key

Free-tier keys return scrambled player names/stats — we validate by checking
that at least 3 returned player names match our known players for today.
If validation fails the module returns {} gracefully (bot continues normally).

Projection fields used by the analyzer:
  pts, reb, ast, stl, blk, to, threes, pra, min
  (injury_status, lineup_confirmed for informational filtering)
"""

import os
import unicodedata
import requests

SPORTSDATA_BASE = "https://api.sportsdata.io/v3/nba/projections/json"

# Minimum projected minutes to consider a player's projection valid
MIN_PROJ_MINUTES = 18.0


def get_player_projections(
    date_str: str,
    known_players: list[str],
) -> dict[str, dict]:
    """
    Returns {player_name: projection_dict} for today's games.
    Returns {} if key missing, API fails, or data looks scrambled.
    """
    key = os.environ.get("SPORTSDATA_API_KEY", "")
    if not key:
        print("[projections] SPORTSDATA_API_KEY not set — skipping")
        return {}

    try:
        records = _fetch(date_str, key)
        if not records:
            print("[projections] SportsData: no records returned")
            return {}

        if not _validate(records, known_players):
            print("[projections] SportsData: player names don't match known players "
                  "(likely free-tier scrambled data) — skipping")
            return {}

        result = _parse(records)
        print(f"[projections] SportsData: {len(result)} valid player projections loaded")
        return result

    except requests.HTTPError as e:
        print(f"[projections] SportsData HTTP error: {e}")
        return {}
    except Exception as e:
        print(f"[projections] SportsData error: {e}")
        return {}


# ── Internal helpers ───────────────────────────────────────────────────────────

def _fetch(date_str: str, api_key: str) -> list[dict]:
    """Fetch projections. SportsData date format: 2026-APR-09"""
    y, m, d = date_str.split("-")
    months = ["JAN","FEB","MAR","APR","MAY","JUN",
               "JUL","AUG","SEP","OCT","NOV","DEC"]
    sd_date = f"{y}-{months[int(m)-1]}-{d}"

    resp = requests.get(
        f"{SPORTSDATA_BASE}/PlayerGameProjectionStatsByDate/{sd_date}",
        headers={"Ocp-Apim-Subscription-Key": api_key},
        timeout=12,
    )
    resp.raise_for_status()
    return resp.json()


def _normalize(name: str) -> str:
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    return name.lower().strip()


def _validate(records: list[dict], known_players: list[str]) -> bool:
    """Returns True if ≥3 of today's known players appear in the projection records."""
    if not records or not known_players:
        return False
    proj_names = {_normalize(r.get("Name", "")) for r in records}
    matches = sum(
        1 for p in known_players
        if _normalize(p) in proj_names
        or any(_normalize(p) in pn for pn in proj_names)
    )
    print(f"[projections] SportsData validation: {matches}/{len(known_players)} players matched")
    return matches >= 3


def _parse(records: list[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for r in records:
        name = r.get("Name", "")
        if not name:
            continue

        inj = (r.get("InjuryStatus") or "").lower()
        # Skip confirmed-out players (saves them being proposed as picks)
        if inj == "out":
            continue

        minutes = float(r.get("Minutes") or 0)
        pts     = float(r.get("Points")       or 0)
        reb     = float(r.get("Rebounds")     or 0)
        ast     = float(r.get("Assists")      or 0)
        stl     = float(r.get("Steals")       or 0)
        blk     = float(r.get("BlockedShots") or 0)
        to      = float(r.get("Turnovers")    or 0)
        threes  = float(r.get("ThreePointersMade") or 0)
        usg     = float(r.get("UsageRatePercentage") or 0)

        result[name] = {
            "pts":    pts,
            "reb":    reb,
            "ast":    ast,
            "stl":    stl,
            "blk":    blk,
            "to":     to,
            "threes": threes,
            "pra":    pts + reb + ast,
            "min":    minutes,
            "usage":  usg,
            "inj_status":       r.get("InjuryStatus"),
            "lineup_confirmed": r.get("LineupConfirmed"),
        }
    return result
