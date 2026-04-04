import json
import os
import requests
from datetime import date, timedelta
from dataclasses import asdict


HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "picks_history.json")
NBA_API_BASE = "https://www.balldontlie.io/api/v1"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def load_history() -> list[dict]:
    """Load all saved picks from the history file."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f).get("picks", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_picks(picks, games: list[dict]) -> None:
    """Append today's picks to history (skip if already saved for today)."""
    history = load_history()
    today_str = date.today().isoformat()

    # Avoid duplicates: remove any existing picks for today before re-saving
    history = [p for p in history if p.get("date") != today_str]

    game_map = {g["id"]: g for g in games}

    for pick in picks:
        # Find the original game by matching team names
        game = _find_game_for_pick(pick, games)
        if not game:
            continue

        history.append({
            "date": today_str,
            "game_id": game["id"],
            "home_team_id": game["home_team"]["id"],
            "visitor_team_id": game["visitor_team"]["id"],
            "home_team": pick.home_team,
            "visitor_team": pick.visitor_team,
            "predicted_winner": pick.recommended_bet,
            "predicted_home_wins": pick.home_team in pick.recommended_bet,
            "confidence": pick.confidence,
            "model_edge": round(pick.model_edge, 4),
            "market_spread": pick.market_spread,
            # Results filled in later
            "home_score": None,
            "visitor_score": None,
            "correct": None,
        })

    _write_history(history)


def update_results(history: list[dict]) -> tuple[list[dict], int]:
    """Fetch final scores for any pending picks and mark them correct/incorrect.

    Returns (updated_history, num_updated).
    """
    pending = [p for p in history if p.get("correct") is None and p.get("date") != date.today().isoformat()]
    if not pending:
        return history, 0

    # Fetch actual results for each pending game
    updated = 0
    for pick in pending:
        game_result = _fetch_game_result(pick["game_id"])
        if game_result is None:
            continue

        home_score = game_result.get("home_team_score")
        visitor_score = game_result.get("visitor_team_score")
        if not home_score or not visitor_score:
            continue  # game not finished yet

        actual_home_wins = home_score > visitor_score
        pick["home_score"] = home_score
        pick["visitor_score"] = visitor_score
        pick["correct"] = pick["predicted_home_wins"] == actual_home_wins
        updated += 1

    if updated:
        _write_history(history)

    return history, updated


def compute_stats(history: list[dict]) -> dict:
    """Return running performance stats from history."""
    resolved = [p for p in history if p.get("correct") is not None]
    if not resolved:
        return {"total": 0, "correct": 0, "accuracy": 0.0, "last_7": {"total": 0, "correct": 0, "accuracy": 0.0}}

    total = len(resolved)
    correct = sum(1 for p in resolved if p["correct"])
    accuracy = correct / total

    cutoff = (date.today() - timedelta(days=7)).isoformat()
    last_7 = [p for p in resolved if p.get("date", "") >= cutoff]
    l7_total = len(last_7)
    l7_correct = sum(1 for p in last_7 if p["correct"])

    return {
        "total": total,
        "correct": correct,
        "accuracy": accuracy,
        "last_7": {
            "total": l7_total,
            "correct": l7_correct,
            "accuracy": l7_correct / l7_total if l7_total else 0.0,
        },
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_game_for_pick(pick, games: list[dict]):
    for game in games:
        if (game["home_team"]["full_name"] == pick.home_team and
                game["visitor_team"]["full_name"] == pick.visitor_team):
            return game
    return None


def _fetch_game_result(game_id: int) -> dict | None:
    url = f"{NBA_API_BASE}/games/{game_id}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error fetching result for game {game_id}: {e}")
        return None


def _write_history(history: list[dict]) -> None:
    with open(HISTORY_FILE, "w") as f:
        json.dump({"picks": history}, f, indent=2)
