"""
analyzer.py — Simple NBA prop analyzer.

Logic:
  1. For each player/prop, compute L15 hit rate, L15/L5 avg, L10 min, streak.
  2. Assign confidence: Alta / Media / Baja based on hit rate + avg edge over line.
  3. Filter: skip OUT players, skip < 20 min avg, skip Baja if enough picks.
  4. Cap at MAX_TOTAL_PICKS (15), MAX_PICKS_PER_GAME (4) per game.
"""

from dataclasses import dataclass, field
from modules.fetch_player_stats import get_stat_value, parse_minutes
from modules.fetch_props import MARKET_LABELS

# ── Config ────────────────────────────────────────────────────────────────────
MIN_MINUTES_AVG    = 20.0  # players averaging < 20 min are discarded
MIN_GAMES_REQUIRED = 5     # minimum valid games to analyze
MAX_PICKS_PER_GAME = 4     # top N picks per game
MAX_TOTAL_PICKS    = 15    # hard cap across all games

# Confidence thresholds
ALTA_HIT_RATE  = 0.80   # >= 80% L15 hit rate
ALTA_EDGE      = 0.10   # avg L15 >= line * 1.10 (+10%)
MEDIA_HIT_RATE = 0.67   # >= 67% L15 hit rate
MEDIA_EDGE     = 0.05   # avg L15 >= line * 1.05 (+5%)


@dataclass
class PlayerPick:
    player: str
    game_label: str
    market_key: str
    market: str
    side: str
    line: float
    price: int

    avg_l15: float           # arithmetic mean of last 15 valid games
    avg_l5: float            # arithmetic mean of last 5 valid games
    hit_count_l15: int       # times player exceeded line in last 15
    games_l15: int           # valid games counted in last 15
    hit_count_l10: int       # times player exceeded line in last 10
    games_l10: int           # valid games counted in last 10
    hit_count_l5: int        # times player exceeded line in last 5
    games_l5: int            # valid games counted in last 5
    min_l10: float           # minimum value in last 10 valid games
    consecutive_streak: int  # current consecutive hits from most recent game

    confidence: str          # "Alta", "Media", "Baja"
    is_b2b: bool
    score: float = 0.0

    injury_status: str | None = None

    # Compatibility with history.py (not computed in simple mode)
    ev_pct: float = 0.0


def _compute_stats(logs: list[dict], market_key: str, line: float) -> dict | None:
    """
    Compute all needed stats from game logs for a given prop line.
    Returns None if not enough valid games.
    """
    values: list[float] = []
    for g in logs:
        val = get_stat_value(g, market_key)
        if val is not None:
            values.append(val)

    if len(values) < MIN_GAMES_REQUIRED:
        return None

    # L15
    vals_l15 = values[:15]
    hits_l15 = sum(1 for v in vals_l15 if v > line)
    avg_l15  = sum(vals_l15) / len(vals_l15)

    # L10
    vals_l10 = values[:10]
    hits_l10 = sum(1 for v in vals_l10 if v > line)
    min_l10  = min(vals_l10) if vals_l10 else 0.0

    # L5
    vals_l5  = values[:5]
    hits_l5  = sum(1 for v in vals_l5 if v > line)
    avg_l5   = sum(vals_l5) / len(vals_l5)

    # Consecutive streak (from most recent game going back)
    streak = 0
    for v in values:
        if v > line:
            streak += 1
        else:
            break

    return {
        "avg_l15":            avg_l15,
        "avg_l5":             avg_l5,
        "hit_count_l15":      hits_l15,
        "games_l15":          len(vals_l15),
        "hit_count_l10":      hits_l10,
        "games_l10":          len(vals_l10),
        "hit_count_l5":       hits_l5,
        "games_l5":           len(vals_l5),
        "min_l10":            min_l10,
        "consecutive_streak": streak,
    }


def _get_avg_minutes(logs: list[dict]) -> float:
    minutes: list[float] = []
    for g in logs[:15]:
        val = g.get("MIN")
        if val is not None:
            m = parse_minutes(val)
            if m > 0:
                minutes.append(m)
    if not minutes:
        return 0.0
    return sum(minutes) / len(minutes)


def _assign_confidence(hit_rate_l15: float, avg_l15: float, line: float) -> str:
    edge_ratio = avg_l15 / line if line > 0 else 1.0
    if hit_rate_l15 >= ALTA_HIT_RATE and edge_ratio >= (1.0 + ALTA_EDGE):
        return "Alta"
    if hit_rate_l15 >= MEDIA_HIT_RATE and edge_ratio >= (1.0 + MEDIA_EDGE):
        return "Media"
    return "Baja"


def analyze_player_props(
    prop_records: list[dict],
    player_logs: dict[str, list[dict]],
    injury_statuses: dict[str, str | None],
    b2b_team_abbrs: set[str],
    games: list[dict],
    team_context=None,
    game_lines=None,
    team_absent_players=None,
    market_ev_multipliers=None,
    min_ev_threshold=None,
) -> dict[str, list[PlayerPick]]:
    """
    Analyze player props and return picks_by_game.

    Simple logic:
      - Only Over props analyzed
      - Confirmed OUT players skipped
      - Players averaging < 20 min skipped
      - Confidence: Alta / Media / Baja by hit rate + edge
      - Back-to-back: lowers confidence one level, adds warning
      - Returns up to MAX_TOTAL_PICKS, MAX_PICKS_PER_GAME per game
      - Alta and Media only; Baja included only if < 5 picks total
    """
    # Build game_label → team abbreviations map for B2B detection
    game_team_abbrs: dict[str, set[str]] = {}
    for g in games:
        label = f"{g['visitor_team']['full_name']} @ {g['home_team']['full_name']}"
        game_team_abbrs[label] = {
            g["home_team"]["abbreviation"],
            g["visitor_team"]["abbreviation"],
        }

    candidates: list[PlayerPick] = []
    seen_props: set[tuple] = set()

    for rec in prop_records:
        # Case-insensitive check — Odds API returns "Over" (capital O)
        if rec.get("side", "").lower() != "over":
            continue

        player     = rec["player"]
        market_key = rec["market_key"]
        line       = float(rec["line"])
        price      = rec.get("price") or -110
        game_label = rec.get("game_label", "")
        market     = MARKET_LABELS.get(market_key, market_key)

        # Deduplicate (player, market, line)
        prop_key = (player, market_key, line)
        if prop_key in seen_props:
            continue
        seen_props.add(prop_key)

        # Skip confirmed OUT
        inj = injury_statuses.get(player)
        if inj and "out" in inj.lower():
            continue

        logs = player_logs.get(player, [])
        if not logs:
            continue

        # Skip low-minute players
        if _get_avg_minutes(logs) < MIN_MINUTES_AVG:
            continue

        stats = _compute_stats(logs, market_key, line)
        if stats is None:
            continue

        hit_rate_l15 = stats["hit_count_l15"] / stats["games_l15"]
        confidence   = _assign_confidence(hit_rate_l15, stats["avg_l15"], line)

        # B2B: downgrade confidence one level
        is_b2b = bool(game_team_abbrs.get(game_label, set()) & b2b_team_abbrs)
        if is_b2b:
            if confidence == "Alta":
                confidence = "Media"
            elif confidence == "Media":
                confidence = "Baja"

        # Score: hit rate primary, edge secondary
        edge_ratio = (stats["avg_l15"] / line) if line > 0 else 1.0
        score = hit_rate_l15 * 0.70 + min(edge_ratio - 1.0, 0.50) * 0.30

        pick = PlayerPick(
            player=player,
            game_label=game_label,
            market_key=market_key,
            market=market,
            side="over",
            line=line,
            price=price,
            avg_l15=round(stats["avg_l15"], 1),
            avg_l5=round(stats["avg_l5"], 1),
            hit_count_l15=stats["hit_count_l15"],
            games_l15=stats["games_l15"],
            hit_count_l10=stats["hit_count_l10"],
            games_l10=stats["games_l10"],
            hit_count_l5=stats["hit_count_l5"],
            games_l5=stats["games_l5"],
            min_l10=round(stats["min_l10"], 1),
            consecutive_streak=stats["consecutive_streak"],
            confidence=confidence,
            is_b2b=is_b2b,
            injury_status=inj,
            score=round(score, 4),
            ev_pct=0.0,
        )
        candidates.append(pick)

    # Sort by score descending
    candidates.sort(key=lambda p: -p.score)

    # Separate by confidence
    alta_picks  = [p for p in candidates if p.confidence == "Alta"]
    media_picks = [p for p in candidates if p.confidence == "Media"]
    baja_picks  = [p for p in candidates if p.confidence == "Baja"]

    # Alta + Media first; include Baja only if too few picks
    pool = alta_picks + media_picks
    if len(pool) < 5:
        pool = pool + baja_picks

    # Apply per-game and total caps
    picks_by_game: dict[str, list[PlayerPick]] = {}
    total = 0
    for pick in pool:
        if total >= MAX_TOTAL_PICKS:
            break
        game_picks = picks_by_game.setdefault(pick.game_label, [])
        if len(game_picks) >= MAX_PICKS_PER_GAME:
            continue
        game_picks.append(pick)
        total += 1

    return picks_by_game
