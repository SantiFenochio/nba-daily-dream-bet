"""
Escalera del Día — selects the best player/stat ladder for the daily Telegram message.

Generates a 3-line progressive ladder bet for the best matchup of the day,
with estimated or real odds at each threshold and a detailed analysis paragraph.
"""

from modules.analyzer import PlayerPick

# Markets eligible for escalera, in preference order (most predictable first)
PREFERRED_MARKETS = ["player_rebounds", "player_assists", "player_points"]

# Spanish names for each market
MARKET_TO_STAT_NAME: dict[str, str] = {
    "player_points":                  "puntos",
    "player_rebounds":                "rebotes",
    "player_assists":                 "asistencias",
    "player_threes":                  "triples",
    "player_steals":                  "robos",
    "player_blocks":                  "tapas",
    "player_points_rebounds_assists": "PRA",
    "player_turnovers":               "pérdidas",
}

# (step_to_L2, step_to_L3_from_L2) — added above the base line
MARKET_STEP_SIZES: dict[str, tuple[float, float]] = {
    "player_rebounds":                  (2.0, 3.0),
    "player_assists":                   (2.0, 3.0),
    "player_points":                    (3.0, 5.0),
    "player_threes":                    (1.0, 2.0),
    "player_steals":                    (1.0, 2.0),
    "player_blocks":                    (1.0, 2.0),
    "player_points_rebounds_assists":   (5.0, 8.0),
    "player_turnovers":                 (1.0, 2.0),
}

# Per-unit probability decay factor when moving to a higher line.
# Calibrated so that a typical 2-unit step halves the implied probability.
MARKET_DECAY_FACTOR: dict[str, float] = {
    "player_rebounds":                  0.72,
    "player_assists":                   0.72,
    "player_points":                    0.82,
    "player_threes":                    0.65,
    "player_steals":                    0.60,
    "player_blocks":                    0.60,
    "player_points_rebounds_assists":   0.80,
    "player_turnovers":                 0.68,
}

# Fixed unit allocation: safest line → middle → extreme
UNITS = (20, 8, 2)

# Confidence weights for pick selection scoring
_CONFIDENCE_WEIGHT = {"Alta": 1.0, "Media": 0.5, "Baja": -0.5, "Riesgosa": -1.5}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _american_to_decimal(american: int) -> float:
    """Convert American odds integer to decimal odds."""
    if american > 0:
        return round(american / 100 + 1, 2)
    return round(100 / abs(american) + 1, 2)


def _round_to_half(value: float) -> float:
    """Round to nearest 0.5 (standard prop line increment)."""
    return round(value * 2) / 2


def _estimate_decimal(base_decimal: float, step: float, market_key: str) -> float:
    """
    Estimate decimal odds for a line `step` units above the base.

    Uses an exponential decay model: each additional unit above the base line
    reduces the implied probability by a market-specific decay factor.
    Calibrated against real bookmaker ladders (e.g. REB 7.5→9.5→12.5).
    """
    decay = MARKET_DECAY_FACTOR.get(market_key, 0.72)
    base_prob = 1.0 / base_decimal
    new_prob = max(0.03, base_prob * (decay ** step))
    raw = 1.0 / new_prob
    # Round nicely: 0.1 precision for small odds, 0.5 for medium, 1.0 for high
    if raw < 5.0:
        return round(raw * 10) / 10
    if raw < 15.0:
        return round(raw * 2) / 2
    return float(round(raw))


def _find_higher_lines(
    player: str,
    market_key: str,
    base_line: float,
    prop_records: list[dict],
) -> list[dict]:
    """
    Search prop_records for Over lines above the base for the same player/market.
    Returns records sorted by line ascending.
    """
    candidates = [
        r for r in prop_records
        if r["player"] == player
        and r["market_key"] == market_key
        and r["side"] == "over"
        and r["line"] > base_line
    ]
    return sorted(candidates, key=lambda r: r["line"])


# ── Pick selection ────────────────────────────────────────────────────────────

def _escalera_score(pick: PlayerPick) -> float:
    """
    Score a pick for escalera suitability.

    Prefers: preferred markets, high ceiling relative to line,
    confident model, no B2B, consistent hit rate.
    """
    # Market preference (rebounds > assists > points > others)
    market_pref = 0.0
    for rank, mkt in enumerate(reversed(PREFERRED_MARKETS)):
        if pick.market_key == mkt:
            market_pref = (rank + 1) * 2.0
            break

    # How much room above the line (ceiling factor)
    ceiling = pick.avg_l5 / max(pick.line, 1.0)

    # Hit rate as confidence proxy (replaces model_prob)
    hit_rate = pick.hit_count_l15 / max(pick.games_l15, 1)
    prob_bonus = hit_rate * 2.0

    # Penalties
    b2b_penalty = -1.5 if pick.is_b2b else 0.0
    conf_bonus  = _CONFIDENCE_WEIGHT.get(pick.confidence, 0.0)

    return market_pref + ceiling + prob_bonus + b2b_penalty + conf_bonus


def _select_best_pick(picks_by_game: dict[str, list[PlayerPick]]) -> PlayerPick | None:
    all_picks = [p for picks in picks_by_game.values() for p in picks]
    if not all_picks:
        return None
    return max(all_picks, key=_escalera_score)


# ── Analysis paragraph ────────────────────────────────────────────────────────

def _generate_analysis(pick: PlayerPick, player_logs: dict[str, list[dict]]) -> str:
    """Build a 4-5 sentence analyst-style analysis for the escalera."""
    stat   = MARKET_TO_STAT_NAME.get(pick.market_key, pick.market)
    player = pick.player
    avg_l15 = pick.avg_l15
    sentences: list[str] = []

    # 1. Opening: consistency signal
    hit_rate_l15 = pick.hit_count_l15 / max(pick.games_l15, 1) * 100
    sentences.append(
        f"{player} es el candidato ideal para la escalera del día: "
        f"superó {pick.line} {stat} en {pick.hit_count_l15} de sus últimos "
        f"{pick.games_l15} partidos ({hit_rate_l15:.0f}%)."
    )

    # 2. Recent production vs line
    hit_rate_l10 = pick.hit_count_l10 / max(pick.games_l10, 1) * 100
    sentences.append(
        f"En los últimos {pick.games_l15} partidos promedia {avg_l15:.1f} {stat} "
        f"y superó la línea base en el {hit_rate_l10:.0f}% de los últimos {pick.games_l10}."
    )

    # 3. Form or streak
    if avg_l15 > 0 and pick.avg_l5 > avg_l15 * 1.08:
        diff_pct = ((pick.avg_l5 / avg_l15) - 1) * 100
        sentences.append(
            f"Su forma reciente es destacada: {pick.avg_l5:.1f} de promedio en los últimos 5 partidos "
            f"(+{diff_pct:.0f}% sobre su L15 de {avg_l15:.1f})."
        )
    elif pick.consecutive_streak >= 3:
        sentences.append(
            f"Lleva {pick.consecutive_streak} partidos consecutivos superando esta línea, "
            f"mostrando una consistencia que refuerza la base de la escalera."
        )
    else:
        edge = round(avg_l15 - pick.line, 1)
        sentences.append(
            f"Promedia {avg_l15:.1f} {stat} en L15, "
            f"con un margen de +{edge} sobre la línea base."
        )

    # 4. Escalera rationale (always last)
    sentences.append(
        f"La estructura 20/8/2 unidades concentra el capital en la línea más probable "
        f"mientras mantiene exposición a las cuotas altas superiores, "
        f"donde el mercado suele subpreciar a jugadores con este techo estadístico."
    )

    return " ".join(sentences)


# ── Main public function ──────────────────────────────────────────────────────

def generate_escalera_data(
    picks_by_game: dict[str, list[PlayerPick]],
    prop_records: list[dict],
    player_logs: dict[str, list[dict]],
) -> dict | None:
    """
    Build the 'Escalera del Día' data dict for the best player/stat combo today.

    Returns None if no suitable pick is available.

    Return structure:
        {
            "player":    str,
            "stat_name": str,          # human-readable Spanish stat name
            "lines": [                 # exactly 3 entries, ascending by line
                {"line": float, "decimal": float, "units": int},
                ...
            ],
            "analysis":  str,          # multi-sentence analysis paragraph
            "game_label": str,
        }
    """
    pick = _select_best_pick(picks_by_game)
    if pick is None:
        return None

    stat_name  = MARKET_TO_STAT_NAME.get(pick.market_key, pick.market)
    step1, step2 = MARKET_STEP_SIZES.get(pick.market_key, (2.0, 4.0))

    base_decimal = (
        _american_to_decimal(pick.price)
        if pick.price  # price=0 is falsy, use default
        else 1.91      # ~American -110 default
    )
    base_line = pick.line

    # Try to find real higher lines from The Odds API data
    higher = _find_higher_lines(pick.player, pick.market_key, base_line, prop_records)

    # ── Line 1: base (from the actual pick) ──────────────────────────────────
    line1 = {"line": base_line, "decimal": base_decimal, "units": UNITS[0]}

    # ── Line 2: real if available, estimated otherwise ────────────────────────
    if higher:
        h = higher[0]
        if h.get("price"):
            dec2 = _american_to_decimal(h["price"])
        else:
            dec2 = _estimate_decimal(base_decimal, h["line"] - base_line, pick.market_key)
        line2 = {"line": h["line"], "decimal": dec2, "units": UNITS[1]}
    else:
        line2_val = _round_to_half(base_line + step1)
        dec2 = _estimate_decimal(base_decimal, step1, pick.market_key)
        line2 = {"line": line2_val, "decimal": dec2, "units": UNITS[1]}

    # ── Line 3: real if available, estimated otherwise ────────────────────────
    if len(higher) >= 2:
        h = higher[1]
        if h.get("price"):
            dec3 = _american_to_decimal(h["price"])
        else:
            dec3 = _estimate_decimal(base_decimal, h["line"] - base_line, pick.market_key)
        line3 = {"line": h["line"], "decimal": dec3, "units": UNITS[2]}
    else:
        line3_val = _round_to_half(base_line + step1 + step2)
        dec3 = _estimate_decimal(base_decimal, step1 + step2, pick.market_key)
        line3 = {"line": line3_val, "decimal": dec3, "units": UNITS[2]}

    analysis = _generate_analysis(pick, player_logs)

    return {
        "player":     pick.player,
        "stat_name":  stat_name,
        "lines":      [line1, line2, line3],
        "analysis":   analysis,
        "game_label": pick.game_label,
    }
