import time
import unicodedata
import requests
from datetime import date

from nba_api.stats.static import players as nba_players
from nba_api.stats.endpoints import playergamelogs

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

# Maps Odds API market keys to nba_api DataFrame column names
STAT_COL = {
    "player_points": "PTS",
    "player_rebounds": "REB",
    "player_assists": "AST",
    "player_threes": "FG3M",
    "player_steals": "STL",
    "player_blocks": "BLK",
    "player_turnovers": "TOV",
    "player_points_rebounds_assists": "__PRA__",
}

_PLAYER_ID_CACHE: dict[str, int | None] = {}
_ALL_NBA_PLAYERS: list[dict] = []


def _load_nba_players() -> list[dict]:
    global _ALL_NBA_PLAYERS
    if not _ALL_NBA_PLAYERS:
        _ALL_NBA_PLAYERS = nba_players.get_players()
    return _ALL_NBA_PLAYERS


def _current_season() -> str:
    today = date.today()
    year = today.year if today.month >= 10 else today.year - 1
    return f"{year}-{str(year + 1)[-2:]}"


def _current_season_type() -> str:
    """
    BUG FIX: Detect NBA season phase so we pull the right game logs.
    - Regular season ends ~April 12
    - Play-In: ~April 14-17  (nba_api uses "PlayIn" or "Regular Season" for those)
    - Playoffs: ~April 18 onwards
    """
    today = date.today()
    # Playoffs start around April 18 each year
    if today.month > 4 or (today.month == 4 and today.day >= 18):
        return "Playoffs"
    return "Regular Season"


def _normalize(text: str) -> str:
    """Lowercase + strip accents for fuzzy matching (handles Jokić → Jokic etc.)."""
    return unicodedata.normalize("NFD", text.lower().strip()).encode("ascii", "ignore").decode()


def find_player_id(name: str) -> int | None:
    if name in _PLAYER_ID_CACHE:
        return _PLAYER_ID_CACHE[name]

    all_p = _load_nba_players()
    name_norm = _normalize(name)

    # 1. Exact normalized match
    for p in all_p:
        if _normalize(p["full_name"]) == name_norm:
            _PLAYER_ID_CACHE[name] = p["id"]
            return p["id"]

    # 2. All parts present (handles middle names / suffixes)
    parts = name_norm.split()
    for p in all_p:
        full_norm = _normalize(p["full_name"])
        if all(part in full_norm for part in parts):
            _PLAYER_ID_CACHE[name] = p["id"]
            return p["id"]

    # 3. Last name + first initial (e.g. "P. Reed" style mismatches)
    if len(parts) >= 2:
        last = parts[-1]
        first_initial = parts[0][0]
        for p in all_p:
            full_norm = _normalize(p["full_name"])
            full_parts = full_norm.split()
            if len(full_parts) >= 2 and full_parts[-1] == last and full_parts[0].startswith(first_initial):
                _PLAYER_ID_CACHE[name] = p["id"]
                return p["id"]

    _PLAYER_ID_CACHE[name] = None
    return None


def _fetch_game_logs(player_id: int, season: str, season_type: str, last_n: int) -> list[dict]:
    """Internal: fetch game logs for a specific season type."""
    try:
        time.sleep(0.7)  # Respect stats.nba.com rate limit
        logs = playergamelogs.PlayerGameLogs(
            player_id_nullable=str(player_id),
            season_nullable=season,
            season_type_nullable=season_type,
            last_n_games_nullable=last_n,
        )
        df = logs.get_data_frames()[0]
        if df.empty:
            return []
        df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)
        return df.to_dict("records")
    except Exception as e:
        print(f"[player_stats] ERROR fetching logs ({season_type}): {e}")
        return []


def get_player_logs(player_name: str, last_n: int = 20) -> list[dict]:
    """
    Fetch the last `last_n` game logs for a player via nba_api.
    BUG FIX: Detects playoffs automatically. If in playoffs but fewer than
    5 games, supplements with recent regular season games so the model
    always has enough data.
    Returns a list of dicts sorted newest-first, or [] on failure.
    """
    player_id = find_player_id(player_name)
    if not player_id:
        print(f"[player_stats] Not found in NBA DB: {player_name}")
        return []

    season = _current_season()
    season_type = _current_season_type()

    records = _fetch_game_logs(player_id, season, season_type, last_n)

    # In playoffs: supplement with regular season data if not enough playoff games
    if season_type == "Playoffs" and len(records) < 5:
        print(f"[player_stats] {player_name}: only {len(records)} playoff games — supplementing with regular season")
        reg_records = _fetch_game_logs(player_id, season, "Regular Season", last_n)
        existing_ids = {r.get("GAME_ID") for r in records}
        supplemental = [r for r in reg_records if r.get("GAME_ID") not in existing_ids]
        records = records + supplemental

    records = records[:last_n]

    if records:
        print(f"[player_stats] {player_name}: {len(records)} games fetched ({season_type})")
    else:
        print(f"[player_stats] No logs for {player_name} (season {season}, type {season_type})")
    return records


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
    """Parse nba_api MIN field which can be float or 'MM:SS' string."""
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


# ══════════════════════════════════════════════════════════════════════════
# Injury status — ESPN public API (primary) + Rotowire (fallback)
# BUG FIX: Rotowire CSS selectors were broken. Now uses ESPN JSON API first.
# ══════════════════════════════════════════════════════════════════════════

def get_injury_statuses(player_names: list[str]) -> dict[str, str | None]:
    """
    Returns {player_name: injury_description_or_None}.
    Primary: ESPN public API (no auth, JSON).
    Fallback: Rotowire scraper.
    """
    result: dict[str, str | None] = {n: None for n in player_names}

    # ── Primary: ESPN public API ──────────────────────────────────────────
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

    # ── Fallback: Rotowire ────────────────────────────────────────────────
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


def _ascii_safe(text: str) -> str:
    """Replace non-ASCII characters so Windows console (cp1252) doesn't choke."""
    return unicodedata.normalize("NFD", text).encode("ascii", "replace").decode("ascii")


def _fetch_espn_injuries() -> dict[str, str]:
    """
    ESPN public API — no auth required.
    Returns {normalized_player_name: status_string}.
    """
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    injured: dict[str, str] = {}

    # ESPN response: {"injuries": [{"team": {...}, "injuries": [{...}, ...]}, ...]}
    team_groups = data.get("injuries", [])
    for group in team_groups:
        for entry in group.get("injuries", []):
            athlete = entry.get("athlete", {})
            raw_name = athlete.get("displayName", "").strip()
            if not raw_name:
                continue

            status = entry.get("status", "").strip()
            # Skip "Active" — only flag truly limited/out players
            if not status or status.lower() in ("active", ""):
                continue

            # Build description — sanitize to ASCII to avoid Windows encoding errors
            details = entry.get("details", {})
            injury_type = _ascii_safe(details.get("type", ""))
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
    """
    Rotowire fallback — improved multi-selector parsing.
    Returns {normalized_player_name: status_string}.
    """
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

    soup = BeautifulSoup(resp.text, "html.parser")
    injured: dict[str, str] = {}

    # Try multiple selectors for different Rotowire HTML versions
    selectors = [
        "tr.injury-report__row",
        "tr[class*='injury-report']",
        "div.lineup__player",
    ]

    rows = []
    for sel in selectors:
        rows = soup.select(sel)
        if rows:
            break

    for row in rows:
        cells = row.select("td")
        if len(cells) < 4:
            continue
        raw_name = cells[0].get_text(strip=True)
        status_txt = cells[2].get_text(strip=True) if len(cells) > 2 else ""
        desc_txt = cells[3].get_text(strip=True) if len(cells) > 3 else ""

        if not raw_name or status_txt.lower() in ("active", ""):
            continue

        label = f"{status_txt} — {desc_txt}" if desc_txt else status_txt
        injured[_normalize(raw_name)] = label

    # Generic fallback: find any table that looks like an injury report
    if not injured:
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue
                raw_name = cells[0].get_text(strip=True)
                # Look for status keywords in any cell
                row_text = " ".join(c.get_text(strip=True).lower() for c in cells)
                if any(kw in row_text for kw in ("questionable", "out", "doubtful", "day-to-day", "gtd")):
                    status_txt = next(
                        (c.get_text(strip=True) for c in cells
                         if c.get_text(strip=True).lower() in ("questionable", "out", "doubtful", "day-to-day", "gtd")),
                        "Questionable"
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
    """Match injury dict (normalized names) against target player list."""
    for target in player_names:
        target_norm = _normalize(target)
        # Exact match
        if target_norm in injured:
            result[target] = injured[target_norm]
            print(f"[player_stats] Injury: {target} -> {injured[target_norm]}")
            continue
        # All name parts present
        parts = target_norm.split()
        for inj_name, inj_status in injured.items():
            if all(p in inj_name for p in parts):
                result[target] = inj_status
                print(f"[player_stats] Injury (fuzzy): {target} -> {inj_status}")
                break
