import requests
from datetime import date


NBA_API_BASE = "https://www.balldontlie.io/api/v1"


def get_today_games() -> list[dict]:
    today = date.today().isoformat()
    url = f"{NBA_API_BASE}/games"
    params = {"dates[]": today, "per_page": 30}

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        games = data.get("data", [])
        return games
    except requests.RequestException as e:
        print(f"Error fetching games: {e}")
        return []
