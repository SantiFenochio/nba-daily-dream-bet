import asyncio
import os
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from modules.fetch_games import get_today_games
from modules.fetch_props import get_player_props, parse_props
from modules.fetch_player_stats import get_player_logs, get_injury_statuses
from modules.analyzer import analyze_player_props
from modules.formatter import format_message
from modules.telegram_client import send_telegram_message

load_dotenv()

ET = ZoneInfo("America/New_York")


def _get_b2b_team_abbrs(date_str: str) -> set[str]:
    """Return set of team abbreviations that played yesterday (back-to-back detection)."""
    yesterday = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_games = get_today_games(yesterday)
    b2b = set()
    for g in yesterday_games:
        b2b.add(g["home_team"]["abbreviation"])
        b2b.add(g["visitor_team"]["abbreviation"])
    if b2b:
        print(f"[main] B2B teams today (played yesterday): {', '.join(sorted(b2b))}")
    else:
        print("[main] No back-to-back teams detected.")
    return b2b


async def main():
    date_str = datetime.now(ET).strftime("%Y-%m-%d")
    print(f"[main] Date (ET): {date_str}")

    try:
        # 1. Today's games
        print("[main] Fetching today's NBA games...")
        games = get_today_games(date_str)
        print(f"[main] Games found: {len(games)}")

        if not games:
            print("[main] No games today — notifying Telegram.")
            await send_telegram_message("Sin partidos NBA hoy 🏀")
            return

        # 2. Back-to-back detection
        print("[main] Checking back-to-back teams...")
        b2b_team_abbrs = _get_b2b_team_abbrs(date_str)

        # 3. Player props from The Odds API
        print("[main] Fetching player props...")
        raw_props = get_player_props(games)
        prop_records = parse_props(raw_props, games)

        if not prop_records:
            print("[main] No prop records found — notifying Telegram.")
            await send_telegram_message("No se encontraron props para hoy 🏀")
            return

        # 4. Unique players that have props
        unique_players = list({r["player"] for r in prop_records})
        print(f"[main] Unique players with props: {len(unique_players)}")

        # 5. Fetch historical game logs for each player (nba_api)
        print("[main] Fetching player game logs from nba_api (this may take ~30s)...")
        player_logs: dict[str, list[dict]] = {}
        for name in unique_players:
            player_logs[name] = get_player_logs(name, last_n=20)

        found = sum(1 for v in player_logs.values() if v)
        print(f"[main] Game logs fetched: {found}/{len(unique_players)} players matched")

        # 6. Injury statuses via Tank01
        print("[main] Checking injury statuses (Tank01)...")
        injury_statuses = get_injury_statuses(unique_players)

        # 7. Analyze
        print("[main] Analyzing player props...")
        picks_by_game = analyze_player_props(
            prop_records=prop_records,
            player_logs=player_logs,
            injury_statuses=injury_statuses,
            b2b_team_abbrs=b2b_team_abbrs,
            games=games,
        )

        if not picks_by_game:
            print("[main] No picks generated — notifying Telegram.")
            await send_telegram_message(
                "No se pudieron generar picks hoy \\(insuficientes datos históricos\\)\\."
            )
            return

        # 8. Format and send
        print("[main] Formatting and sending message...")
        message = format_message(picks_by_game)
        await send_telegram_message(message)
        print("[main] Done.")

    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[main] UNHANDLED EXCEPTION:\n{tb}")
        safe_tb = tb[-600:].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        error_text = f"<b>NBA Bot Error</b>\n<code>{safe_tb}</code>"
        try:
            await send_telegram_message(error_text)
        except Exception as tg_exc:
            print(f"[main] Also failed to send error to Telegram: {tg_exc}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
