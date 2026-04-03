import asyncio
import os
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from modules.fetch_games import get_today_games
from modules.fetch_stats import get_team_stats
from modules.fetch_props import get_player_props
from modules.analyzer import analyze_games
from modules.formatter import format_message
from modules.telegram_client import send_telegram_message

load_dotenv()

ET = ZoneInfo("America/New_York")


async def main():
    date_str = datetime.now(ET).strftime("%Y-%m-%d")
    print(f"[main] Date (ET): {date_str}")

    try:
        print("[main] Fetching today's NBA games...")
        games = get_today_games(date_str)
        print(f"[main] Games found: {len(games)}")

        if not games:
            print("[main] No games today — notifying Telegram.")
            await send_telegram_message("Sin partidos NBA hoy 🏀")
            return

        print(f"[main] Fetching team stats for {len(games)} games...")
        stats = get_team_stats(games)
        print(f"[main] Stats fetched for {len(stats)} teams.")

        print("[main] Fetching player props...")
        props = get_player_props(games)
        total_entries = sum(len(v) for v in props.values())
        print(f"[main] Props result: {len(props)} games matched, {total_entries} bookmaker entries total.")

        if os.getenv("ODDS_API_KEY") and not props:
            print("[main] ODDS_API_KEY set but no props returned — notifying Telegram.")
            await send_telegram_message("No se encontraron props para hoy 🏀")
            return

        print("[main] Analyzing matchups...")
        picks = analyze_games(games, stats, props)
        print(f"[main] Picks generated: {len(picks)}")

        print("[main] Formatting and sending message...")
        message = format_message(picks)
        await send_telegram_message(message)
        print("[main] Done. ✓")

    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[main] UNHANDLED EXCEPTION:\n{tb}")
        error_text = f"❌ *NBA Bot Error*\n```\n{tb[-800:]}\n```"
        try:
            await send_telegram_message(error_text)
        except Exception as tg_exc:
            print(f"[main] Also failed to send error to Telegram: {tg_exc}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
