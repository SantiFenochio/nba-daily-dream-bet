"""
fetch_player_stats.py — ESPN-based player game logs

stats.nba.com is blocked on GitHub Actions (cloud IPs rejected).
BallDontLie /v1/stats requires a paid tier (free plan = 401).

This module uses ESPN's public APIs instead, which are accessible from cloud runners.
- Player ID lookup : site.api.espn.com/apis/site/v2/sports/basketball/nba/athletes
- Per-game stats   : site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{id}/gamelog
- Injuries         : site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries (unchanged)

Exported:
    get_player_logs(player_name, last_n=20) -> list[dict]
    get_stat_value(game, market_key)        -> float | None
    parse_minutes(min_val)                  -> float
    get_injury_statuses(player_names)       -> dict[str, str | None]
"""

import os
import time
import unicodedata
import requests
from datetime import date

# ── ESPN endpoints ────────────────────────────────────────────────────────────
ESPN_SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_WEB_BASE  = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"
ESPN_HEADERS   = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

# Maps Odds API market keys to normalised stat column names (uppercase)
STAT_COL = {
    "player_points":                   "PTS",
    "player_rebounds":                 "REB",
    "player_assists":                  "AST",
    "player_threes":                   "FG3M",
    "player_steals":                   "STL",
    "player_blocks":                   "BLK",
    "player_turnovers":                "TOV",
    "player_points_rebounds_assists":  "__PRA__",
}

_ESPN_ID_CACHE: dict[str, int | None] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase + strip accents (handles Jokić → Jokic etc.)."""
    return (
        unicodedata.normalize("NFD", text.lower().strip())
        .encode("ascii", "ignore")
        .decode()
    )


def _ascii_safe(text: str) -> str:
    return unicodedata.normalize("NFD", text).encode("ascii", "replace").decode("ascii")


def _current_season_year() -> int:
    """Returns the start year of the current NBA season (2025 for 2025-26)."""
    today = date.today()
    return today.year if today.month >= 10 else today.year - 1


# ── ESPN player ID lookup ─────────────────────────────────────────────────────

def _find_espn_id(name: str) -> int | None:
    """Search ESPN for the athlete and return their integer ID."""
    if name in _ESPN_ID_CACHE:
        return _ESPN_ID_CACHE[name]

    name_norm = _normalize(name)
    parts     = name_norm.split()

    try:
        time.sleep(0.15)
        resp = requests.get(
            f"{ESPN_SITE_BASE}/athletes",
            headers=ESPN_HEADERS,
            params={"limit": 10, "search": name},
            timeout=10,
        )
        resp.raise_for_status()
        athletes = resp.json().get("athletes", [])

        # Exact normalised match
        for a in athletes:
            if _normalize(a.get("displayName", "")) == name_norm:
                pid = int(a["id"])
                _ESPN_ID_CACHE[name] = pid
                return pid

        # All name parts present
        for a in athletes:
            full = _normalize(a.get("displayName", ""))
            if all(p in full for p in parts):
                pid = int(a["id"])
                _ESPN_ID_CACHE[name] = pid
                return pid

        # Last name + first initial fallback
        if len(parts) >= 2:
            last, init = parts[-1], parts[0][0]
            for a in athletes:
                fp = _normalize(a.get("displayName", "")).split()
                if len(fp) >= 2 and fp[-1] == last and fp[0].startswith(init):
                    pid = int(a["id"])
                    _ESPN_ID_CACHE[name] = pid
                    return pid

        # First result if any
        if athletes:
            pid = int(athletes[0]["id"])
            _ESPN_ID_CACHE[name] = pid
            return pid

    except Exception as e:
        print(f"[player_stats] ESPN player lookup error for '{name}': {e}")

    _ESPN_ID_CACHE[name] = None
    return None


# ── ESPN game log parsing ─────────────────────────────────────────────────────

def _safe_int(val) -> int:
    try:
        return int(float(val or 0))
    except (ValueError, TypeError):
        return 0


def _safe_float(val) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def _parse_espn_gamelog(data: dict, last_n: int) -> list[dict]:
    """
    Parse ESPN's /gamelog response into normalised game records.

    ESPN returns two parallel lists under different keys depending on the
    API version: `labels`/`names` for column names and `events` (array of
    event objects each containing a `stats` array of values).
    """
    # ── Column names ──────────────────────────────────────────────────────────
    # ESPN /gamelog returns either {"categories": [{name: ...}, ...]} or
    # {"labels": ["PTS", "REB", ...]}
    raw_categories = data.get("categories", [])
    raw_labels     = data.get("labels", [])

    col_names: list[str] = []
    if raw_categories:
        for cat in raw_categories:
            if isinstance(cat, dict):
                col_names.append((cat.get("name") or cat.get("abbreviation") or "").lower())
            else:
                col_names.append(str(cat).lower())
    elif raw_labels:
        col_names = [str(lb).lower() for lb in raw_labels]

    def _find_val(stats: list, *candidates) -> float:
        for c in candidates:
            if c in col_names:
                idx = col_names.index(c)
                if idx < len(stats):
                    return _safe_float(
                        stats[idx].get("value") if isinstance(stats[idx], dict) else stats[idx]
                    )
        return 0.0

    # ── Events (individual games) ─────────────────────────────────────────────
    events = data.get("events", [])
    result: list[dict] = []

    for ev in events:
        stats    = ev.get("stats", [])
        team_obj = ev.get("team", {}) or {}
        opp_obj  = (
            ev.get("opponent", {}) or
            ev.get("opponentTeam", {}) or
            {}
        )

        team_abbr = team_obj.get("abbreviation", "") or ""
        opp_abbr  = opp_obj.get("abbreviation",  "") or ""

        home_away = (ev.get("homeAway") or ev.get("home_away") or "").lower()
        is_home   = home_away in ("home", "vs")
        matchup   = (
            f"{team_abbr} vs. {opp_abbr}" if is_home
            else f"{team_abbr} @ {opp_abbr}"
        )

        # Date — strip time component if ISO format
        raw_date = (
            ev.get("gameDate") or ev.get("date") or
            ev.get("game", {}).get("date", "") or ""
        )
        game_date = raw_date[:10] if "T" in raw_date else raw_date

        # Minutes — ESPN stores as decimal or "MM:SS"
        min_raw  = _find_val(stats, "min", "minutes")
        # Convert decimal minutes (e.g. 35.0) → "35:00"
        if min_raw > 0:
            mins     = int(min_raw)
            secs     = int((min_raw - mins) * 60)
            min_str  = f"{mins}:{secs:02d}"
        else:
            min_str = "0:00"

        result.append({
            "PTS":               _safe_int(_find_val(stats, "pts", "points")),
            "REB":               _safe_int(_find_val(stats, "reb", "rebounds", "totalrebounds")),
            "AST":               _safe_int(_find_val(stats, "ast", "assists")),
            "FG3M":              _safe_int(_find_val(stats, "3pm", "fg3m", "threepointersmade", "3pointsmade")),
            "STL":               _safe_int(_find_val(stats, "stl", "steals")),
            "BLK":               _safe_int(_find_val(stats, "blk", "blocks")),
            "TOV":               _safe_int(_find_val(stats, "to", "tov", "turnovers")),
            "PF":                _safe_int(_find_val(stats, "pf", "fouls", "personalfouls")),
            "MIN":               min_str,
            "TEAM_ABBREVIATION": team_abbr,
            "GAME_DATE":         game_date,
            "GAME_ID":           str(ev.get("id", "")),
            "MATCHUP":           matchup,
        })

    if not result:
        return []

    # Sort newest first (most events come oldest-first in ESPN's response)
    result.sort(key=lambda g: g["GAME_DATE"] or "", reverse=True)
    return result[:last_n]


def _fetch_espn_gamelog(espn_id: int, last_n: int) -> list[dict]:
    """Try ESPN's /gamelog endpoint (JSON).  Returns [] on any failure."""
    season = _current_season_year()
    # Try current season with explicit seasontype=2 (regular season)
    urls = [
        f"{ESPN_WEB_BASE}/athletes/{espn_id}/gamelog",
        f"{ESPN_WEB_BASE}/athletes/{espn_id}/gamelog?season={season}&seasontype=2",
    ]
    for url in urls:
        try:
            time.sleep(0.15)
            resp = requests.get(url, headers=ESPN_HEADERS, timeout=15)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            result = _parse_espn_gamelog(resp.json(), last_n)
            if result:
                return result
        except Exception as e:
            print(f"[player_stats] ESPN gamelog error (url={url}): {e}")

    return []


# ── Public API ────────────────────────────────────────────────────────────────

def get_player_logs(player_name: str, last_n: int = 20) -> list[dict]:
    """
    Fetch the last `last_n` game logs for a player via ESPN's public API.
    Returns a list of dicts sorted newest-first, or [] on failure.
    """
    espn_id = _find_espn_id(player_name)
    if espn_id is None:
        print(f"[player_stats] Not found on ESPN: {player_name}")
        return []

    result = _fetch_espn_gamelog(espn_id, last_n)
    if result:
        print(f"[player_stats] {player_name}: {len(result)} games (ESPN, id={espn_id})")
    else:
        print(f"[player_stats] No game logs for {player_name} (ESPN id={espn_id})")
    return result


# ── Stat extraction (called by analyzer) ──────────────────────────────────────

def get_stat_value(game: dict, market_key: str) -> float | None:
    """Extract the relevant stat from a game log record for a given market."""
    if market_key == "player_points_rebounds_assists":
        return (
            float(game.get("PTS") or 0) +
            float(game.get("REB") or 0) +
            float(game.get("AST") or 0)
        )
    col = STAT_COL.get(market_key)
    if col and col != "__PRA__":
        val = game.get(col)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                return None
    return None


def parse_minutes(min_val) -> float:
    """Parse MIN field (float or 'MM:SS' string → decimal minutes)."""
    if isinstance(min_val, (int, float)):
        return float(min_val)
    if isinstance(min_val, str):
        if ":" in min_val:
            parts = min_val.split(":")
            try:
                return float(parts[0]) + float(parts[1]) / 60
            except ValueError:
                pass
        try:
            return float(min_val)
        except ValueError:
            pass
    return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Injury status — ESPN public API (primary) + Rotowire (fallback)
# ══════════════════════════════════════════════════════════════════════════════

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False


def get_injury_statuses(player_names: list[str]) -> dict[str, str | None]:
    """
    Returns {player_name: injury_description_or_None}.
    Primary: ESPN public API.  Fallback: Rotowire scraper.
    """
    result: dict[str, str | None] = {n: None for n in player_names}

    try:
        print("[player_stats] Fetching injury report from ESPN API...")
        injured = _fetch_espn_injuries()
        if injured:
            _match_injuries(injured, player_names, result)
            injured_count = sum(1 for v in result.values() if v)
            print(f"[player_stats] ESPN: {injured_count} of {len(player_names)} players flagged")
            return result
        else:
            print("[player_stats] ESPN returned 0 injuries — trying Rotowire fallback...")
    except Exception as e:
        print(f"[player_stats] ESPN injury API error: {e} — trying Rotowire fallback...")

    if BS4_AVAILABLE:
        try:
            injured = _fetch_rotowire_injuries()
            if injured:
                _match_injuries(injured, player_names, result)
                injured_count = sum(1 for v in result.values() if v)
                print(f"[player_stats] Rotowire: {injured_count} of {len(player_names)} players flagged")
        except Exception as e:
            print(f"[player_stats] Rotowire also failed: {e}")

    return result


def _fetch_espn_injuries() -> dict[str, str]:
    url = f"{ESPN_SITE_BASE}/injuries"
    resp = requests.get(url, headers=ESPN_HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    injured: dict[str, str] = {}
    for group in data.get("injuries", []):
        for entry in group.get("injuries", []):
            athlete  = entry.get("athlete", {})
            raw_name = athlete.get("displayName", "").strip()
            if not raw_name:
                continue
            status = entry.get("status", "").strip()
            if not status or status.lower() == "active":
                continue
            details      = entry.get("details", {})
            injury_type  = _ascii_safe(details.get("type", ""))
            detail_desc  = _ascii_safe(details.get("detail", ""))
            long_comment = _ascii_safe(entry.get("longComment", "")[:80])
            if detail_desc:
                desc = f"{status} - {detail_desc}"
            elif injury_type:
                desc = f"{status} - {injury_type}"
            elif long_comment:
                desc = f"{status} - {long_comment}"
            else:
                desc = status
            injured[_normalize(raw_name)] = desc

    return injured


def _fetch_rotowire_injuries() -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    }
    resp = requests.get(
        "https://www.rotowire.com/basketball/injury-report.php",
        headers=headers, timeout=15,
    )
    resp.raise_for_status()
    soup    = BeautifulSoup(resp.text, "html.parser")
    injured: dict[str, str] = {}

    for sel in ["tr.injury-report__row", "tr[class*='injury-report']", "div.lineup__player"]:
        rows = soup.select(sel)
        if rows:
            break

    for row in rows:
        cells      = row.select("td")
        raw_name   = cells[0].get_text(strip=True) if cells else ""
        status_txt = cells[2].get_text(strip=True) if len(cells) > 2 else ""
        desc_txt   = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        if not raw_name or status_txt.lower() in ("active", ""):
            continue
        label = f"{status_txt} — {desc_txt}" if desc_txt else status_txt
        injured[_normalize(raw_name)] = label

    if not injured:
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells    = row.find_all("td")
                raw_name = cells[0].get_text(strip=True) if cells else ""
                row_text = " ".join(c.get_text(strip=True).lower() for c in cells)
                if any(kw in row_text for kw in ("questionable", "out", "doubtful", "day-to-day", "gtd")):
                    status_txt = next(
                        (c.get_text(strip=True) for c in cells
                         if c.get_text(strip=True).lower() in
                         ("questionable", "out", "doubtful", "day-to-day", "gtd")),
                        "Questionable",
                    )
                    if raw_name:
                        injured[_normalize(raw_name)] = status_txt

    print(f"[player_stats] Rotowire: {len(injured)} injured/questionable found")
    return injured


def _match_injuries(
    injured: dict[str, str],
    player_names: list[str],
    result: dict[str, str | None],
) -> None:
    for target in player_names:
        target_norm = _normalize(target)
        if target_norm in injured:
            result[target] = injured[target_norm]
            print(f"[player_stats] Injury: {target} -> {injured[target_norm]}")
            continue
        parts = target_norm.split()
        for inj_name, inj_status in injured.items():
            if all(p in inj_name for p in parts):
                result[target] = inj_status
                print(f"[player_stats] Injury (fuzzy): {target} -> {inj_status}")
                break
