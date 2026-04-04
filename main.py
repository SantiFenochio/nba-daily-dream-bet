import asyncio
from dotenv import load_dotenv
from modules.fetch_games import get_today_games
from modules.fetch_stats import get_team_stats
from modules.fetch_props import get_player_props, get_game_odds
from modules.fetch_schedule import get_rest_days
from modules.analyzer import analyze_games
from modules.formatter import format_message
from modules.telegram_client import send_telegram_message

load_dotenv()


async def main():
    print("Fetching today's NBA games...")
    games = get_today_games()

    if not games:
        print("No games today.")
        return

    print(f"Found {len(games)} games. Fetching stats...")
    stats = get_team_stats(games)

    print("Fetching player props...")
    props = get_player_props(games)

    print("Fetching game odds (spread/totals)...")
    game_odds = get_game_odds(games)

    print("Fetching rest/schedule info...")
    rest_info = get_rest_days(games)

    print("Analyzing matchups...")
    picks = analyze_games(games, stats, props, game_odds, rest_info)

    print("Formatting message...")
    message = format_message(picks)

    print("Sending to Telegram...")
    await send_telegram_message(message)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
