"""
fetch_player_stats.py — ESPN box score batch game-log loader

WHY THIS EXISTS:
  - stats.nba.com      → blocks all GitHub Actions (cloud) IPs completely
  - BallDontLie /stats → requires paid tier (free tier = 401 Unauthorized)
  - SportsData.io free → sandbox-only (2019-20 data), no current season data
  - ESPN athlete search → /athletes?search= endpoint returns 404

SOLUTION:
  ESPN public APIs require no auth and are accessible from GitHub Actions:
  1. ESPN scoreboard /scoreboard?dates=YYYYMMDD → game IDs + team context
  2. ESPN /summary?event=GAMEID → full box score for ALL players in a game

  ~28 scoreboard calls + ~100 summary calls = ~130 total API calls per run.
  Each call is throttled to 0.15-0.20s apart.

Exports:
    get_player_logs(name, last_n=20)  → list[dict]
    get_stat_value(game, market_key) → float | None
    parse_minutes(min_val)           → float
    get_injury_statuses(names)       → dict[str, str | None]
"""

import time
import unicodedata
import requests
from datetime import date, timedelta

# ── ESPN API config ───────────────────────────────────────────────────────────
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

# ── Stat column map (market_key → internal field name) ────────────────────────
STAT_COL = {
    "player_points":                  "PTS",
    "player_rebounds":                "REB",
    "player_assists":                 "AST",
    "player_threes":                  "FG3M",
    "player_steals":                  "STL",
    "player_blocks":                  "BLK",
    "player_turnovers":               "TOV",
    "player_points_rebounds_assists": "__PRA__",
    "player_points_assists":          "__PA__",
    "player_points_rebounds":         "__PR__",
    "player_rebounds_assists":        "__RA__",
    "player_blocks_steals":           "__BS__",
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


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _parse_made(val: str) -> float:
    """Parse 'made-attempted' string → float(made). E.g. '4-10' → 4.0"""
    if isinstance(val, str) and "-" in val:
        try:
            return float(val.split("-")[0])
        except ValueError:
            pass
    return _safe_float(val)


def _get_stat(labels: list, stats: list, label: str) -> str:
    """Look up a stat value by its column label."""
    try:
        idx = labels.index(label)
        return stats[idx] if idx < len(stats) else "0"
    except ValueError:
        return "0"


def _espn_date(d: date) -> str:
    """ESPN scoreboard date format: '20260406'"""
    return d.strftime("%Y%m%d")


# ── Cache loader (called once, fetches last N game days via ESPN) ─────────────

def _load_cache(max_game_days: int = 28) -> None:
    """
    Fetch ESPN scoreboard + game summaries for each of the last max_game_days
    days that had NBA games, then populate _LOG_CACHE.  Runs once per process.
    """
    global _LOG_CACHE, _CACHE_READY

    if _CACHE_READY:
        return

    print(f"[player_stats] Loading ESPN box score cache (up to {max_game_days} game days)...")

    today = date.today()
    game_days = 0
    tmp: dict[str, list[dict]] = {}

    for days_back in range(1, 55):  # search up to 55 calendar days back
        if game_days >= max_game_days:
            break

        check = today - timedelta(days=days_back)
        date_str = _espn_date(check)

        try:
            time.sleep(0.2)
            # Step 1: get game IDs for this date
            r = requests.get(
                f"{ESPN_SITE}/scoreboard",
                headers=ESPN_HEADERS,
                params={"dates": date_str},
                timeout=12,
            )
            if r.status_code != 200:
                continue

            events = r.json().get("events", [])
            if not events:
                continue  # no games this date

            # Build game context: {game_id: {date, home_abbr, away_abbr}}
            game_ctx: dict[str, dict] = {}
            for evt in events:
                gid = evt.get("id", "")
                if not gid:
                    continue
                comp = evt.get("competitions", [{}])[0]
                teams: dict[str, str] = {}
                for comp_team in comp.get("competitors", []):
                    ha = comp_team.get("homeAway", "")
                    abbr = comp_team.get("team", {}).get("abbreviation", "").upper()
                    if ha and abbr:
                        teams[ha] = abbr
                game_ctx[gid] = {
                    "date":  check,
                    "home":  teams.get("home", ""),
                    "away":  teams.get("away", ""),
                }

            game_days += 1

            # Step 2: for each game, fetch box score
            for gid, ctx in game_ctx.items():
                time.sleep(0.15)
                try:
                    sr = requests.get(
                        f"{ESPN_SITE}/summary",
                        headers=ESPN_HEADERS,
                        params={"event": gid},
                        timeout=12,
                    )
                    if sr.status_code != 200:
                        continue

                    boxscore = sr.json().get("boxscore", {})
                    for team_data in boxscore.get("players", []):
                        team_abbr = team_data.get("team", {}).get("abbreviation", "").upper()
                        opp_abbr  = ctx["away"] if team_abbr == ctx["home"] else ctx["home"]
                        is_home   = (team_abbr == ctx["home"])
                        matchup   = (
                            f"{team_abbr} vs. {opp_abbr}" if is_home
                            else f"{team_abbr} @ {opp_abbr}"
                        )

                        for stat_group in team_data.get("statistics", []):
                            labels = stat_group.get("labels", [])
                            for athlete_data in stat_group.get("athletes", []):
                                # Skip DNP players
                                if athlete_data.get("didNotPlay", False):
                                    continue
                                stats_arr = athlete_data.get("stats", [])
                                if not stats_arr:
                                    continue

                                name = (
                                    athlete_data.get("athlete", {}).get("displayName") or ""
                                ).strip()
                                if not name:
                                    continue

                                min_str = _get_stat(labels, stats_arr, "MIN")
                                if parse_minutes(min_str) <= 0:
                                    continue

                                rec = {
                                    "PTS":  _safe_float(_get_stat(labels, stats_arr, "PTS")),
                                    "REB":  _safe_float(_get_stat(labels, stats_arr, "REB")),
                                    "AST":  _safe_float(_get_stat(labels, stats_arr, "AST")),
                                    "FG3M": _parse_made(_get_stat(labels, stats_arr, "3PT")),
                                    "STL":  _safe_float(_get_stat(labels, stats_arr, "STL")),
                                    "BLK":  _safe_float(_get_stat(labels, stats_arr, "BLK")),
                                    "TOV":  _safe_float(_get_stat(labels, stats_arr, "TO")),
                                    "PF":   _safe_float(_get_stat(labels, stats_arr, "PF")),
                                    "MIN":  min_str,
                                    "TEAM_ABBREVIATION": team_abbr,
                                    "GAME_DATE": ctx["date"].isoformat(),
                                    "GAME_ID":   str(gid),
                                    "MATCHUP":   matchup,
                                }
                                tmp.setdefault(_normalize(name), []).append(rec)

                except Exception:
                    continue

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


# ── Public API ────────────────────────────────────────────────────────────────

def get_player_logs(player_name: str, last_n: int = 20) -> list[dict]:
    """
    Return the last `last_n` game records for `player_name` from the
    ESPN box score cache.  Cache is built on first call.
    """
    _load_cache()

    if not _LOG_CACHE:
        return []

    name_norm = _normalize(player_name)
    parts     = name_norm.split()

    # 1. Exact normalised match
    if name_norm in _LOG_CACHE:
        logs = _LOG_CACHE[name_norm][:last_n]
        print(f"[player_stats] {player_name}: {len(logs)} games (ESPN)")
        return logs

    # 2. All name parts present (handles "Jr", middle initials, etc.)
    for key, logs in _LOG_CACHE.items():
        if all(p in key for p in parts):
            result = logs[:last_n]
            print(f"[player_stats] {player_name} → '{key}': {len(result)} games (ESPN)")
            return result

    # 3. Last name + first initial
    if len(parts) >= 2:
        last, init = parts[-1], parts[0][0]
        for key, logs in _LOG_CACHE.items():
            kp = key.split()
            if len(kp) >= 2 and kp[-1] == last and kp[0].startswith(init):
                result = logs[:last_n]
                print(f"[player_stats] {player_name} → '{key}': {len(result)} games (ESPN)")
                return result

    print(f"[player_stats] Not found in cache: {player_name}")
    return []


# ── Stat extraction helpers ───────────────────────────────────────────────────

def get_stat_value(game: dict, market_key: str) -> float | None:
    combos = {
        "player_points_rebounds_assists": ("PTS", "REB", "AST"),
        "player_points_assists":          ("PTS", "AST"),
        "player_points_rebounds":         ("PTS", "REB"),
        "player_rebounds_assists":        ("REB", "AST"),
        "player_blocks_steals":           ("BLK", "STL"),
    }
    if market_key in combos:
        return sum(float(game.get(c) or 0) for c in combos[market_key])
    col = STAT_COL.get(market_key)
    if col and not col.startswith("__"):
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

    rows = []
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
