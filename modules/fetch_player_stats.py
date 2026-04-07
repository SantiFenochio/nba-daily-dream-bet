"""
fetch_player_stats.py — BallDontLie API replacement for nba_api / stats.nba.com

stats.nba.com is blocked on GitHub Actions (cloud provider IPs are rejected).
This module uses the BallDontLie API instead, which works from cloud runners.

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

NBA_API_BASE = "https://api.balldontlie.io/v1"

# Maps Odds API market keys to normalized stat column names (uppercase, like nba_api)
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

_PLAYER_ID_CACHE: dict[str, int | None] = {}
_TEAM_ID_TO_ABBR: dict[int, str]        = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bdl_headers() -> dict:
    api_key = os.getenv("BALLDONTLIE_API_KEY", "")
    return {"Authorization": api_key} if api_key else {}


def _current_season() -> int:
    """BallDontLie uses the start year as an integer: 2025 for the 2025-26 season."""
    today = date.today()
    return today.year if today.month >= 10 else today.year - 1


def _normalize(text: str) -> str:
    """Lowercase + strip accents (handles Jokić → Jokic etc.)."""
    return (
        unicodedata.normalize("NFD", text.lower().strip())
        .encode("ascii", "ignore")
        .decode()
    )


def _ascii_safe(text: str) -> str:
    """Replace non-ASCII chars so Windows console (cp1252) doesn't choke."""
    return unicodedata.normalize("NFD", text).encode("ascii", "replace").decode("ascii")


def _load_team_map() -> None:
    """Populate _TEAM_ID_TO_ABBR once. Used to build MATCHUP strings."""
    global _TEAM_ID_TO_ABBR
    if _TEAM_ID_TO_ABBR:
        return
    try:
        resp = requests.get(
            f"{NBA_API_BASE}/teams",
            headers=_bdl_headers(),
            params={"per_page": 35},
            timeout=10,
        )
        resp.raise_for_status()
        for t in resp.json().get("data", []):
            _TEAM_ID_TO_ABBR[t["id"]] = t["abbreviation"]
        print(f"[player_stats] Team map loaded: {len(_TEAM_ID_TO_ABBR)} teams")
    except Exception as e:
        print(f"[player_stats] Could not load team map: {e}")


# ── Player ID lookup ──────────────────────────────────────────────────────────

def find_player_id(name: str) -> int | None:
    if name in _PLAYER_ID_CACHE:
        return _PLAYER_ID_CACHE[name]

    name_norm = _normalize(name)
    parts     = name_norm.split()

    def _match(players: list[dict]) -> int | None:
        # 1. Exact normalized match
        for p in players:
            full = _normalize(f"{p['first_name']} {p['last_name']}")
            if full == name_norm:
                return p["id"]
        # 2. All name parts present (handles middle names / "Jr" suffixes)
        for p in players:
            full = _normalize(f"{p['first_name']} {p['last_name']}")
            if all(part in full for part in parts):
                return p["id"]
        # 3. Last + first initial
        if len(parts) >= 2:
            last, first_initial = parts[-1], parts[0][0]
            for p in players:
                full = _normalize(f"{p['first_name']} {p['last_name']}")
                fp = full.split()
                if len(fp) >= 2 and fp[-1] == last and fp[0].startswith(first_initial):
                    return p["id"]
        return None

    try:
        # Primary search: full name
        resp = requests.get(
            f"{NBA_API_BASE}/players",
            headers=_bdl_headers(),
            params={"search": name, "per_page": 25},
            timeout=10,
        )
        resp.raise_for_status()
        players = resp.json().get("data", [])
        pid = _match(players)
        if pid is not None:
            _PLAYER_ID_CACHE[name] = pid
            return pid

        # Fallback: search by last name only
        if len(parts) >= 2:
            resp2 = requests.get(
                f"{NBA_API_BASE}/players",
                headers=_bdl_headers(),
                params={"search": parts[-1], "per_page": 25},
                timeout=10,
            )
            resp2.raise_for_status()
            pid2 = _match(resp2.json().get("data", []))
            if pid2 is not None:
                _PLAYER_ID_CACHE[name] = pid2
                return pid2

    except Exception as e:
        print(f"[player_stats] Error finding player '{name}': {e}")

    print(f"[player_stats] Not found in BDL: {name}")
    _PLAYER_ID_CACHE[name] = None
    return None


# ── Game logs ─────────────────────────────────────────────────────────────────

def get_player_logs(player_name: str, last_n: int = 20) -> list[dict]:
    """
    Fetch the last `last_n` game logs for a player via BallDontLie.
    Returns a list of dicts sorted newest-first, or [] on failure.
    Field names are normalised to uppercase to match the analyzer's expectations
    (PTS, REB, AST, FG3M, STL, BLK, TOV, MIN, PF, TEAM_ABBREVIATION,
     GAME_DATE, GAME_ID, MATCHUP).
    """
    player_id = find_player_id(player_name)
    if not player_id:
        return []

    _load_team_map()
    season = _current_season()

    try:
        time.sleep(0.2)   # Light rate limiting
        resp = requests.get(
            f"{NBA_API_BASE}/stats",
            headers=_bdl_headers(),
            params={
                "player_ids[]": player_id,
                "seasons[]":    season,
                "per_page":     100,
            },
            timeout=15,
        )
        resp.raise_for_status()
        games = resp.json().get("data", [])
    except Exception as e:
        print(f"[player_stats] ERROR fetching logs for {player_name}: {e}")
        return []

    if not games:
        print(f"[player_stats] No logs for {player_name} (BDL season {season})")
        return []

    # Sort newest first
    games.sort(key=lambda g: g.get("game", {}).get("date", "") or "", reverse=True)
    games = games[:last_n]

    result = []
    for g in games:
        game_obj        = g.get("game", {})
        team_obj        = g.get("team", {})
        player_team_id  = team_obj.get("id", 0)
        home_team_id    = game_obj.get("home_team_id", 0)
        visitor_team_id = game_obj.get("visitor_team_id", 0)
        is_home         = player_team_id == home_team_id
        opp_team_id     = visitor_team_id if is_home else home_team_id
        team_abbr       = team_obj.get("abbreviation", "") or _TEAM_ID_TO_ABBR.get(player_team_id, "")
        opp_abbr        = _TEAM_ID_TO_ABBR.get(opp_team_id, "")

        # nba_api-compatible MATCHUP format for location/opponent splits
        matchup = f"{team_abbr} vs. {opp_abbr}" if is_home else f"{team_abbr} @ {opp_abbr}"

        result.append({
            "PTS":               int(g.get("pts",      0) or 0),
            "REB":               int(g.get("reb",      0) or 0),
            "AST":               int(g.get("ast",      0) or 0),
            "FG3M":              int(g.get("fg3m",     0) or 0),
            "STL":               int(g.get("stl",      0) or 0),
            "BLK":               int(g.get("blk",      0) or 0),
            "TOV":               int(g.get("turnover", 0) or 0),
            "MIN":               g.get("min", "0") or "0",
            "PF":                int(g.get("pf",       0) or 0),
            "TEAM_ABBREVIATION": team_abbr,
            "GAME_DATE":         game_obj.get("date", ""),
            "GAME_ID":           str(game_obj.get("id", "")),
            "MATCHUP":           matchup,
        })

    print(f"[player_stats] {player_name}: {len(result)} games (BDL, season {season})")
    return result


# ── Stat extraction (called by analyzer) ──────────────────────────────────────

def get_stat_value(game: dict, market_key: str) -> float | None:
    """Extract the relevant stat from a game log record for a given market."""
    if market_key == "player_points_rebounds_assists":
        pts = float(game.get("PTS") or 0)
        reb = float(game.get("REB") or 0)
        ast = float(game.get("AST") or 0)
        return pts + reb + ast

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
    """Parse MIN field which can be float or 'MM:SS' string."""
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
    Primary: ESPN public API (no auth, JSON).
    Fallback: Rotowire scraper.
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
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
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
            details     = entry.get("details", {})
            injury_type = _ascii_safe(details.get("type", ""))
            detail_desc = _ascii_safe(details.get("detail", ""))
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
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()

    soup    = BeautifulSoup(resp.text, "html.parser")
    injured: dict[str, str] = {}

    for sel in ["tr.injury-report__row", "tr[class*='injury-report']", "div.lineup__player"]:
        rows = soup.select(sel)
        if rows:
            break

    for row in rows:
        cells = row.select("td")
        if len(cells) < 4:
            continue
        raw_name   = cells[0].get_text(strip=True)
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
