import os
import requests


ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "basketball_nba"
MARKETS = "player_points,player_rebounds,player_assists"


def get_player_props(games: list[dict]) -> dict:
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        print("ODDS_API_KEY not set — skipping props fetch.")
        return {}

    props = {}
    for game in games:
        event_id = _find_event_id(game, api_key)
        if event_id:
            game_props = _fetch_event_props(event_id, api_key)
            props[game["id"]] = game_props

    return props


def _find_event_id(game: dict, api_key: str) -> str | None:
    url = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events"
    params = {"apiKey": api_key}

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        events = response.json()
        home = game["home_team"]["full_name"].lower()
        visitor = game["visitor_team"]["full_name"].lower()
        for event in events:
            if home in event.get("home_team", "").lower() or visitor in event.get("away_team", "").lower():
                return event["id"]
    except requests.RequestException as e:
        print(f"Error fetching events: {e}")

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
        response.raise_for_status()
        data = response.json()
        return data.get("bookmakers", [])
    except requests.RequestException as e:
        print(f"Error fetching props for event {event_id}: {e}")
        return []
