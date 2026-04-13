"""
analyzer.py — Simple NBA prop analyzer.

Logic:
  1. For each player/prop, compute L15 hit rate, L15/L5 avg, L10 min, streak.
  2. Assign confidence: Alta / Media / Baja based on hit rate + avg edge over line.
  3. Apply matchup multiplier from team_context (opponent def quality + pace).
  4. Apply projection multiplier from SportsData.io projections if available.
  5. Filter: skip OUT players, skip < 20 min avg, skip Baja if enough picks.
  6. Cap at MAX_TOTAL_PICKS (15), MAX_PICKS_PER_GAME (4) per game.
"""

import statistics
from collections import Counter
from dataclasses import dataclass
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

# Team context defaults (league averages 2025-26)
LEAGUE_AVG_OPP_PTS = 114.0
LEAGUE_AVG_PACE    = 99.5

# ── Steals market filter ──────────────────────────────────────────────────────
STEALS_OVER_HIGH_LINE  = 1.5   # Over >= this line requires stricter thresholds
STEALS_MIN_HIT_L15     = 0.73  # min L15 hit rate for steals Over >= 1.5
STEALS_MIN_HIT_L5      = 0.60  # min L5 hit rate for steals Over >= 1.5

# ── Minutes variability penalty ───────────────────────────────────────────────
MIN_CV_PENALTY          = 0.40  # coeff. of variation above this → penalize
MIN_CV_SCORE_FACTOR     = 0.88  # score multiplier for high-variability players

# ── Blowout risk for bench players ────────────────────────────────────────────
BLOWOUT_SPREAD_THRESHOLD = 12.0  # abs(spread) > this = blowout risk
BLOWOUT_BENCH_MIN_AVG    = 28.0  # players averaging < this are role/bench players
BLOWOUT_SCORE_FACTOR     = 0.90  # score penalty for bench in blowout games

# ── Multi-market same-player boost ────────────────────────────────────────────
MULTI_PICK_MIN_COUNT  = 2     # minimum number of picks from same player to boost
MULTI_PICK_SCORE_BOOST = 1.06  # score multiplier when player hits 2+ markets

# ── Per-player stacking cap ───────────────────────────────────────────────────
MAX_PICKS_PER_PLAYER = 2      # max picks per player in final output (avoid cascade failures)

# ── Day-To-Day / Questionable: downgrade confidence ──────────────────────────
DTD_KEYWORDS = {"day-to-day", "questionable"}

# ── L5 vs L15 hit-rate divergence penalty (recent underperformance) ───────────
L5_DIVERGENCE_SEVERE     = 0.60   # L5_hr / L15_hr below this → severe penalty
L5_DIVERGENCE_MILD       = 0.75   # L5_hr / L15_hr below this → mild penalty
L5_DIVERGENCE_SCORE_SEVERE = 0.87
L5_DIVERGENCE_SCORE_MILD   = 0.93

# ── Blowout risk: extends to assists markets for ALL players ──────────────────
# Even stars generate fewer assists when a game is a blowout (game flow changes)
BLOWOUT_STAR_ASSISTS_FACTOR = 0.93

# ── Assists-based markets (most sensitive to game flow / blowout risk) ────────
ASSISTS_MARKETS = {
    "player_assists",
    "player_points_assists",
    "player_rebounds_assists",
    "player_points_rebounds_assists",
}

# ── Low-line triples penalty (Over ≤ 1.0 = high noise, low signal) ───────────
TRIPLES_LOW_LINE_THRESHOLD = 1.0
TRIPLES_LOW_LINE_FACTOR    = 0.88

# Markets where opponent defensive quality matters (offensive props)
OFFENSIVE_MARKETS = {
    "player_points", "player_rebounds", "player_assists",
    "player_points_rebounds_assists", "player_threes",
    "player_points_assists", "player_points_rebounds",
    "player_rebounds_assists",
}

# SportsData.io projection field per market key
_PROJ_STAT_MAP = {
    "player_points":                    "pts",
    "player_rebounds":                  "reb",
    "player_assists":                   "ast",
    "player_steals":                    "stl",
    "player_blocks":                    "blk",
    "player_turnovers":                 "to",
    "player_threes":                    "threes",
    "player_points_rebounds_assists":   "pra",
}


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
    is_dtd: bool = False     # Day-To-Day / Questionable (flagged for parlay exclusion)
    score: float = 0.0

    injury_status: str | None = None

    # EV% = (model_prob / fair_implied - 1)*100 | model_prob = 0.65*L15 + 0.35*L5
    ev_pct: float = 0.0
    model_prob: float = 0.0


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


def _get_proj_stat(proj: dict, market_key: str) -> float | None:
    """Return the projected value for a given market key from SportsData projections."""
    key = _PROJ_STAT_MAP.get(market_key)
    if key and key in proj:
        try:
            return float(proj[key])
        except (TypeError, ValueError):
            pass
    return None


# ── EV helpers ────────────────────────────────────────────────────────────────

def _american_to_implied(price: int) -> float:
    """Convert American odds to raw implied probability (with vig)."""
    if price >= 100:
        return 100.0 / (price + 100.0)
    return abs(price) / (abs(price) + 100.0)


def _devig_implied(price_over: int, price_under: int | None) -> float:
    """Two-sided devig: returns fair implied probability for the Over side."""
    p_over = _american_to_implied(price_over)
    if price_under is not None:
        p_under = _american_to_implied(price_under)
        total = p_over + p_under
        if total > 0:
            return p_over / total
    return p_over  # fallback: single-sided (no devig available)


def _compute_model_prob(stats: dict) -> float:
    """
    Blend L15 (stable) and L5 (recent form) hit rates into a model probability.
    Weights: 65% L15, 35% L5.
    """
    hr_l15 = stats["hit_count_l15"] / stats["games_l15"] if stats["games_l15"] > 0 else 0.0
    hr_l5  = stats["hit_count_l5"]  / stats["games_l5"]  if stats["games_l5"]  > 0 else 0.0
    return round(hr_l15 * 0.65 + hr_l5 * 0.35, 4)


def _compute_ev_pct(model_prob: float, price: int, opposite_price: int | None) -> float:
    """
    EV% = (model_prob / fair_implied_prob - 1) * 100
    Positive EV means the model sees more value than the market implies.
    """
    fair_implied = _devig_implied(price, opposite_price)
    if fair_implied <= 0:
        return 0.0
    return round((model_prob / fair_implied - 1) * 100, 2)


def _get_minutes_cv(logs: list[dict]) -> float:
    """
    Returns coefficient of variation (std/mean) of minutes in last 15 games.
    High CV = inconsistent minutes = high-variability bench/role player.
    """
    minutes: list[float] = []
    for g in logs[:15]:
        val = g.get("MIN")
        if val is not None:
            m = parse_minutes(val)
            if m > 0:
                minutes.append(m)
    if len(minutes) < 5:
        return 0.0
    avg = sum(minutes) / len(minutes)
    if avg <= 0:
        return 0.0
    return statistics.stdev(minutes) / avg


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
    projections=None,
    claude_refinements: dict[str, dict] | None = None,
) -> dict[str, list[PlayerPick]]:
    """
    Analyze player props and return picks_by_game.

      - Only Over props analyzed
      - Confirmed OUT players skipped (also from SportsData projections)
      - Players averaging < 20 min skipped
      - Confidence: Alta / Media / Baja by hit rate + edge
      - Back-to-back: lowers confidence one level
      - Matchup multiplier: opponent defensive quality + game pace (team_context)
      - Projection multiplier: SportsData.io projected stat vs line (projections)
      - Returns up to MAX_TOTAL_PICKS, MAX_PICKS_PER_GAME per game
    """
    # Build game_label → {team_abbr, ...} map
    game_team_abbrs: dict[str, set[str]] = {}
    game_label_to_id: dict[str, str] = {}
    for g in games:
        label = f"{g['visitor_team']['full_name']} @ {g['home_team']['full_name']}"
        game_team_abbrs[label] = {
            g["home_team"]["abbreviation"],
            g["visitor_team"]["abbreviation"],
        }
        game_label_to_id[label] = g["id"]

    ctx_hits = 0   # count picks that received a context adjustment
    proj_hits = 0  # count picks that received a projection adjustment

    candidates: list[PlayerPick] = []
    seen_props: set[tuple] = set()

    for rec in prop_records:
        # Case-insensitive check — Odds API returns "Over" (capital O)
        if rec.get("side", "").lower() != "over":
            continue

        player         = rec["player"]
        market_key     = rec["market_key"]
        line           = float(rec["line"])
        price          = rec.get("price") or -110
        opposite_price = rec.get("opposite_price")
        game_label     = rec.get("game_label", "")
        market         = MARKET_LABELS.get(market_key, market_key)

        # Deduplicate (player, market, line)
        prop_key = (player, market_key, line)
        if prop_key in seen_props:
            continue
        seen_props.add(prop_key)

        # Skip confirmed OUT (ESPN + manual overrides already in injury_statuses)
        inj = injury_statuses.get(player)
        if inj and "out" in inj.lower():
            continue

        # Also skip if SportsData marks player as OUT (extra layer)
        if projections and player in projections:
            if (projections[player].get("inj_status") or "").lower() == "out":
                continue

        # Flag Day-To-Day / Questionable (used later to downgrade confidence)
        is_dtd = bool(inj and any(kw in inj.lower() for kw in DTD_KEYWORDS))

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
        hr_l5        = stats["hit_count_l5"] / stats["games_l5"] if stats["games_l5"] > 0 else 0.0
        confidence   = _assign_confidence(hit_rate_l15, stats["avg_l15"], line)

        # ── Steals Over high-line: extra strict (market muy volátil) ──────────
        if market_key == "player_steals" and line >= STEALS_OVER_HIGH_LINE:
            if hit_rate_l15 < STEALS_MIN_HIT_L15 or hr_l5 < STEALS_MIN_HIT_L5:
                continue

        # ── Low-line triples: alta varianza, señal débil ────────────────────
        if market_key == "player_threes" and line <= TRIPLES_LOW_LINE_THRESHOLD:
            # No se elimina, pero se penaliza en score para salir más abajo
            pass  # penalty applied below after base score

        # ── Day-To-Day: downgrade confidence one level + score penalty ────────
        if is_dtd:
            if confidence == "Alta":
                confidence = "Media"
            elif confidence == "Media":
                confidence = "Baja"

        # B2B: downgrade confidence one level
        is_b2b = bool(game_team_abbrs.get(game_label, set()) & b2b_team_abbrs)
        if is_b2b:
            if confidence == "Alta":
                confidence = "Media"
            elif confidence == "Media":
                confidence = "Baja"

        # Base score: hit rate primary, edge secondary
        edge_ratio = (stats["avg_l15"] / line) if line > 0 else 1.0
        score = hit_rate_l15 * 0.70 + min(edge_ratio - 1.0, 0.50) * 0.30

        # ── L5 vs L15 divergence: recent underperformance is a warning signal ──
        # If a player is doing worse recently vs their historical rate, penalize.
        if hit_rate_l15 > 0:
            l5_l15_ratio = hr_l5 / hit_rate_l15
            if l5_l15_ratio < L5_DIVERGENCE_SEVERE:
                score *= L5_DIVERGENCE_SCORE_SEVERE
                if confidence == "Alta":
                    confidence = "Media"
            elif l5_l15_ratio < L5_DIVERGENCE_MILD:
                score *= L5_DIVERGENCE_SCORE_MILD

        # ── Low-line triples penalty ──────────────────────────────────────────
        if market_key == "player_threes" and line <= TRIPLES_LOW_LINE_THRESHOLD:
            score *= TRIPLES_LOW_LINE_FACTOR

        # ── Minutes variability penalty (bench/role players con minutos erráticos)
        min_cv = _get_minutes_cv(logs)
        if min_cv > MIN_CV_PENALTY:
            score *= MIN_CV_SCORE_FACTOR
            if confidence == "Alta":
                confidence = "Media"

        # ── Matchup multiplier (team_context) ─────────────────────────────────
        if team_context:
            player_team = (player_logs.get(player) or [{}])[0].get("TEAM_ABBREVIATION")
            if player_team:
                game_abbrs = game_team_abbrs.get(game_label, set())
                opp_team   = next((a for a in game_abbrs if a != player_team), None)

                # Opponent defensive quality → affects offensive props
                if opp_team and opp_team in team_context and market_key in OFFENSIVE_MARKETS:
                    opp_pts = team_context[opp_team].get("opp_pts", LEAGUE_AVG_OPP_PTS)
                    if opp_pts > 117:       # weak defense
                        score *= 1.07
                        ctx_hits += 1
                    elif opp_pts > 114:
                        score *= 1.03
                        ctx_hits += 1
                    elif opp_pts < 108:     # elite defense
                        score *= 0.91
                        ctx_hits += 1
                    elif opp_pts < 111:
                        score *= 0.96
                        ctx_hits += 1

                # Game pace → affects all props
                own_pace = team_context.get(player_team, {}).get("pace_est", LEAGUE_AVG_PACE)
                opp_pace = team_context.get(opp_team,   {}).get("pace_est", LEAGUE_AVG_PACE) if opp_team else LEAGUE_AVG_PACE
                game_pace = (own_pace + opp_pace) / 2
                if game_pace > 102:
                    score *= 1.02
                elif game_pace < 96:
                    score *= 0.98

        # ── Projection multiplier (SportsData.io) ─────────────────────────────
        if projections and player in projections:
            proj = projections[player]

            # Skip if projected minutes are too low (player unlikely to play enough)
            proj_min = proj.get("min", 35.0)
            if proj_min > 0 and proj_min < 18:
                continue

            proj_stat = _get_proj_stat(proj, market_key)
            if proj_stat is not None and proj_stat > 0 and line > 0:
                proj_edge = (proj_stat - line) / line
                if proj_edge > 0.25:        # projection 25%+ above line → strong signal
                    score *= 1.10
                    proj_hits += 1
                elif proj_edge > 0.12:      # 12%+ above
                    score *= 1.05
                    proj_hits += 1
                elif proj_edge < -0.20:     # projection 20%+ below line → fade
                    score *= 0.87
                    proj_hits += 1
                elif proj_edge < -0.10:
                    score *= 0.93
                    proj_hits += 1

        # ── Blowout risk ──────────────────────────────────────────────────────
        # Bench players (<28 min avg): broad penalty on all stats
        # Star players (≥28 min avg): penalty only on assists markets (game flow)
        avg_min = _get_avg_minutes(logs)
        if game_lines:
            game_id_for_lines = game_label_to_id.get(game_label)
            if game_id_for_lines:
                gl = game_lines.get(game_id_for_lines, {})
                spread = gl.get("spread")
                if spread is not None and abs(spread) > BLOWOUT_SPREAD_THRESHOLD:
                    if avg_min < BLOWOUT_BENCH_MIN_AVG:
                        # Bench/role player in blowout: full penalty
                        score *= BLOWOUT_SCORE_FACTOR
                    elif market_key in ASSISTS_MARKETS:
                        # Star player: assists cratered by lopsided game flow
                        score *= BLOWOUT_STAR_ASSISTS_FACTOR
                        if confidence == "Alta":
                            confidence = "Media"

        # ── Compute real model probability and EV% ────────────────────────────
        m_prob = _compute_model_prob(stats)
        ev     = _compute_ev_pct(m_prob, price, opposite_price)

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
            is_dtd=is_dtd,
            injury_status=inj,
            score=round(score, 4),
            ev_pct=ev,
            model_prob=m_prob,
        )
        candidates.append(pick)

    print(f"[analyzer] {len(candidates)} candidates | "
          f"context adjustments: {ctx_hits} | projection adjustments: {proj_hits}")

    # ── Multi-market same-player boost ────────────────────────────────────────
    # When the model identifies 3+ markets for the same player, it signals strong
    # consistency. Boost score slightly so they cluster together in the final output.
    player_pick_count = Counter(p.player for p in candidates)
    multi_boosted = 0
    for pick in candidates:
        if (player_pick_count[pick.player] >= MULTI_PICK_MIN_COUNT
                and pick.confidence in ("Alta", "Media")):
            pick.score = round(pick.score * MULTI_PICK_SCORE_BOOST, 4)
            multi_boosted += 1
    if multi_boosted:
        print(f"[analyzer] Multi-market boost applied to {multi_boosted} picks")

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

    # Apply per-game, per-player, and total caps
    picks_by_game: dict[str, list[PlayerPick]] = {}
    player_pick_counts_final: dict[str, int] = {}
    total = 0
    for pick in pool:
        if total >= MAX_TOTAL_PICKS:
            break
        game_picks = picks_by_game.setdefault(pick.game_label, [])
        if len(game_picks) >= MAX_PICKS_PER_GAME:
            continue
        # Per-player cap: avoid cascade failures from stacking same player
        if player_pick_counts_final.get(pick.player, 0) >= MAX_PICKS_PER_PLAYER:
            continue
        game_picks.append(pick)
        player_pick_counts_final[pick.player] = player_pick_counts_final.get(pick.player, 0) + 1
        total += 1

    # ── Claude qualitative refinements (optional, injected by Orchestrator) ──────
    # Refinements are score multipliers + optional confidence overrides produced by
    # the multi-agent system. They never replace the base quantitative logic — they
    # only nudge scores based on qualitative context (news, ramp-up, blowout, etc.).
    # Key format: "player_name|market_key"  →  {"score_factor": float, "confidence": str}
    if claude_refinements:
        refined_count = 0
        for game_picks in picks_by_game.values():
            for pick in game_picks:
                key = f"{pick.player}|{pick.market_key}"
                ref = claude_refinements.get(key)
                if not ref:
                    continue
                factor = float(ref.get("score_factor", 1.0))
                # Clamp to safe range: ±25% max adjustment over base score
                factor = max(0.75, min(1.25, factor))
                if abs(factor - 1.0) > 0.005:
                    pick.score = round(pick.score * factor, 4)
                    refined_count += 1
                conf_override = ref.get("confidence_override")
                if conf_override in ("Alta", "Media", "Baja"):
                    pick.confidence = conf_override
        if refined_count:
            print(f"[analyzer] Claude refinements applied to {refined_count} picks")

    return picks_by_game
