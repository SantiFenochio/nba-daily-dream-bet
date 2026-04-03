import os
import requests


NBA_API_BASE = "https://www.balldontlie.io/api/v1"


def get_today_games(date_str: str) -> list[dict]:
    print(f"[fetch_games] Querying BallDontLie for date: {date_str}")
    url = f"{NBA_API_BASE}/games"
    params = {"dates[]": date_str, "per_page": 30}

    headers = {}
    api_key = os.getenv("BALLDONTLIE_API_KEY")
    if api_key:
        headers["Authorization"] = api_key
        print("[fetch_games] Using BALLDONTLIE_API_KEY.")

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        print(f"[fetch_games] HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()
        games = data.get("data", [])
        print(f"[fetch_games] Games in response: {len(games)}")
        for g in games:
            print(
                f"[fetch_games]   • {g['visitor_team']['full_name']} @ "
                f"{g['home_team']['full_name']} — status: {g.get('status')}"
            )
        return games
    except requests.RequestException as e:
        print(f"[fetch_games] ERROR: {e}")
        return []
