"""
fetch_player_stats.py — SportsData.io batch game-log loader

WHY THIS EXISTS:
  - stats.nba.com      → blocks all GitHub Actions (cloud) IPs completely
  - BallDontLie stats  → requires paid tier (free tier = 401 Unauthorized)
  - ESPN athlete API   → /athletes?search= endpoint returns 404

SOLUTION:
  SportsData.io /GameStatsByDate/{date} fetches ALL players' stats for each game
  date in one call, so we need only ~25-30 API calls total per run (instead of
  100+ individual calls). The free trial key gives 1 000 requests — ~14 days
  of daily runs at ~30 calls each.

  SPORTSDATA_API_KEY env var must be set (from GitHub secret SPORTSDATA_KEY).

Exports:
    get_player_logs(name, last_n=20)  → list[dict]
    get_stat_value(game, market_key) → float | None
    parse_minutes(min_val)           → float
    get_injury_statuses(names)       → dict[str, str | None]
"""

import os
import time
import unicodedata
import requests
from datetime import date, timedelta

# ── SportsData.io config ──────────────────────────────────────────────────────
SD_BASE = "https://api.sportsdata.io/v3/nba"

# ── Stat column map (uppercase aliases, same as legacy nba_api format) ────────
STAT_COL = {
    "player_points":                  "PTS",
    "player_rebounds":                "REB",
    "player_assists":                 "AST",
    "player_threes":                  "FG3M",
    "player_steals":                  "STL",
    "player_blocks":                  "BLK",
    "player_turnovers":               "TOV",
    "player_points_rebounds_assists": "__PRA__",
}

# ── Module-level game-log cache built once per process ────────────────────────
# Keys: normalised player name  Values: list of game records (newest-first)
_LOG_CACHE: dict[str, list[dict]] = {}
_CACHE_READY = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    return (
        unicodedata.normalize("NFD", text.lower().strip())
        .encode("ascii", "ignore")
        .decode()
    )


def _ascii_safe(text: str) -> str:
    return unicodedata.normalize("NFD", text).encode("ascii", "replace").decode("ascii")


def _sd_headers() -> dict:
    key = os.getenv("SPORTSDATA_API_KEY", "")
    if not key:
        return {}
    return {"Ocp-Apim-Subscription-Key": key}


def _sd_date(d: date) -> str:
    """SportsData.io NBA date format: '2026-APR-06'"""
    return d.strftime("%Y-") + d.strftime("%b").upper() + d.strftime("-%d")


# ── Cache loader (called once, fetches last N game days in bulk) ─────────────

def _load_cache(max_game_days: int = 28) -> None:
    """
    Fetch /GameStatsByDate for each of the last max_game_days days that had
    NBA games and populate _LOG_CACHE.  Runs once per process.
    """
    global _LOG_CACHE, _CACHE_READY

    if _CACHE_READY:
        return

    key = os.getenv("SPORTSDATA_API_KEY", "")
    if not key:
        print("[player_stats] SPORTSDATA_API_KEY not set — no game logs available")
        _CACHE_READY = True
        return

    print(f"[player_stats] Loading SportsData.io cache (up to {max_game_days} game days)...")

    today       = date.today()
    game_days   = 0
    tmp: dict[str, list[dict]] = {}

    for days_back in range(1, 55):  # search up to 55 calendar days back
        if game_days >= max_game_days:
            break

        check = today - timedelta(days=days_back)
        date_str = _sd_date(check)

        try:
            time.sleep(0.15)
            resp = requests.get(
                f"{SD_BASE}/stats/json/GameStatsByDate/{date_str}",
                headers=_sd_headers(),
                timeout=12,
            )

            # 404 / 204 = no games on this date
            if resp.status_code in (404, 204):
                continue
            # 401 / 403 = auth problem — stop immediately
            if resp.status_code in (401, 403):
                print(f"[player_stats] SportsData.io auth error {resp.status_code} — check SPORTSDATA_API_KEY")
                break

            resp.raise_for_status()
            stats = resp.json()
            if not isinstance(stats, list) or not stats:
                continue

            game_days += 1
            for s in stats:
                raw_name = (s.get("Name") or "").strip()
                if not raw_name:
                    continue
                rec = _sd_to_record(s, check)
                # Skip DNP (0 minutes)
                if parse_minutes(rec["MIN"]) <= 0:
                    continue
                tmp.setdefault(_normalize(raw_name), []).append(rec)

        except requests.exceptions.ConnectionError:
            time.sleep(1)
            continue
        except Exception:
            continue

    # Sort each player newest-first
    for k in tmp:
        tmp[k].sort(key=lambda g: g.get("GAME_DATE", ""), reverse=True)

    _LOG_CACHE  = tmp
    _CACHE_READY = True

    total_players = len(_LOG_CACHE)
    print(f"[player_stats] Cache ready: {game_days} game days loaded, {total_players} players cached")


def _sd_to_record(s: dict, game_date: date) -> dict:
    """Convert one SportsData.io PlayerGameStats row → internal game record."""
    team_abbr = (s.get("Team")     or "").upper()
    opp_abbr  = (s.get("Opponent") or "").upper()
    home_away = (s.get("HomeOrAway") or "").upper()
    is_home   = home_away == "HOME"
    matchup   = (
        f"{team_abbr} vs. {opp_abbr}" if is_home
        else f"{team_abbr} @ {opp_abbr}"
    )

    # Minutes can arrive as "35:20" string or decimal float
    min_raw = s.get("Minutes") or "0"

    return {
        "PTS":               float(s.get("Points")            or 0),
        "REB":               float(s.get("Rebounds")          or 0),
        "AST":               float(s.get("Assists")           or 0),
        "FG3M":              float(s.get("ThreePointersMade") or 0),
        "STL":               float(s.get("Steals")            or 0),
        "BLK":               float(s.get("BlockedShots")      or 0),
        "TOV":               float(s.get("TurnOvers")         or 0),
        "PF":                float(s.get("PersonalFouls")     or 0),
        "MIN":               str(min_raw),
        "TEAM_ABBREVIATION": team_abbr,
        "GAME_DATE":         game_date.isoformat(),
        "GAME_ID":           str(s.get("GameID") or s.get("StatID") or ""),
        "MATCHUP":           matchup,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def get_player_logs(player_name: str, last_n: int = 20) -> list[dict]:
    """
    Return the last `last_n` game records for `player_name` from the
    SportsData.io cache.  Cache is built on first call.
    """
    _load_cache()

    if not _LOG_CACHE:
        return []

    name_norm = _normalize(player_name)
    parts     = name_norm.split()

    # 1. Exact normalised match
    if name_norm in _LOG_CACHE:
        logs = _LOG_CACHE[name_norm][:last_n]
        print(f"[player_stats] {player_name}: {len(logs)} games (SportsData.io)")
        return logs

    # 2. All name parts present (handles "Jr", middle initials, etc.)
    for key, logs in _LOG_CACHE.items():
        if all(p in key for p in parts):
            result = logs[:last_n]
            print(f"[player_stats] {player_name} → '{key}': {len(result)} games (SportsData.io)")
            return result

    # 3. Last name + first initial
    if len(parts) >= 2:
        last, init = parts[-1], parts[0][0]
        for key, logs in _LOG_CACHE.items():
            kp = key.split()
            if len(kp) >= 2 and kp[-1] == last and kp[0].startswith(init):
                result = logs[:last_n]
                print(f"[player_stats] {player_name} → '{key}': {len(result)} games (SportsData.io)")
                return result

    print(f"[player_stats] Not found in cache: {player_name}")
    return []


# ── Stat extraction helpers ───────────────────────────────────────────────────

def get_stat_value(game: dict, market_key: str) -> float | None:
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
    """Parse MIN: float or 'MM:SS' string → decimal minutes."""
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
# Injury statuses — ESPN public API (primary) + Rotowire (fallback)
# ══════════════════════════════════════════════════════════════════════════════

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

ESPN_SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_HEADERS   = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


def get_injury_statuses(player_names: list[str]) -> dict[str, str | None]:
    result: dict[str, str | None] = {n: None for n in player_names}
    try:
        print("[player_stats] Fetching injury report from ESPN API...")
        injured = _fetch_espn_injuries()
        if injured:
            _match_injuries(injured, player_names, result)
            print(f"[player_stats] ESPN: {sum(1 for v in result.values() if v)} of {len(player_names)} players flagged")
            return result
        print("[player_stats] ESPN returned 0 injuries — trying Rotowire...")
    except Exception as e:
        print(f"[player_stats] ESPN injury error: {e} — trying Rotowire...")

    if BS4_AVAILABLE:
        try:
            injured = _fetch_rotowire_injuries()
            if injured:
                _match_injuries(injured, player_names, result)
                print(f"[player_stats] Rotowire: {sum(1 for v in result.values() if v)} flagged")
        except Exception as e:
            print(f"[player_stats] Rotowire failed: {e}")

    return result


def _fetch_espn_injuries() -> dict[str, str]:
    resp = requests.get(
        f"{ESPN_SITE_BASE}/injuries",
        headers=ESPN_HEADERS, timeout=10,
    )
    resp.raise_for_status()
    injured: dict[str, str] = {}
    for group in resp.json().get("injuries", []):
        for entry in group.get("injuries", []):
            raw   = (entry.get("athlete", {}).get("displayName") or "").strip()
            status = (entry.get("status") or "").strip()
            if not raw or status.lower() == "active":
                continue
            d    = entry.get("details", {})
            desc = (
                f"{status} - {_ascii_safe(d.get('detail',''))}" if d.get("detail")
                else f"{status} - {_ascii_safe(d.get('type',''))}" if d.get("type")
                else status
            )
            injured[_normalize(raw)] = desc
    return injured


def _fetch_rotowire_injuries() -> dict[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = requests.get(
        "https://www.rotowire.com/basketball/injury-report.php",
        headers=headers, timeout=15,
    )
    resp.raise_for_status()
    soup    = BeautifulSoup(resp.text, "html.parser")
    injured: dict[str, str] = {}

    for sel in ["tr.injury-report__row", "tr[class*='injury-report']"]:
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
        injured[_normalize(raw_name)] = f"{status_txt} — {desc_txt}" if desc_txt else status_txt

    print(f"[player_stats] Rotowire: {len(injured)} found")
    return injured


def _match_injuries(injured, player_names, result):
    for target in player_names:
        norm = _normalize(target)
        if norm in injured:
            result[target] = injured[norm]
            print(f"[player_stats] Injury: {target} -> {injured[norm]}")
            continue
        parts = norm.split()
        for inj_name, inj_status in injured.items():
            if all(p in inj_name for p in parts):
                result[target] = inj_status
                print(f"[player_stats] Injury (fuzzy): {target} -> {inj_status}")
                break
