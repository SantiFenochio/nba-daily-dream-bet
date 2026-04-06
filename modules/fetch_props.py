import os
import requests


ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "basketball_nba"
MARKETS = (
    "player_points,"
    "player_rebounds,"
    "player_assists,"
    "player_threes,"
    "player_steals,"
    "player_blocks,"
    "player_points_rebounds_assists,"
    "player_turnovers"
)

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


def get_player_props(games: list[dict]) -> dict:
    """Returns {game_id: [bookmaker, ...]} with all available player prop markets."""
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        print("[fetch_props] ODDS_API_KEY not set — skipping props.")
        return {}

    print(f"[fetch_props] Fetching props for {len(games)} games...")

    # BUG FIX: fetch all events ONCE instead of once per game
    all_events = _fetch_all_events(api_key)
    print(f"[fetch_props] Total events from Odds API: {len(all_events)}")

    props = {}
    for game in games:
        label = f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}"
        print(f"[fetch_props] Matching event for: {label}")
        event_id = _match_event(game, all_events)
        if event_id:
            print(f"[fetch_props]   Event matched: {event_id}")
            game_props = _fetch_event_props(event_id, api_key)
            print(f"[fetch_props]   Bookmakers returned: {len(game_props)}")
            props[game["id"]] = game_props
        else:
            print(f"[fetch_props]   No event match found for: {label}")

    return props


def parse_props(raw_props: dict, games: list[dict]) -> list[dict]:
    """
    Flatten raw bookmaker data into a clean list of individual prop records.

    Each record:
        player, market_key, line, side, price, opposite_price,
        game_id, game_label, home_team_abbr, visitor_team_abbr

    Deduplication: one record per (player, market_key, side, game_id) — best price kept.
    opposite_price: the price of the other side (Over↔Under) for two-sided devig.
    """
    game_meta = {
        g["id"]: {
            "game_label": f"{g['visitor_team']['full_name']} @ {g['home_team']['full_name']}",
            "home_team_abbr": g["home_team"]["abbreviation"],
            "visitor_team_abbr": g["visitor_team"]["abbreviation"],
        }
        for g in games
    }

    # (player, market_key, side, game_id) → best record
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
                    side = (outcome.get("name") or "").strip()     # "Over" / "Under"
                    line = outcome.get("point")
                    price = outcome.get("price")

                    if not player or not side or line is None or price is None:
                        continue

                    dedup_key = (player, market_key, side, game_id)

                    # Keep the most favorable price (highest American odds = best payout)
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

    # BUG FIX: add opposite_price for proper two-sided devig in analyzer
    for key, rec in best.items():
        player, mkt, side, game_id = key
        opp_side = "Under" if side == "Over" else "Over"
        opp_key = (player, mkt, opp_side, game_id)
        rec["opposite_price"] = best[opp_key]["price"] if opp_key in best else None

    result = list(best.values())
    print(f"[fetch_props] Parsed {len(result)} unique prop outcomes across all games")
    return result


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fetch_all_events(api_key: str) -> list[dict]:
    """Fetch all current NBA events from The Odds API in a single call."""
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
    """
    Match a BallDontLie game to an Odds API event.
    Uses AND logic (both teams must match) before falling back to OR.
    """
    home = game["home_team"]["full_name"].lower()
    visitor = game["visitor_team"]["full_name"].lower()

    # Strict: both teams match
    for event in events:
        ev_home = event.get("home_team", "").lower()
        ev_away = event.get("away_team", "").lower()
        home_match = home in ev_home or ev_home in home
        away_match = visitor in ev_away or ev_away in visitor
        if home_match and away_match:
            return event["id"]

    # Lenient fallback: either team matches
    for event in events:
        ev_home = event.get("home_team", "").lower()
        ev_away = event.get("away_team", "").lower()
        if home in ev_home or visitor in ev_away or home in ev_away or visitor in ev_home:
            return event["id"]

    return None


def _fetch_event_props(event_id: str, api_key: str) -> list[dict]:
    url = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events/{event_id}/odds"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": MARKETS,
        "oddsFormat": "american",
    }

    try:
        response = requests.get(url, params=params, timeout=15)
        print(f"[fetch_props]   /events/odds HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()
        return data.get("bookmakers", [])
    except requests.RequestException as e:
        print(f"[fetch_props] ERROR /events/odds {event_id}: {e}")
        return []
