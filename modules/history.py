"""
history.py — Sistema de aprendizaje del bot

Ciclo de vida:
  1. Al iniciar: carga historial, backtestea picks de ayer con ESPN data real
  2. Al terminar: guarda los picks de hoy (hit=None, a verificar mañana)
  3. Calcula factores de calibración por mercado (basado en hit rate histórico)
  4. El analyzer usa esos factores para ajustar umbrales de EV

Almacenamiento: data/picks_history.json en el repo (el workflow hace git push).
Se mantienen solo los últimos 60 días para no crecer indefinidamente.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from modules.fetch_player_stats import get_stat_value

HISTORY_FILE = Path("data/picks_history.json")
MAX_DAYS_KEPT = 60


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {}
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[history] Error loading history: {e}")
        return {}


def save_history(history: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Purge entries older than MAX_DAYS_KEPT
    cutoff = (date.today() - timedelta(days=MAX_DAYS_KEPT)).isoformat()
    history = {k: v for k, v in history.items() if k >= cutoff}
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        print(f"[history] History saved ({len(history)} days)")
    except Exception as e:
        print(f"[history] Error saving history: {e}")


# ── Record today's picks (hit=None, filled tomorrow) ─────────────────────────

def record_picks(date_str: str, picks_by_game: dict, history: dict) -> dict:
    """Add today's picks to history with hit=None (to be resolved next run)."""
    today_records = []
    for game, picks in picks_by_game.items():
        for pick in picks:
            today_records.append({
                "player":     pick.player,
                "market":     pick.market,       # human label e.g. "Puntos"
                "market_key": _market_key(pick), # internal key e.g. "player_points"
                "side":       pick.side,
                "line":       float(pick.line),
                "ev_pct":     round(pick.ev_pct, 2),
                "model_prob": round(pick.model_prob, 4),
                "confidence": pick.confidence,
                "game":       game,
                "hit":        None,
            })
    history[date_str] = today_records
    print(f"[history] Recorded {len(today_records)} picks for {date_str}")
    return history


def _market_key(pick) -> str:
    """Recover market_key from the PlayerPick's market label."""
    from modules.fetch_props import MARKET_LABELS
    label_to_key = {v: k for k, v in MARKET_LABELS.items()}
    return label_to_key.get(pick.market, pick.market)


# ── Backtest yesterday's picks against actual ESPN data ───────────────────────

def backtest_yesterday(
    history: dict,
    player_logs: dict[str, list[dict]],
) -> tuple[dict, dict | None]:
    """
    Fill in 'hit' for yesterday's picks using actual game logs from ESPN cache.
    Returns (updated_history, accuracy_report | None).
    """
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    if yesterday not in history:
        print(f"[history] No picks recorded for {yesterday} — skipping backtest")
        return history, None

    yesterday_picks = history[yesterday]
    pending = [p for p in yesterday_picks if p["hit"] is None]
    if not pending:
        print(f"[history] Yesterday's picks already resolved — skipping backtest")
        return history, compute_accuracy(history)

    resolved = 0
    dnp      = 0
    for pick in yesterday_picks:
        if pick["hit"] is not None:
            continue
        player = pick["player"]
        market_key = pick.get("market_key") or pick.get("market")
        logs = player_logs.get(player, [])
        # Find the game from yesterday
        game_log = next((g for g in logs if g.get("GAME_DATE") == yesterday), None)
        if game_log is None:
            dnp += 1
            continue  # DNP or no data — leave as None

        actual = get_stat_value(game_log, market_key)
        if actual is None:
            continue

        line = pick["line"]
        side = pick["side"].lower()
        if side == "over":
            pick["hit"] = bool(actual > line)
        elif side == "under":
            pick["hit"] = bool(actual < line)
        resolved += 1

    history[yesterday] = yesterday_picks
    print(f"[history] Backtest {yesterday}: {resolved} resolved, {dnp} DNP/no-data, "
          f"{len(pending) - resolved - dnp} unmatched")

    return history, compute_accuracy(history)


# ── Accuracy computation ──────────────────────────────────────────────────────

def compute_accuracy(history: dict) -> dict:
    """
    Returns accuracy stats:
        {
          "overall":            {"hits": N, "total": N, "rate": 0.xx},
          "player_points":      {...},
          "conf_Alta":          {...},
          ...
          "yesterday":          {"hits": N, "total": N, "rate": 0.xx, "date": "YYYY-MM-DD"},
          "last_7_days":        {...},
        }
    """
    stats: dict[str, dict] = defaultdict(lambda: {"hits": 0, "total": 0})
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    last_7    = (date.today() - timedelta(days=7)).isoformat()

    for date_str, picks in history.items():
        for pick in picks:
            if pick["hit"] is None:
                continue
            mk   = pick.get("market_key") or pick.get("market", "unknown")
            conf = pick.get("confidence", "unknown")
            hit  = bool(pick["hit"])

            for key in ("overall", mk, f"conf_{conf}"):
                stats[key]["total"] += 1
                if hit:
                    stats[key]["hits"] += 1

            if date_str == yesterday:
                stats["yesterday"]["total"] += 1
                if hit:
                    stats["yesterday"]["hits"] += 1

            if date_str >= last_7:
                stats["last_7_days"]["total"] += 1
                if hit:
                    stats["last_7_days"]["hits"] += 1

    result = {}
    for key, data in stats.items():
        if data["total"] > 0:
            result[key] = {
                "hits":  data["hits"],
                "total": data["total"],
                "rate":  round(data["hits"] / data["total"], 3),
            }
    if "yesterday" in result:
        result["yesterday"]["date"] = yesterday

    return result


# ── Calibration factors ───────────────────────────────────────────────────────

def get_calibration_factors(accuracy: dict | None) -> dict[str, float]:
    """
    Returns {market_key: ev_multiplier} to adjust MIN_EV_BY_MARKET thresholds.

    Rules (minimum 20 resolved picks per market to activate):
      hit_rate ≥ 0.65  → multiplier 0.80  (modelo bueno → bajar umbral, más picks)
      hit_rate ≥ 0.58  → multiplier 0.90
      hit_rate ≤ 0.38  → multiplier 1.35  (modelo malo → subir umbral, más selectivo)
      hit_rate ≤ 0.46  → multiplier 1.15
      otherwise        → multiplier 1.00  (sin cambio)
    """
    if not accuracy:
        return {}

    factors: dict[str, float] = {}
    market_keys = [
        "player_points", "player_rebounds", "player_assists",
        "player_points_rebounds_assists", "player_threes",
        "player_steals", "player_blocks", "player_turnovers",
    ]
    for mk in market_keys:
        data = accuracy.get(mk)
        if not data or data["total"] < 20:
            continue  # not enough data
        rate = data["rate"]
        if rate >= 0.65:
            factors[mk] = 0.80
        elif rate >= 0.58:
            factors[mk] = 0.90
        elif rate <= 0.38:
            factors[mk] = 1.35
        elif rate <= 0.46:
            factors[mk] = 1.15

    if factors:
        print(f"[history] Calibration factors active: "
              + ", ".join(f"{k.split('_',1)[1]}: x{v}" for k, v in factors.items()))

    return factors
