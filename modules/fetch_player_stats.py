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


def get_player_logs(player_name: str, last_n: int = 20) -> list[dict]:
    """
    Fetch the last `last_n` game logs for a player via nba_api (stats.nba.com).
    Returns a list of dicts sorted newest-first, or [] on failure.
    """
    player_id = find_player_id(player_name)
    if not player_id:
        print(f"[player_stats] Not found in NBA DB: {player_name}")
        return []

    season = _current_season()
    try:
        time.sleep(0.7)  # Respect stats.nba.com rate limit
        logs = playergamelogs.PlayerGameLogs(
            player_id_nullable=str(player_id),
            season_nullable=season,
            season_type_nullable="Regular Season",
            last_n_games_nullable=last_n,
        )
        df = logs.get_data_frames()[0]
        if df.empty:
            print(f"[player_stats] No logs for {player_name} (season {season})")
            return []

        df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)
        records = df.to_dict("records")
        print(f"[player_stats] {player_name}: {len(records)} games fetched")
        return records
    except Exception as e:
        print(f"[player_stats] ERROR fetching {player_name}: {e}")
        return []


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


def get_injury_statuses(player_names: list[str]) -> dict[str, str | None]:
    """
    Mejora 13 — Scrape NBA injury report from Rotowire (free, no auth required).
    Returns {player_name: injury_description_or_None}.
    Falls back gracefully on any error.
    """
    result: dict[str, str | None] = {n: None for n in player_names}

    if not BS4_AVAILABLE:
        print("[player_stats] beautifulsoup4 not installed — skipping injury check")
        return result

    try:
        print("[player_stats] Fetching injury report from Rotowire...")
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(
            "https://www.rotowire.com/basketball/injury-report.php",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # Rotowire injury table: each row has player name + status + description
        injured: dict[str, str] = {}
        for row in soup.select("tr.injury-report__row, div.player-injury"):
            # Try table row format
            cells = row.select("td")
            if len(cells) >= 4:
                raw_name   = cells[0].get_text(strip=True)
                status_txt = cells[2].get_text(strip=True)
                desc_txt   = cells[3].get_text(strip=True)
                if raw_name and status_txt.lower() not in ("active", ""):
                    label = f"{status_txt} — {desc_txt}" if desc_txt else status_txt
                    injured[_normalize(raw_name)] = label

        # Fallback: try link-based player names
        if not injured:
            for link in soup.select("a.player-injury__name, td.player-injury-report__player a"):
                raw_name = link.get_text(strip=True)
                row = link.find_parent("tr") or link.find_parent("div")
                if row:
                    status_el = row.select_one(".player-injury__status, td:nth-child(3)")
                    desc_el   = row.select_one(".player-injury__desc,  td:nth-child(4)")
                    status_txt = status_el.get_text(strip=True) if status_el else ""
                    desc_txt   = desc_el.get_text(strip=True)   if desc_el   else ""
                    if raw_name and status_txt.lower() not in ("active", ""):
                        label = f"{status_txt} — {desc_txt}" if desc_txt else status_txt
                        injured[_normalize(raw_name)] = label

        print(f"[player_stats] Rotowire: {len(injured)} injured/questionable players found")

        # Match against our target player list
        for target in player_names:
            target_norm = _normalize(target)
            # Exact match first
            if target_norm in injured:
                result[target] = injured[target_norm]
                print(f"[player_stats] Injury: {target} → {injured[target_norm]}")
                continue
            # Partial: all parts of target name in injured name
            parts = target_norm.split()
            for inj_name, inj_status in injured.items():
                if all(p in inj_name for p in parts):
                    result[target] = inj_status
                    print(f"[player_stats] Injury (fuzzy): {target} → {inj_status}")
                    break

    except Exception as e:
        print(f"[player_stats] Rotowire injury fetch error: {e}")

    return result
