import os
import requests


ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "basketball_nba"
MARKETS = "player_points,player_rebounds,player_assists"


def get_player_props(games: list[dict]) -> dict:
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        print("[fetch_props] ODDS_API_KEY not set — skipping props.")
        return {}

    print(f"[fetch_props] Fetching props for {len(games)} games...")
    props = {}
    for game in games:
        label = f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}"
        print(f"[fetch_props] Looking up event for: {label}")
        event_id = _find_event_id(game, api_key)
        if event_id:
            print(f"[fetch_props]   Event found: {event_id}")
            game_props = _fetch_event_props(event_id, api_key)
            print(f"[fetch_props]   Bookmakers returned: {len(game_props)}")
            props[game["id"]] = game_props
        else:
            print(f"[fetch_props]   No event match found for: {label}")

    return props


def _find_event_id(game: dict, api_key: str) -> str | None:
    url = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events"
    params = {"apiKey": api_key}

    try:
        response = requests.get(url, params=params, timeout=10)
        print(f"[fetch_props]   /events HTTP {response.status_code}")
        response.raise_for_status()
        events = response.json()
        print(f"[fetch_props]   Total events from API: {len(events)}")
        home = game["home_team"]["full_name"].lower()
        visitor = game["visitor_team"]["full_name"].lower()
        for event in events:
            if home in event.get("home_team", "").lower() or visitor in event.get("away_team", "").lower():
                return event["id"]
    except requests.RequestException as e:
        print(f"[fetch_props] ERROR /events: {e}")

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
        response = requests.get(url, params=params, timeout=10)
        print(f"[fetch_props]   /events/odds HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()
        bookmakers = data.get("bookmakers", [])
        return bookmakers
    except requests.RequestException as e:
        print(f"[fetch_props] ERROR /events/odds {event_id}: {e}")
        return []
