import asyncio
from dotenv import load_dotenv
from modules.fetch_games import get_today_games
from modules.fetch_stats import get_team_stats
from modules.fetch_props import get_player_props, get_game_odds
from modules.fetch_schedule import get_rest_days
from modules.fetch_recent_form import get_recent_form
from modules.fetch_h2h import get_h2h_records
from modules.analyzer import analyze_games
from modules.formatter import format_message
from modules.telegram_client import send_telegram_message
from modules.history import load_history, save_picks, update_results, compute_stats

load_dotenv()


async def main():
    # --- Results: update yesterday's picks before doing anything else ---
    print("Updating results for past picks...")
    history = load_history()
    history, updated = update_results(history)
    if updated:
        print(f"  Updated {updated} pick result(s).")
    history_stats = compute_stats(history)

    # --- Fetch today's games ---
    print("Fetching today's NBA games...")
    games = get_today_games()

    if not games:
        print("No games today.")
        return

    print(f"Found {len(games)} games. Fetching data...")

    stats = get_team_stats(games)
    props = get_player_props(games)
    game_odds = get_game_odds(games)
    rest_info = get_rest_days(games)

    print("Fetching recent form (last ~20 games)...")
    recent_form = get_recent_form(games)

    print("Fetching H2H records (last 2 seasons)...")
    h2h = get_h2h_records(games)

    print("Analyzing matchups...")
    picks = analyze_games(games, stats, props, game_odds, rest_info,
                          recent_form=recent_form, h2h=h2h)

    print("Formatting message...")
    message = format_message(picks, history_stats=history_stats)

    print("Sending to Telegram...")
    await send_telegram_message(message)

    print("Saving today's picks to history...")
    save_picks(picks, games)

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
