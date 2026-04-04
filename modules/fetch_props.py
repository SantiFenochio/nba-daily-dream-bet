import os
import requests


ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "basketball_nba"
PROP_MARKETS = "player_points,player_rebounds,player_assists"
GAME_MARKETS = "h2h,spreads,totals"


def get_player_props(games: list[dict]) -> dict:
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        print("ODDS_API_KEY not set — skipping props fetch.")
        return {}

    events = _fetch_all_events(api_key)
    props = {}
    for game in games:
        event_id = _match_event(game, events)
        if event_id:
            game_props = _fetch_event_props(event_id, api_key)
            props[game["id"]] = game_props

    return props


def get_game_odds(games: list[dict]) -> dict:
    """Returns {game_id: {"spread": float, "total": float, "moneyline_home": int, "moneyline_visitor": int}}."""
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        return {}

    events = _fetch_all_events(api_key)
    odds = {}
    for game in games:
        event_id = _match_event(game, events)
        if event_id:
            game_odds = _fetch_event_game_odds(event_id, api_key)
            odds[game["id"]] = game_odds

    return odds


def _fetch_all_events(api_key: str) -> list[dict]:
    url = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events"
    try:
        response = requests.get(url, params={"apiKey": api_key}, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error fetching events: {e}")
        return []


def _match_event(game: dict, events: list[dict]) -> str | None:
    home = game["home_team"]["full_name"].lower()
    visitor = game["visitor_team"]["full_name"].lower()
    for event in events:
        event_home = event.get("home_team", "").lower()
        event_away = event.get("away_team", "").lower()
        if home in event_home or visitor in event_away:
            return event["id"]
    return None


def _fetch_event_props(event_id: str, api_key: str) -> list[dict]:
    url = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events/{event_id}/odds"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": PROP_MARKETS,
        "oddsFormat": "american",
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("bookmakers", [])
    except requests.RequestException as e:
        print(f"Error fetching props for event {event_id}: {e}")
        return []


def _fetch_event_game_odds(event_id: str, api_key: str) -> dict:
    url = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events/{event_id}/odds"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": GAME_MARKETS,
        "oddsFormat": "american",
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        return _parse_game_odds(data)
    except requests.RequestException as e:
        print(f"Error fetching game odds for event {event_id}: {e}")
        return {}


def _parse_game_odds(data: dict) -> dict:
    result = {"spread": 0.0, "total": 0.0, "moneyline_home": 0, "moneyline_visitor": 0}
    bookmakers = data.get("bookmakers", [])
    if not bookmakers:
        return result

    # Use the first bookmaker
    bookmaker = bookmakers[0]
    for market in bookmaker.get("markets", []):
        key = market.get("key", "")
        outcomes = market.get("outcomes", [])

        if key == "spreads":
            home_team = data.get("home_team", "")
            for outcome in outcomes:
                if outcome.get("name", "").lower() in home_team.lower():
                    result["spread"] = outcome.get("point", 0.0)
                    break
            else:
                # fallback: first outcome point
                if outcomes:
                    result["spread"] = outcomes[0].get("point", 0.0)

        elif key == "totals":
            for outcome in outcomes:
                if outcome.get("name", "").lower() == "over":
                    result["total"] = outcome.get("point", 0.0)
                    break

        elif key == "h2h":
            home_team = data.get("home_team", "")
            for outcome in outcomes:
                name = outcome.get("name", "")
                price = outcome.get("price", 0)
                if name.lower() in home_team.lower():
                    result["moneyline_home"] = price
                else:
                    result["moneyline_visitor"] = price

    return result
