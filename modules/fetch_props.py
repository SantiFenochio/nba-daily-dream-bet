import os
import requests


ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "basketball_nba"

# Player prop markets
PROP_MARKETS = (
    "player_points,"
    "player_rebounds,"
    "player_assists,"
    "player_threes,"
    "player_steals,"
    "player_blocks,"
    "player_points_rebounds_assists,"
    "player_turnovers"
)

# Game line markets (fetched together to avoid extra API calls)
LINE_MARKETS = "spreads,totals"

MARKET_LABELS = {
    "player_points": "Puntos",
    "player_rebounds": "Rebotes",
    "player_assists": "Asistencias",
    "player_threes": "Triples",
    "player_steals": "Robos",
    "player_blocks": "Tapas",
    "player_points_rebounds_assists": "PRA",
    "player_turnovers": "Pérdidas",
}


def get_player_props(games: list[dict]) -> tuple[dict, dict]:
    """
    Returns:
      props      — {game_id: [bookmaker, ...]}
      game_lines — {game_id: {spread, total, home_is_favorite}}

    Fetches player props AND game spread/total in a single API call per game,
    so we can detect blowout risk in the analyzer without an extra request.
    """
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        print("[fetch_props] ODDS_API_KEY not set — skipping props.")
        return {}, {}

    print(f"[fetch_props] Fetching props for {len(games)} games...")

    # Fetch all events ONCE (not once per game)
    all_events = _fetch_all_events(api_key)
    print(f"[fetch_props] Total events from Odds API: {len(all_events)}")

    props: dict = {}
    game_lines: dict = {}

    for game in games:
        label = f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}"
        print(f"[fetch_props] Matching event for: {label}")
        event_id = _match_event(game, all_events)

        if event_id:
            print(f"[fetch_props]   Event matched: {event_id}")
            bookmakers, lines = _fetch_event_props_and_lines(
                event_id, api_key,
                home_full=game["home_team"]["full_name"],
            )
            print(f"[fetch_props]   Bookmakers returned: {len(bookmakers)}")
            props[game["id"]] = bookmakers
            if lines:
                game_lines[game["id"]] = lines
                spread_str = f"{lines['spread']:+.1f}" if lines.get("spread") is not None else "n/a"
                total_str  = f"{lines['total']:.1f}"  if lines.get("total")  is not None else "n/a"
                print(f"[fetch_props]   Lines — spread: {spread_str} | total: {total_str}")
        else:
            print(f"[fetch_props]   No event match found for: {label}")

    return props, game_lines


def parse_props(raw_props: dict, games: list[dict]) -> list[dict]:
    """
    Flatten raw bookmaker data into a clean list of individual prop records.

    Each record:
        player, market_key, line, side, price, opposite_price,
        game_id, game_label, home_team_abbr, visitor_team_abbr

    Deduplication: one record per (player, market_key, side, game_id) — best price kept.
    opposite_price: the other side's price for proper two-sided devig.
    """
    game_meta = {
        g["id"]: {
            "game_label": f"{g['visitor_team']['full_name']} @ {g['home_team']['full_name']}",
            "home_team_abbr": g["home_team"]["abbreviation"],
            "visitor_team_abbr": g["visitor_team"]["abbreviation"],
        }
        for g in games
    }

    best: dict[tuple, dict] = {}

    for game_id, bookmakers in raw_props.items():
        meta = game_meta.get(game_id, {})

        for bookmaker in bookmakers:
            for market in bookmaker.get("markets", []):
                market_key = market.get("key", "")
                if market_key not in MARKET_LABELS:
                    continue

                for outcome in market.get("outcomes", []):
                    player = (outcome.get("description") or "").strip()
                    side   = (outcome.get("name") or "").strip()
                    line   = outcome.get("point")
                    price  = outcome.get("price")

                    if not player or not side or line is None or price is None:
                        continue

                    dedup_key = (player, market_key, side, game_id)
                    if dedup_key not in best or price > best[dedup_key]["price"]:
                        best[dedup_key] = {
                            "player": player,
                            "market_key": market_key,
                            "line": float(line),
                            "side": side,
                            "price": int(price),
                            "game_id": game_id,
                            **meta,
                        }

    # Add opposite_price for proper two-sided devig in analyzer
    for key, rec in best.items():
        player, mkt, side, game_id = key
        opp_side = "Under" if side == "Over" else "Over"
        opp_key  = (player, mkt, opp_side, game_id)
        rec["opposite_price"] = best[opp_key]["price"] if opp_key in best else None

    result = list(best.values())
    print(f"[fetch_props] Parsed {len(result)} unique prop outcomes across all games")
    return result


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fetch_all_events(api_key: str) -> list[dict]:
    """Fetch all current NBA events in a single call."""
    url = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events"
    try:
        response = requests.get(url, params={"apiKey": api_key}, timeout=10)
        print(f"[fetch_props]   /events HTTP {response.status_code}")
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"[fetch_props] ERROR fetching /events: {e}")
        return []


def _match_event(game: dict, events: list[dict]) -> str | None:
    """Match a BallDontLie game to an Odds API event (AND logic first, OR fallback)."""
    home    = game["home_team"]["full_name"].lower()
    visitor = game["visitor_team"]["full_name"].lower()

    for event in events:
        ev_home = event.get("home_team", "").lower()
        ev_away = event.get("away_team", "").lower()
        if (home in ev_home or ev_home in home) and (visitor in ev_away or ev_away in visitor):
            return event["id"]

    for event in events:
        ev_home = event.get("home_team", "").lower()
        ev_away = event.get("away_team", "").lower()
        if home in ev_home or visitor in ev_away or home in ev_away or visitor in ev_home:
            return event["id"]

    return None


def _fetch_event_props_and_lines(
    event_id: str,
    api_key: str,
    home_full: str,
) -> tuple[list[dict], dict]:
    """
    Single API call returning both player prop bookmakers and game lines.
    Requesting spreads+totals alongside player props costs no extra quota.
    """
    all_markets = f"{PROP_MARKETS},{LINE_MARKETS}"
    url = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events/{event_id}/odds"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": all_markets,
        "oddsFormat": "american",
    }

    try:
        response = requests.get(url, params=params, timeout=15)
        print(f"[fetch_props]   /events/odds HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()
        bookmakers = data.get("bookmakers", [])
        lines = _extract_game_lines(bookmakers, home_full)
        return bookmakers, lines
    except requests.RequestException as e:
        print(f"[fetch_props] ERROR /events/odds {event_id}: {e}")
        return [], {}


def _extract_game_lines(bookmakers: list[dict], home_full: str) -> dict:
    """
    Extract point spread and game total from bookmaker data.

    spread   — from the HOME team's perspective (negative = home is favored)
    total    — expected combined score
    home_is_favorite — True if spread < 0

    Returns {} if data is missing.
    """
    spread: float | None = None
    total:  float | None = None
    home_words = set(home_full.lower().split())

    for bm in bookmakers:
        for market in bm.get("markets", []):
            key      = market.get("key", "")
            outcomes = market.get("outcomes", [])

            if key == "spreads" and spread is None:
                for outcome in outcomes:
                    name  = outcome.get("name", "").lower()
                    point = outcome.get("point")
                    if point is None:
                        continue
                    # Match outcome name to home team
                    name_words = set(name.split())
                    if name_words & home_words:          # intersection
                        spread = float(point)
                        break

            if key == "totals" and total is None:
                for outcome in outcomes:
                    if outcome.get("name", "") == "Over":
                        total = float(outcome.get("point", 0) or 0)
                        break

        if spread is not None and total is not None:
            break

    if spread is None and total is None:
        return {}

    return {
        "spread": spread,                                    # home-perspective
        "total": total,
        "home_is_favorite": (spread < 0) if spread is not None else None,
    }
