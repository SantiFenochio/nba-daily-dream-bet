import asyncio
import json
import os
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from modules.fetch_games import get_today_games
from modules.fetch_props import get_player_props, parse_props
from modules.fetch_player_stats import get_player_logs, get_injury_statuses
from modules.fetch_context import get_team_context
from modules.fetch_projections import get_player_projections
from modules.analyzer import analyze_player_props
from modules.formatter import format_message
from modules.escalera import generate_escalera_data
from modules.consistency_picks import generate_consistency_picks
from modules.parlay_builder import build_parlays
from modules.history import (
    load_history, save_history, record_picks,
    backtest_yesterday, get_calibration_factors,
)
from modules.telegram_client import send_telegram_message

load_dotenv()

ET = ZoneInfo("America/New_York")
AR = timezone(timedelta(hours=-3))  # Argentina UTC-3 (no DST)


def _get_b2b_team_abbrs(date_str: str) -> set[str]:
    """Return team abbreviations that played yesterday (back-to-back detection)."""
    yesterday = (
        datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)
    ).strftime("%Y-%m-%d")
    yesterday_games = get_today_games(yesterday)
    b2b: set[str] = set()
    for g in yesterday_games:
        b2b.add(g["home_team"]["abbreviation"])
        b2b.add(g["visitor_team"]["abbreviation"])
    if b2b:
        print(f"[main] B2B teams: {', '.join(sorted(b2b))}")
    else:
        print("[main] No back-to-back teams today.")
    return b2b


def _build_game_times(games: list[dict]) -> dict[str, str]:
    """
    Build {game_label: hora_argentina} for upcoming games.
    BallDontLie returns status as ISO UTC for unstarted games.
    """
    result: dict[str, str] = {}
    for g in games:
        label  = f"{g['visitor_team']['full_name']} @ {g['home_team']['full_name']}"
        status = g.get("status", "")
        if status and "T" in status and "Z" in status:
            try:
                dt_utc = datetime.fromisoformat(status.replace("Z", "+00:00"))
                dt_ar  = dt_utc.astimezone(AR)
                result[label] = dt_ar.strftime("%H:%M hs (ARG)")
            except Exception:
                pass
    return result


def _build_team_absent_players(
    injury_statuses: dict[str, str | None],
    player_logs: dict[str, list[dict]],
) -> dict[str, set[str]]:
    """
    Returns {team_abbr: {player_name, ...}} for players confirmed OUT today.
    Used by the analyzer to apply a teammate-absence usage boost.
    Only players listed as "Out" (not Questionable/Day-To-Day) are included,
    to avoid over-boosting for players who end up playing.
    """
    absent: dict[str, set[str]] = {}
    for player, status in injury_statuses.items():
        if not status:
            continue
        # Only confirmed-out players trigger the cascade
        if "out" not in status.lower():
            continue
        logs = player_logs.get(player, [])
        if not logs:
            continue
        team_abbr = logs[0].get("TEAM_ABBREVIATION", "")
        if team_abbr:
            absent.setdefault(team_abbr, set()).add(player)
            print(f"[main] Absent teammate cascade: {player} ({team_abbr}) is OUT")
    return absent


async def main():
    date_str = os.environ.get("DATE_OVERRIDE") or datetime.now(ET).strftime("%Y-%m-%d")
    print(f"[main] Date (ET): {date_str}")

    try:
        # ── 0. Load picks history + backtest yesterday ────────────────────────
        print("[main] Loading picks history...")
        history = load_history()

        # ── 1. Today's games ────────────────────────────────────────────────
        print("[main] Fetching today's NBA games...")
        games = get_today_games(date_str)
        print(f"[main] Games found: {len(games)}")

        if not games:
            print("[main] No games today — notifying Telegram.")
            await send_telegram_message("Sin partidos NBA hoy 🏀")
            return

        # ── 2. Back-to-back detection ────────────────────────────────────────
        print("[main] Checking back-to-back teams...")
        b2b_team_abbrs = _get_b2b_team_abbrs(date_str)

        # ── 3. Team context: pace + DEF_RATING ───────────────────────────────
        print("[main] Fetching team context (pace + DEF_RATING)...")
        team_context = get_team_context()

        # ── 4. Player props + game lines (spread/total) ───────────────────────
        print("[main] Fetching player props and game lines...")
        raw_props, game_lines = get_player_props(games)
        prop_records = parse_props(raw_props, games)

        if not prop_records:
            print("[main] No prop records found — notifying Telegram.")
            await send_telegram_message("No se encontraron props para hoy 🏀")
            return

        # ── 5. Unique players with props ─────────────────────────────────────
        unique_players = list({r["player"] for r in prop_records})
        print(f"[main] Unique players with props: {len(unique_players)}")

        # ── 6. Historical game logs via nba_api ──────────────────────────────
        print("[main] Fetching player game logs (may take ~30s)...")
        player_logs: dict[str, list[dict]] = {}
        for name in unique_players:
            player_logs[name] = get_player_logs(name, last_n=20)

        found = sum(1 for v in player_logs.values() if v)
        print(f"[main] Game logs fetched: {found}/{len(unique_players)} players matched")

        # ── 7. Injury statuses via ESPN API ──────────────────────────────────
        print("[main] Checking injury statuses (ESPN)...")
        injury_statuses = get_injury_statuses(unique_players)

        # Merge manual overrides (data/injury_overrides.json) for today's date
        _overrides_file = Path("data/injury_overrides.json")
        if _overrides_file.exists():
            try:
                _all_overrides = json.loads(_overrides_file.read_text(encoding="utf-8"))
                _today_overrides = _all_overrides.get(date_str, {})
                if _today_overrides:
                    injury_statuses.update(_today_overrides)
                    print(f"[main] Injected {len(_today_overrides)} manual injury overrides for {date_str}")
            except Exception as _e:
                print(f"[main] Could not load injury overrides: {_e}")

        # ── 8. Absent teammate cascade (for usage boost) ──────────────────────
        team_absent_players = _build_team_absent_players(injury_statuses, player_logs)

        # ── 9. SportsData.io player projections ──────────────────────────────
        print("[main] Fetching SportsData.io player projections...")
        projections = get_player_projections(date_str, unique_players)

        # ── 10. Backtest yesterday + compute calibration ──────────────────────
        history, accuracy = backtest_yesterday(history, player_logs)
        calibration = get_calibration_factors(accuracy)

        # ── 11. Analyze ───────────────────────────────────────────────────────
        print("[main] Analyzing player props...")
        shared_args = dict(
            prop_records=prop_records,
            player_logs=player_logs,
            injury_statuses=injury_statuses,
            b2b_team_abbrs=b2b_team_abbrs,
            games=games,
            team_context=team_context,
            game_lines=game_lines,
            team_absent_players=team_absent_players,
            market_ev_multipliers=calibration,
            projections=projections,
        )
        picks_by_game = analyze_player_props(**shared_args)

        # ── Fallback: if EV filter removes everything, send best available ──
        fallback_mode = False
        if not picks_by_game:
            print("[main] No picks above EV threshold — retrying in fallback mode (EV ≥ 0%)...")
            picks_by_game = analyze_player_props(**shared_args, min_ev_threshold=0.0)
            if picks_by_game:
                fallback_mode = True
                total_fb = sum(len(v) for v in picks_by_game.values())
                print(f"[main] Fallback: {total_fb} picks selected (below normal EV threshold)")

        if not picks_by_game:
            print("[main] Truly no picks available — notifying Telegram.")
            await send_telegram_message(
                "🏀 <b>NBA Daily Dream Bet</b>\n"
                "Sin datos suficientes para generar picks hoy."
            )
            return

        # ── 12. Game start times in Argentina timezone ───────────────────────
        game_times = _build_game_times(games)

        # ── 12. Build parlay recommendations ─────────────────────────────────
        parlays = build_parlays(picks_by_game, n_parlays=5)
        print(f"[main] Parlays built: {len(parlays)}")

        # ── 12b. Escalera del Día ─────────────────────────────────────────────
        escalera_data = generate_escalera_data(picks_by_game, prop_records, player_logs)
        print(f"[main] Escalera: {escalera_data['player'] if escalera_data else 'none'}")

        # ── 12c. Picks de Consistencia ────────────────────────────────────────
        print("[main] Building consistency picks...")
        consistency = generate_consistency_picks(player_logs, prop_records)
        print(f"[main] Consistency picks: {len(consistency)}")

        # ── 13. Record today's picks in history (hit=None, filled tomorrow) ──
        history = record_picks(date_str, picks_by_game, history)
        save_history(history)

        # ── 14. Format and send ───────────────────────────────────────────────
        print("[main] Formatting and sending message...")
        message = format_message(
            picks_by_game,
            game_times=game_times,
            fallback_mode=fallback_mode,
            parlays=parlays,
            accuracy=accuracy,
            escalera_data=escalera_data,
            consistency_picks=consistency or None,
        )
        await send_telegram_message(message)
        print("[main] Done.")

    except Exception:
        tb = traceback.format_exc()
        print(f"[main] UNHANDLED EXCEPTION:\n{tb}")
        safe_tb = (
            tb[-600:]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        try:
            await send_telegram_message(
                f"<b>NBA Bot Error</b>\n<code>{safe_tb}</code>"
            )
        except Exception as tg_exc:
            print(f"[main] Also failed to send error to Telegram: {tg_exc}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
