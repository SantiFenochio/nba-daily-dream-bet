"""
Player prop analyzer.

Bug fixes in this version:
  - BUG FIX 2:  Two-sided devig using actual Over+Under prices (not a single-side estimate)
  - BUG FIX 4:  Dynamic league averages calculated from real team_context data
  - BUG FIX 3:  Playoff detection handled in fetch_player_stats; analyzer is season-type agnostic
  - FEATURE:    MAX_PICKS_PER_GAME increased to 6 (user wants multiple picks per game)

Improvements retained:
  - Mejora 1:   True EV via devig
  - Mejora 10:  Poisson distribution
  - Mejora 14:  Bayesian Laplace smoothing
  - Mejoras 5+7: Pace + DEF_RATING context factors
  - Mejora NEW: Exponential decay weighting (recent games weighted more)
  - Mejora NEW: Rest days factor (fresh legs vs rust)
  - Mejora NEW: Minutes trend detection (expanding/contracting role)
  - Mejora NEW: Hot/cold form detection (L5 vs L10 performance delta)
"""

import statistics
from dataclasses import dataclass
from datetime import date, datetime
from scipy.stats import poisson as poisson_dist

from modules.fetch_player_stats import get_stat_value, parse_minutes
from modules.fetch_props import MARKET_LABELS

# ── Tunable constants ────────────────────────────────────────────────────────
MIN_MINUTES_THRESHOLD = 20.0   # Filter low-usage players (raised from 18 → 20)
MIN_GAMES_REQUIRED    = 5      # Minimum game log sample to analyze
MAX_PICKS_PER_GAME    = 6      # Top N picks per game
MAX_TOTAL_PICKS       = 20     # Hard cap across all games
OPP_HISTORY_WEIGHT    = 0.10   # Weight of opponent-specific historical adj
LOCATION_WEIGHT       = 0.15   # Weight of home/away split adjustment
B2B_PENALTY           = 0.93   # Performance multiplier on back-to-back nights
LAPLACE_PRIOR         = 0.50   # Bayesian prior (50/50 neutral)
LAPLACE_WEIGHT        = 4      # Equivalent prior observations
MIN_EV_THRESHOLD      = 2.0    # Default minimum EV% (overridden per market below)
QUARTER_KELLY         = 0.25   # Fraction of full Kelly to stake
# ── EV mínimo por tipo de mercado (mejora 1) ──────────────────────────────────
# Stats impredecibles (steals, blocks, threes) necesitan mayor ventaja esperada
# para compensar su alta varianza inherente. PRA es más suave por ser combinado.
MIN_EV_BY_MARKET: dict[str, float] = {
    "player_points":                    3.0,   # Puntos: umbral medio
    "player_rebounds":                  3.0,   # Rebotes: umbral medio
    "player_assists":                   3.0,   # Asistencias: umbral medio
    "player_points_rebounds_assists":   2.0,   # PRA: más flexible (combo suaviza varianza)
    "player_threes":                    5.0,   # Triples: volátil, necesita más borde
    "player_steals":                    6.0,   # Robos: muy aleatorio, umbral alto
    "player_blocks":                    6.0,   # Tapas: muy aleatorio, umbral alto
    "player_turnovers":                 5.0,   # Pérdidas: inconsistente
}
# ── Coeficiente de variación máximo por mercado (mejora 2) ────────────────────
# Si el jugador varía demasiado, la línea es poco confiable. CV = stdev / mean.
# Steals/blocks tienen CV alto por naturaleza → umbral más permisivo.
MAX_CV_BY_MARKET: dict[str, float] = {
    "player_points":                    0.45,  # 45% CV máx para puntos
    "player_rebounds":                  0.50,  # 50% CV máx para rebotes
    "player_assists":                   0.55,  # 55% CV máx para asistencias
    "player_points_rebounds_assists":   0.40,  # 40% CV máx para PRA (más estable)
    "player_threes":                    0.80,  # 80% CV máx para triples
    "player_steals":                    0.90,  # 90% CV máx para robos
    "player_blocks":                    0.90,  # 90% CV máx para tapas
    "player_turnovers":                 0.70,  # 70% CV máx para pérdidas
}
# ── Riesgo de rotación — minutos muy irregulares (mejora 3) ───────────────────
ROTATION_RISK_STD     = 7.0    # std dev minutos L10 > 7 = jugador rotacional
ROTATION_RISK_PENALTY = 0.93   # −7% proyección para jugadores de rotación irregular
# ── DVP por stat — mapea mercado a la columna de oponente (mejora 4) ──────────
# Usa cuántos pts/reb/ast/3s PERMITE el rival por partido en vez del DEF_RATING genérico.
MARKET_TO_OPP_STAT: dict[str, str] = {
    "player_points":                    "opp_pts",
    "player_rebounds":                  "opp_reb",
    "player_assists":                   "opp_ast",
    "player_threes":                    "opp_fg3m",
    "player_turnovers":                 "opp_tov",
    "player_points_rebounds_assists":   "opp_pts",   # pts como proxy de PRA
    # steals y blocks → fallback a DEF_RATING (posición específica no disponible)
}
# Blowout risk — if the spread exceeds this, the favored team's stars risk
# sitting the 4th quarter early, suppressing their counting stats.
BLOWOUT_SPREAD_THRESHOLD = 12.0   # Points — spread from favored team perspective
BLOWOUT_FAVORITE_PENALTY = 0.91   # 9% reduction for blowout-risk games (SGA effect)
# Teammate absence boost — if a key teammate is out, remaining players get
# more usage and possessions (Flagg/LeBron effect when Doncic+Reaves sat).
ABSENCE_BOOST_PER_PLAYER = 0.10   # 10% per confirmed-Out teammate (capped at 2)
# Foul trouble — players who are historically foul-prone risk reduced minutes
# when sitting due to early foul accumulation.
FOUL_HIGH_AVG_THRESHOLD  = 3.3    # avg PF/game — considered foul-prone
FOUL_TROUBLE_MIN_RATIO   = 0.78   # below this fraction of avg minutes = sat due to fouls
FOUL_TROUBLE_COUNT_RISK  = 3      # 3+ foul-trouble games in last 20 = high risk
FOUL_OUT_COUNT_RISK      = 2      # 2+ foul-outs (6 PF) in last 20 = very high risk
FOUL_TROUBLE_PENALTY     = 0.95   # 5% projection penalty for foul-prone players
# League averages — fallback when team_context is empty
_DEFAULT_LEAGUE_AVG_PACE       = 99.5
_DEFAULT_LEAGUE_AVG_DEF_RATING = 113.5
# Exponential decay weighting — recent games count more
# game 1 (last) = 1.00, game 2 = 0.85, game 5 = 0.52, game 10 = 0.23, game 20 = 0.04
DECAY_FACTOR           = 0.85
# Rest days — performance varies with recovery time
REST_FRESH_DAYS        = 4      # 4+ days between games → well-rested boost
REST_FRESH_BOOST       = 1.025  # +2.5% boost after 4+ days rest
REST_RUST_DAYS         = 7      # 7+ days → possible rust penalty
REST_RUST_PENALTY      = 0.990  # −1% after extended break
# Minutes trend — detects role expansion/contraction over last 5 vs last 10 games
MINUTES_TREND_THRESHOLD = 0.10  # 10% change = meaningful role shift
MINUTES_TREND_WEIGHT    = 0.40  # how much of the trend translates to the projection
# Hot/cold form — L5 vs L10 average comparison
FORM_HOT_THRESHOLD      = 0.12  # L5 > L10 by 12%+ → hot streak
FORM_COLD_THRESHOLD     = 0.12  # L5 < L10 by 12%+ → cold streak
FORM_WEIGHT             = 0.30  # fraction of the form delta applied to projection
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class PlayerPick:
    player: str
    game_label: str
    market_key: str
    market: str
    side: str
    line: float
    price: int

    avg_l5: float
    avg_l10: float
    avg_l20: float
    hit_count_l10: int
    hit_count_l20: int
    games_l10: int
    games_l20: int
    projection: float          # Context-adjusted projection
    edge: float                # Stat-unit edge (projection − line)

    model_prob: float          # Blended Poisson + Bayesian probability
    fair_prob: float           # True no-vig probability from devig
    ev_pct: float              # Expected value as % of stake
    kelly_pct: float           # Quarter-Kelly recommended stake %

    consecutive_streak: int
    confidence: str
    headline: str
    detail: str
    score: float
    is_b2b: bool
    injury_status: str | None = None
    pace_factor: float = 1.0
    dvp_factor: float = 1.0
    blowout_risk: bool = False       # Favored team by 12+ pts — star may sit 4th
    absence_boost: float = 1.0      # Multiplier from absent teammates
    foul_risk: bool = False          # Player is historically foul-prone
    avg_fouls: float = 0.0          # Average personal fouls per game (L10)
    foul_out_count: int = 0         # Foul-outs (6 PF) in last 20 games
    foul_trouble_count: int = 0     # Games sat early due to foul trouble (L20)
    rest_days: int = 2              # Days since last game (1=B2B, 4+=fresh)
    minutes_trend_pct: float = 0.0  # % change avg minutes L5 vs L10 (>0 = expanding role)
    is_hot: bool = False            # L5 avg > L10 avg by FORM_HOT_THRESHOLD
    is_cold: bool = False           # L5 avg < L10 avg by FORM_COLD_THRESHOLD
    high_variance: bool = False     # Player's CV exceeds market threshold — inconsistent
    cv_score: float = 0.0           # Coefficient of variation (stdev/mean) L10
    rotation_risk: bool = False     # Minutes std dev > ROTATION_RISK_STD — rotational player
    min_std: float = 0.0            # Std dev of minutes in L10


# ══════════════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════════════

def analyze_player_props(
    prop_records: list[dict],
    player_logs: dict[str, list[dict]],
    injury_statuses: dict[str, str | None],
    b2b_team_abbrs: set[str],
    games: list[dict],
    team_context: dict | None = None,
    game_lines: dict | None = None,          # {game_id: {spread, total, home_is_favorite}}
    team_absent_players: dict | None = None,  # {team_abbr: {player_name, ...}}
    min_ev_threshold: float = MIN_EV_THRESHOLD,  # override for fallback mode
    market_ev_multipliers: dict[str, float] | None = None,  # calibration from history
) -> dict[str, list["PlayerPick"]]:
    """
    Returns {game_label: [PlayerPick, ...]} sorted by EV, capped at MAX_PICKS_PER_GAME.
    Pass min_ev_threshold=0.0 to get best-available picks ignoring the EV floor.
    """
    game_by_id = {g["id"]: g for g in games}
    tc  = team_context or {}
    gl  = game_lines or {}
    tap = team_absent_players or {}
    mev = market_ev_multipliers or {}

    today_str = date.today().strftime("%Y-%m-%d")
    league_avg_pace, league_avg_def, league_avg_opp = _calculate_league_averages(tc)

    picks_by_game: dict[str, list[PlayerPick]] = {}

    # Group outcomes by (player, market_key, game_id)
    grouped: dict[tuple, list[dict]] = {}
    for rec in prop_records:
        key = (rec["player"], rec["market_key"], rec["game_id"])
        grouped.setdefault(key, []).append(rec)

    for (player, market_key, game_id), outcomes in grouped.items():
        logs = player_logs.get(player, [])
        game = game_by_id.get(game_id)
        if not game or not logs:
            continue

        player_team_abbr = logs[0].get("TEAM_ABBREVIATION", "") if logs else ""
        is_home  = player_team_abbr == game["home_team"]["abbreviation"]
        opp_abbr = (
            game["visitor_team"]["abbreviation"] if is_home
            else game["home_team"]["abbreviation"]
        )
        is_b2b     = player_team_abbr in b2b_team_abbrs
        game_label = f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}"

        pace_factor, dvp_factor = _get_context_factors(
            player_team_abbr, opp_abbr, market_key, tc,
            league_avg_pace, league_avg_def, league_avg_opp,
        )

        # ── Blowout risk (April 5 lesson: SGA scored 20 in a 35-pt blowout) ──
        lines       = gl.get(game_id, {})
        spread      = lines.get("spread")          # home-perspective, negative = home favored
        game_total  = lines.get("total")
        home_fav    = lines.get("home_is_favorite")

        blowout_risk = False
        if spread is not None:
            # Is THIS player's team the heavy favorite?
            player_is_home    = is_home
            player_team_favored = (
                (player_is_home and home_fav) or
                (not player_is_home and not home_fav)
            )
            if player_team_favored and abs(spread) >= BLOWOUT_SPREAD_THRESHOLD:
                blowout_risk = True

        # ── Teammate absence boost (April 5: Flagg 45 pts, LeBron 15 ast) ──
        absent_teammates = tap.get(player_team_abbr, set()) - {player}
        n_absent         = min(len(absent_teammates), 2)   # cap at 2 players
        absence_boost    = 1.0 + ABSENCE_BOOST_PER_PLAYER * n_absent

        # ── Rest days ─────────────────────────────────────────────────────────
        rest_days = _compute_rest_days(logs, today_str)

        best_pick = None
        for rec in outcomes:
            pick = _analyze_one_prop(
                player=player,
                market_key=market_key,
                line=rec["line"],
                side=rec["side"],
                price=rec["price"],
                opposite_price=rec.get("opposite_price"),
                logs=logs,
                is_home=is_home,
                is_b2b=is_b2b,
                opp_abbr=opp_abbr,
                game_label=game_label,
                injury_status=injury_statuses.get(player),
                pace_factor=pace_factor,
                dvp_factor=dvp_factor,
                blowout_risk=blowout_risk,
                absence_boost=absence_boost,
                absent_teammates=absent_teammates,
                game_total=game_total,
                rest_days=rest_days,
                min_ev_threshold=min_ev_threshold,
                market_ev_multiplier=mev.get(market_key, 1.0),
            )
            if pick is None:
                continue
            if best_pick is None or pick.score > best_pick.score:
                best_pick = pick

        if best_pick:
            picks_by_game.setdefault(game_label, []).append(best_pick)

    # Sort by score per game, apply per-game cap
    for label in picks_by_game:
        picks_by_game[label].sort(key=lambda p: p.score, reverse=True)
        picks_by_game[label] = picks_by_game[label][:MAX_PICKS_PER_GAME]

    # Apply total cross-game cap (sorted by highest EV first)
    all_picks = [
        (label, pick)
        for label, picks in picks_by_game.items()
        for pick in picks
    ]
    all_picks.sort(key=lambda x: x[1].score, reverse=True)
    all_picks = all_picks[:MAX_TOTAL_PICKS]

    # Rebuild dict preserving game order
    final: dict[str, list[PlayerPick]] = {}
    for label, pick in all_picks:
        final.setdefault(label, []).append(pick)

    total = sum(len(v) for v in final.values())
    print(f"[analyzer] Total picks selected: {total} across {len(final)} games")
    return final


# ══════════════════════════════════════════════════════════════════════════════
# Core analysis for a single prop outcome
# ══════════════════════════════════════════════════════════════════════════════

def _analyze_one_prop(
    player: str,
    market_key: str,
    line: float,
    side: str,
    price: int,
    opposite_price: int | None,
    logs: list[dict],
    is_home: bool,
    is_b2b: bool,
    opp_abbr: str,
    game_label: str,
    injury_status: str | None,
    pace_factor: float,
    dvp_factor: float,
    blowout_risk: bool = False,
    absence_boost: float = 1.0,
    absent_teammates: set | None = None,
    game_total: float | None = None,
    rest_days: int = 2,
    min_ev_threshold: float = MIN_EV_THRESHOLD,
    market_ev_multiplier: float = 1.0,
) -> "PlayerPick | None":

    # ── Minutes filter (mejora 3) ─────────────────────────────────────────
    # Requires average ≥ 20 min in L10 — ensures meaningful playing time.
    min_vals = [parse_minutes(g.get("MIN", 0)) for g in logs[:10]]
    if min_vals and (sum(min_vals) / len(min_vals)) < MIN_MINUTES_THRESHOLD:
        return None

    avg_minutes_l10 = (sum(min_vals) / len(min_vals)) if min_vals else 30.0

    # ── Rotation risk — irregular minutes (mejora 3) ──────────────────────
    # Players with highly variable minutes are riskier: some nights they
    # barely play. Detect via std dev of minutes in L10.
    if len(min_vals) >= 3:
        min_std       = statistics.stdev(min_vals)
        rotation_risk = min_std > ROTATION_RISK_STD
    else:
        min_std       = 0.0
        rotation_risk = False

    # ── Foul trouble analysis (PF column from nba_api) ────────────────────
    # Uses last 20 games to detect chronic foul issues.
    # Three signals:
    #   1. High average fouls/game (foul-prone player)
    #   2. Foul-outs (6 PF in a game) — forced early exit
    #   3. Foul-trouble games — 4+ PF AND played materially fewer minutes
    foul_vals_l20 = [float(g.get("PF", 0) or 0) for g in logs[:20]]
    min_vals_l20  = [parse_minutes(g.get("MIN", 0)) for g in logs[:20]]

    avg_fouls     = (sum(foul_vals_l20[:10]) / min(10, len(foul_vals_l20))
                     if foul_vals_l20 else 0.0)
    foul_out_count = sum(1 for f in foul_vals_l20 if f >= 6)

    # Foul trouble: 4+ fouls AND played under 78% of their average minutes
    foul_trouble_count = sum(
        1 for f, m in zip(foul_vals_l20, min_vals_l20)
        if f >= 4 and avg_minutes_l10 > 0 and (m / avg_minutes_l10) < FOUL_TROUBLE_MIN_RATIO
    )

    # Flag as foul risk if any threshold is met
    foul_risk = (
        avg_fouls    >= FOUL_HIGH_AVG_THRESHOLD or
        foul_out_count  >= FOUL_OUT_COUNT_RISK or
        foul_trouble_count >= FOUL_TROUBLE_COUNT_RISK
    )

    # ── Collect stat values ───────────────────────────────────────────────
    values: list[float] = []
    for g in logs:
        v = get_stat_value(g, market_key)
        if v is not None:
            values.append(v)

    if len(values) < MIN_GAMES_REQUIRED:
        return None

    n = len(values)

    # ── Variance filter — coeficiente de variación (mejora 2) ─────────────
    # CV = stdev / mean. A high CV means the player is too unpredictable for
    # this stat (e.g., scores 30 one night and 8 the next). Each market has
    # its own tolerance since steals/blocks are inherently more volatile.
    slice_cv = values[:min(10, n)]
    mean_cv  = sum(slice_cv) / len(slice_cv)
    if mean_cv > 0 and len(slice_cv) >= 3:
        cv_score     = statistics.stdev(slice_cv) / mean_cv
        max_cv       = MAX_CV_BY_MARKET.get(market_key, 0.60)
        high_variance = cv_score > max_cv
        if high_variance:
            return None   # too inconsistent — skip this pick
    else:
        cv_score      = 0.0
        high_variance = False

    # ── Averages (display only) ───────────────────────────────────────────
    avg_l5  = sum(values[:5])          / min(5, n)
    avg_l10 = sum(values[:min(10, n)]) / min(10, n)
    avg_l20 = sum(values[:min(20, n)]) / min(20, n)

    # ── Exponential decay projection (NEW) ───────────────────────────────
    # Each prior game is discounted by DECAY_FACTOR^i so the most recent
    # game has the highest influence. This is mathematically superior to
    # simple L5/L10/L20 bucket averages.
    # game 1=1.00, game 2=0.85, game 5=0.52, game 10=0.23, game 20=0.04
    base_projection = _weighted_avg(values[:20], DECAY_FACTOR)

    # ── Minutes trend — role expansion/contraction (NEW) ─────────────────
    # Compare average minutes in last 5 vs last 10 games.
    # If role is expanding, stats follow; if contracting, project lower.
    avg_min_l5  = sum(min_vals[:5]) / min(5, len(min_vals)) if min_vals else avg_minutes_l10
    minutes_trend_pct = (
        (avg_min_l5 - avg_minutes_l10) / avg_minutes_l10
        if avg_minutes_l10 > 0 else 0.0
    )
    # Cap the trend effect: ±20% minutes change max applies
    minutes_trend_pct = max(-0.20, min(0.20, minutes_trend_pct))
    # Only act on meaningful shifts (≥ MINUTES_TREND_THRESHOLD)
    if abs(minutes_trend_pct) >= MINUTES_TREND_THRESHOLD:
        minutes_trend_multiplier = 1.0 + minutes_trend_pct * MINUTES_TREND_WEIGHT
    else:
        minutes_trend_multiplier = 1.0

    # ── Hot / cold form detection (NEW) ──────────────────────────────────
    # Compares L5 to L10 baseline. If the player is on a hot or cold run,
    # nudge the projection in that direction (partially, not fully).
    form_multiplier = 1.0
    is_hot = is_cold = False
    if avg_l10 > 0:
        form_ratio = avg_l5 / avg_l10
        if form_ratio >= (1.0 + FORM_HOT_THRESHOLD):
            # Hot streak: L5 is significantly above L10
            form_multiplier = 1.0 + (form_ratio - 1.0) * FORM_WEIGHT
            form_multiplier = min(form_multiplier, 1.15)   # cap at +15%
            is_hot = True
        elif form_ratio <= (1.0 - FORM_COLD_THRESHOLD):
            # Cold streak: L5 is significantly below L10
            form_multiplier = 1.0 + (form_ratio - 1.0) * FORM_WEIGHT
            form_multiplier = max(form_multiplier, 0.90)   # floor at −10%
            is_cold = True

    # ── Rest days factor (NEW) ────────────────────────────────────────────
    # B2B is already penalized separately. Here we boost for fresh legs.
    rest_factor = _rest_days_factor(rest_days, is_b2b)

    # ── Hit rates ─────────────────────────────────────────────────────────
    side_lower = side.lower()

    def _hit(v: float) -> bool:
        return v > line if side_lower == "over" else v < line

    slice_10 = values[:min(10, n)]
    slice_20 = values[:min(20, n)]
    hit_l10 = sum(1 for v in slice_10 if _hit(v))
    hit_l20 = sum(1 for v in slice_20 if _hit(v))
    rate_l10 = hit_l10 / len(slice_10)
    rate_l20 = hit_l20 / len(slice_20)

    # ── Location split ────────────────────────────────────────────────────
    home_vals = [get_stat_value(g, market_key) for g in logs if "vs." in g.get("MATCHUP", "")]
    home_vals = [v for v in home_vals if v is not None]
    away_vals = [get_stat_value(g, market_key) for g in logs if " @ " in g.get("MATCHUP", "")]
    away_vals = [v for v in away_vals if v is not None]

    loc_adj = 0.0
    if is_home and len(home_vals) >= 3:
        loc_adj = (sum(home_vals) / len(home_vals) - base_projection) * LOCATION_WEIGHT
    elif not is_home and len(away_vals) >= 3:
        loc_adj = (sum(away_vals) / len(away_vals) - base_projection) * LOCATION_WEIGHT

    # ── Opponent-specific split ───────────────────────────────────────────
    opp_vals = [
        get_stat_value(g, market_key) for g in logs
        if opp_abbr and opp_abbr.upper() in g.get("MATCHUP", "").upper()
    ]
    opp_vals = [v for v in opp_vals if v is not None]
    opp_adj = 0.0
    if len(opp_vals) >= 2:
        opp_adj = (sum(opp_vals) / len(opp_vals) - base_projection) * OPP_HISTORY_WEIGHT

    # ── Context-adjusted projection ───────────────────────────────────────
    adj_projection = (
        base_projection
        * (B2B_PENALTY if is_b2b else 1.0)
        * rest_factor                                             # fresh legs / rust
        * pace_factor
        * dvp_factor
        * (BLOWOUT_FAVORITE_PENALTY if blowout_risk else 1.0)   # blowout: star sits early
        * absence_boost                                           # teammate out: more usage
        * (FOUL_TROUBLE_PENALTY if foul_risk else 1.0)           # foul-prone: minutes risk
        * (ROTATION_RISK_PENALTY if rotation_risk else 1.0)      # irregular minutes risk
        * minutes_trend_multiplier                                # role expanding/contracting
        * form_multiplier                                         # hot/cold streak adjustment
        + loc_adj
        + opp_adj
    )
    adj_projection = max(0.0, round(adj_projection, 1))

    edge = round(adj_projection - line, 1) if side_lower == "over" else round(line - adj_projection, 1)

    # ── Poisson probability (Mejora 10) ───────────────────────────────────
    poisson_prob = _poisson_prob(adj_projection, line, side_lower)

    # ── Bayesian hit rate (Mejora 14) ─────────────────────────────────────
    bayes_prob = _bayesian_prob(hit_l10, len(slice_10))

    # Blend 50/50
    model_prob = 0.50 * poisson_prob + 0.50 * bayes_prob

    # ── BUG FIX 2: Two-sided devig (Mejora 1) ────────────────────────────
    if opposite_price is not None:
        fair_prob = _devig_two_sides(price, opposite_price)
    else:
        fair_prob = _devig_single(price)

    ev_pct = _expected_value_pct(model_prob, price)

    # Use market-specific EV threshold (mejora 1) unless caller overrides (fallback mode)
    # Apply calibration multiplier from historical accuracy (market_ev_multiplier)
    base_threshold = min_ev_threshold if min_ev_threshold != MIN_EV_THRESHOLD else \
        MIN_EV_BY_MARKET.get(market_key, MIN_EV_THRESHOLD)
    effective_ev_threshold = base_threshold * market_ev_multiplier
    if ev_pct < effective_ev_threshold:
        return None

    kelly_pct = _quarter_kelly(model_prob, price)

    # ── Consecutive streak ────────────────────────────────────────────────
    streak = 0
    for v in values:
        if _hit(v):
            streak += 1
        else:
            break

    # ── Confidence tier ───────────────────────────────────────────────────
    if ev_pct >= 10.0 and rate_l10 >= 0.65:
        confidence = "Alta"
    elif ev_pct >= 5.0 and rate_l10 >= 0.55:
        confidence = "Media"
    elif ev_pct >= MIN_EV_THRESHOLD:
        confidence = "Baja"
    else:
        confidence = "Riesgosa"

    # ── Natural language headline ─────────────────────────────────────────
    market_label = MARKET_LABELS.get(market_key, market_key)
    headline = _build_headline(
        player=player,
        market_label=market_label,
        side=side,
        line=line,
        values=values,
        streak=streak,
        avg_l5=avg_l5,
        avg_l10=avg_l10,
        hit_l10=hit_l10,
        rate_l10=rate_l10,
        edge=edge,
        adj_projection=adj_projection,
        ev_pct=ev_pct,
        model_prob=model_prob,
        is_b2b=is_b2b,
        opp_vals=opp_vals,
        opp_avg=sum(opp_vals) / len(opp_vals) if opp_vals else None,
        opp_abbr=opp_abbr,
        is_home=is_home,
        home_vals=home_vals,
        away_vals=away_vals,
        pace_factor=pace_factor,
        dvp_factor=dvp_factor,
        blowout_risk=blowout_risk,
        absent_teammates=absent_teammates or set(),
        foul_risk=foul_risk,
        avg_fouls=avg_fouls,
        foul_out_count=foul_out_count,
        foul_trouble_count=foul_trouble_count,
        rest_days=rest_days,
        minutes_trend_pct=minutes_trend_pct,
        is_hot=is_hot,
        is_cold=is_cold,
        rotation_risk=rotation_risk,
        min_std=min_std,
    )

    foul_detail = ""  # built below, appended to detail string
    if foul_risk:
        parts_f = [f"Prom. faltas: {avg_fouls:.1f}/j"]
        if foul_out_count >= FOUL_OUT_COUNT_RISK:
            parts_f.append(f"foul-outs: {foul_out_count}/20j")
        if foul_trouble_count >= FOUL_TROUBLE_COUNT_RISK:
            parts_f.append(f"salidas tempranas por faltas: {foul_trouble_count}/20j")
        foul_detail = " | " + " | ".join(parts_f)

    detail = (
        f"L5: {avg_l5:.1f} | L10: {avg_l10:.1f} | L20: {avg_l20:.1f} | "
        f"Hit L10: {hit_l10}/{len(slice_10)} ({rate_l10*100:.0f}%) | "
        f"Hit L20: {hit_l20}/{len(slice_20)} ({rate_l20*100:.0f}%)"
        f"{foul_detail}"
    )

    score = (ev_pct / 100.0) * 0.60 + rate_l10 * 0.25 + rate_l20 * 0.15

    return PlayerPick(
        player=player,
        game_label=game_label,
        market_key=market_key,
        market=market_label,
        side=side,
        line=line,
        price=price,
        avg_l5=avg_l5,
        avg_l10=avg_l10,
        avg_l20=avg_l20,
        hit_count_l10=hit_l10,
        hit_count_l20=hit_l20,
        games_l10=len(slice_10),
        games_l20=len(slice_20),
        projection=adj_projection,
        edge=edge,
        model_prob=model_prob,
        fair_prob=fair_prob,
        ev_pct=ev_pct,
        kelly_pct=kelly_pct,
        consecutive_streak=streak,
        confidence=confidence,
        headline=headline,
        detail=detail,
        score=score,
        is_b2b=is_b2b,
        injury_status=injury_status,
        pace_factor=pace_factor,
        dvp_factor=dvp_factor,
        blowout_risk=blowout_risk,
        absence_boost=absence_boost,
        foul_risk=foul_risk,
        avg_fouls=round(avg_fouls, 1),
        foul_out_count=foul_out_count,
        foul_trouble_count=foul_trouble_count,
        rest_days=rest_days,
        minutes_trend_pct=round(minutes_trend_pct * 100, 1),  # store as %
        is_hot=is_hot,
        is_cold=is_cold,
        high_variance=high_variance,
        cv_score=round(cv_score, 2),
        rotation_risk=rotation_risk,
        min_std=round(min_std, 1),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Probability / EV helpers
# ══════════════════════════════════════════════════════════════════════════════

def _weighted_avg(values: list[float], decay: float = 0.85) -> float:
    """
    Exponentially weighted average with newest-first ordering.
    game 1 (most recent) has weight decay^0=1.0, game 2 has decay^1, etc.
    This gives the most recent game the highest influence automatically.
    """
    if not values:
        return 0.0
    weights = [decay ** i for i in range(len(values))]
    total_w = sum(weights)
    return sum(v * w for v, w in zip(values, weights)) / total_w


def _compute_rest_days(logs: list[dict], today_str: str) -> int:
    """
    Compute days between player's last game and today.
    Returns 1 for B2B, 2 for standard 1-day rest, 4 for well-rested, etc.
    """
    if not logs:
        return 2
    raw = logs[0].get("GAME_DATE", "")
    if not raw:
        return 2
    try:
        last_date = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        today = datetime.strptime(today_str, "%Y-%m-%d").date()
        delta = (today - last_date).days
        return max(1, delta)
    except Exception:
        return 2


def _rest_days_factor(rest_days: int, is_b2b: bool) -> float:
    """
    Performance multiplier based on recovery time.
    B2B already penalized separately — this covers the upside of extra rest.
    """
    if is_b2b or rest_days <= 1:
        return 1.0   # B2B_PENALTY already applied
    if rest_days >= REST_RUST_DAYS:
        return REST_RUST_PENALTY    # long layoff → possible rust
    if rest_days >= REST_FRESH_DAYS:
        return REST_FRESH_BOOST     # well-rested → boost
    return 1.0   # 2–3 days = standard rest, no adjustment


def _poisson_prob(lambda_rate: float, line: float, side: str) -> float:
    """Poisson CDF probability for count stats."""
    if lambda_rate <= 0:
        return 0.5
    floor_line = int(line)
    try:
        if side == "over":
            return float(1.0 - poisson_dist.cdf(floor_line, mu=lambda_rate))
        else:
            return float(poisson_dist.cdf(floor_line - 1, mu=lambda_rate))
    except Exception:
        return 0.5


def _bayesian_prob(hits: int, games: int) -> float:
    """Laplace-smoothed hit rate. Pulls extreme small-sample estimates toward 50%."""
    return (hits + LAPLACE_PRIOR * LAPLACE_WEIGHT) / (games + LAPLACE_WEIGHT)


def american_to_implied_prob(american_odds: int) -> float:
    """Raw implied probability from American odds (includes vig)."""
    if american_odds > 0:
        return 100.0 / (american_odds + 100.0)
    return abs(american_odds) / (abs(american_odds) + 100.0)


def _devig_two_sides(price_this: int, price_other: int) -> float:
    """
    BUG FIX: Proper two-sided devig using both Over and Under prices.
    fair_prob = implied_this / (implied_this + implied_other)
    This is mathematically correct and removes the vig symmetrically.
    """
    implied_this  = american_to_implied_prob(price_this)
    implied_other = american_to_implied_prob(price_other)
    total = implied_this + implied_other
    return implied_this / total if total > 0 else 0.5


def _devig_single(price: int) -> float:
    """
    Single-side devig fallback (when opposite price is unavailable).
    Assumes a roughly symmetric market.
    """
    implied = american_to_implied_prob(price)
    other_implied = 1.0 - implied + (implied - 0.5) * 0.05
    total = implied + other_implied
    return implied / total


def _expected_value_pct(model_prob: float, american_odds: int) -> float:
    """EV as a percentage of stake."""
    if american_odds >= 0:
        net_win = float(american_odds)
    else:
        net_win = 10000.0 / abs(american_odds)
    ev = (model_prob * net_win) - ((1.0 - model_prob) * 100.0)
    return round(ev, 2)


def _quarter_kelly(model_prob: float, american_odds: int) -> float:
    """Quarter-Kelly stake as % of bankroll, capped at 5%."""
    if american_odds >= 0:
        decimal = american_odds / 100.0 + 1.0
    else:
        decimal = 100.0 / abs(american_odds) + 1.0
    b = decimal - 1.0
    if b <= 0:
        return 0.0
    full_kelly = (b * model_prob - (1.0 - model_prob)) / b
    qk = max(0.0, full_kelly * QUARTER_KELLY)
    return round(min(qk * 100.0, 5.0), 2)


# ══════════════════════════════════════════════════════════════════════════════
# Context factors — Pace + DEF_RATING
# ══════════════════════════════════════════════════════════════════════════════

def _calculate_league_averages(team_context: dict) -> tuple[float, float, dict]:
    """
    Calculates league-average pace, DEF_RATING, and per-stat opponent averages
    from real team data. Returns (avg_pace, avg_def, league_avg_opp_stats).
    Falls back to defaults if team_context is empty.
    """
    if not team_context:
        return _DEFAULT_LEAGUE_AVG_PACE, _DEFAULT_LEAGUE_AVG_DEF_RATING, {}

    paces = [v["pace"] for v in team_context.values() if v.get("pace")]
    def_ratings = [v["def_rating"] for v in team_context.values() if v.get("def_rating")]

    avg_pace = sum(paces) / len(paces) if paces else _DEFAULT_LEAGUE_AVG_PACE
    avg_def  = sum(def_ratings) / len(def_ratings) if def_ratings else _DEFAULT_LEAGUE_AVG_DEF_RATING

    # Compute league-average opponent stats for DVP normalization
    league_avg_opp: dict[str, float] = {}
    for stat_key in ("opp_pts", "opp_reb", "opp_ast", "opp_fg3m", "opp_stl", "opp_blk", "opp_tov"):
        vals = [v[stat_key] for v in team_context.values() if stat_key in v]
        if vals:
            league_avg_opp[stat_key] = sum(vals) / len(vals)

    print(f"[analyzer] Dynamic league averages: pace={avg_pace:.1f}, DEF_RATING={avg_def:.1f}")
    if league_avg_opp:
        print(f"[analyzer] League avg opp: pts={league_avg_opp.get('opp_pts', 0):.1f}, "
              f"reb={league_avg_opp.get('opp_reb', 0):.1f}, "
              f"ast={league_avg_opp.get('opp_ast', 0):.1f}, "
              f"3pm={league_avg_opp.get('opp_fg3m', 0):.1f}")
    return avg_pace, avg_def, league_avg_opp


def _get_stat_dvp_factor(
    market_key: str,
    opp_abbr: str,
    team_context: dict,
    league_avg_opp: dict,
    league_avg_def: float,
) -> float:
    """
    Returns a DVP multiplier specific to the stat market being analyzed.

    For scoring/rebounding/assists/threes: uses how many of that stat the
    opponent ALLOWS per game vs league average.
      > 1.0 = opponent allows more than avg → good for the player
      < 1.0 = opponent allows less than avg → bad for the player

    Falls back to DEF_RATING-based factor when no stat-specific data available.
    """
    stat_key = MARKET_TO_OPP_STAT.get(market_key)
    if stat_key:
        opp_stat    = team_context.get(opp_abbr, {}).get(stat_key)
        league_stat = league_avg_opp.get(stat_key)
        if opp_stat and league_stat and league_stat > 0:
            return round(opp_stat / league_stat, 4)

    # Fallback: generic DEF_RATING factor (higher DEF_RATING = worse defense = good for player)
    opp_def_rating = team_context.get(opp_abbr, {}).get("def_rating", league_avg_def)
    return round(league_avg_def / opp_def_rating, 4) if opp_def_rating > 0 else 1.0


def _get_context_factors(
    player_team: str,
    opp_abbr: str,
    market_key: str,
    team_context: dict,
    league_avg_pace: float,
    league_avg_def: float,
    league_avg_opp: dict,
) -> tuple[float, float]:
    """Returns (pace_factor, dvp_factor) for a player's matchup and specific market."""
    player_pace = team_context.get(player_team, {}).get("pace", league_avg_pace)
    opp_pace    = team_context.get(opp_abbr, {}).get("pace", league_avg_pace)
    game_pace   = (player_pace + opp_pace) / 2.0
    pace_factor = round(game_pace / league_avg_pace, 4) if league_avg_pace > 0 else 1.0

    dvp_factor = _get_stat_dvp_factor(market_key, opp_abbr, team_context, league_avg_opp, league_avg_def)

    return pace_factor, dvp_factor


# ══════════════════════════════════════════════════════════════════════════════
# Natural language headline builder
# ══════════════════════════════════════════════════════════════════════════════

def _build_headline(
    player, market_label, side, line, values, streak,
    avg_l5, avg_l10, hit_l10, rate_l10, edge, adj_projection,
    ev_pct, model_prob,
    is_b2b, opp_vals, opp_avg, opp_abbr,
    is_home, home_vals, away_vals,
    pace_factor, dvp_factor,
    blowout_risk: bool = False,
    absent_teammates: set | None = None,
    foul_risk: bool = False,
    avg_fouls: float = 0.0,
    foul_out_count: int = 0,
    foul_trouble_count: int = 0,
    rest_days: int = 2,
    minutes_trend_pct: float = 0.0,
    is_hot: bool = False,
    is_cold: bool = False,
    rotation_risk: bool = False,
    min_std: float = 0.0,
) -> str:
    n10 = min(10, len(values))
    parts = []

    # 1. Streak lead if prominent
    if streak >= 4:
        parts.append(f"Lleva {streak} partidos consecutivos {side} {line} en {market_label}")
    elif streak >= 2:
        parts.append(f"{streak} partidos seguidos {side} {line}")

    # 2. Hit rate
    if not parts or streak < 4:
        if rate_l10 >= 0.70:
            parts.append(
                f"Fue {side} en {hit_l10}/{n10} últimos partidos ({rate_l10*100:.0f}%)"
                f" — promedio L10: {avg_l10:.1f}"
            )
        elif rate_l10 >= 0.60:
            parts.append(
                f"{hit_l10}/{n10} veces {side} en últimos partidos — promedio L10: {avg_l10:.1f}"
            )
        else:
            parts.append(f"Promedio L5: {avg_l5:.1f} | L10: {avg_l10:.1f} vs línea {line}")

    # 3. Projection + edge
    sign = "+" if edge >= 0 else ""
    parts.append(f"Proyección ajustada: {adj_projection} ({sign}{edge} vs línea)")

    # 4. EV
    parts.append(f"EV: +{ev_pct:.1f}% | Prob. modelo: {model_prob*100:.0f}%")

    # 5. Pace
    if pace_factor >= 1.03:
        parts.append(f"Ritmo elevado (+{(pace_factor-1)*100:.1f}% posesiones)")
    elif pace_factor <= 0.97:
        parts.append(f"Ritmo lento (-{(1-pace_factor)*100:.1f}% posesiones)")

    # 6. DvP
    if dvp_factor >= 1.04:
        parts.append(f"Defensa rival débil — DvP: +{(dvp_factor-1)*100:.1f}%")
    elif dvp_factor <= 0.97:
        parts.append(f"Defensa rival sólida — DvP: {(dvp_factor-1)*100:.1f}%")

    # 7. Teammate absence boost
    if absent_teammates:
        names = ", ".join(sorted(absent_teammates))
        pct = min(len(absent_teammates), 2) * int(ABSENCE_BOOST_PER_PLAYER * 100)
        parts.append(f"Compañero(s) ausente(s): {names} — uso esperado +{pct}%")

    # 8. Blowout risk warning
    if blowout_risk:
        parts.append(f"Alerta paliza: favorito por 12+ pts — posible reduccion de minutos en 4to cuarto")

    # 9. Foul trouble risk
    if foul_risk:
        foul_parts = [f"Riesgo faltas: {avg_fouls:.1f} PF/partido"]
        if foul_out_count >= FOUL_OUT_COUNT_RISK:
            foul_parts.append(f"{foul_out_count} foul-outs en ultimos 20j")
        if foul_trouble_count >= FOUL_TROUBLE_COUNT_RISK:
            foul_parts.append(f"salio temprano por faltas en {foul_trouble_count} partidos")
        parts.append(" — ".join(foul_parts))

    # 10. Hot / cold form
    if is_hot:
        parts.append(f"EN RACHA: L5 ({avg_l5:.1f}) supera L10 ({avg_l10:.1f}) por +{((avg_l5/avg_l10-1)*100):.0f}%")
    elif is_cold:
        parts.append(f"FRIO: L5 ({avg_l5:.1f}) bajo su L10 ({avg_l10:.1f}) un {((1-avg_l5/avg_l10)*100):.0f}%")

    # 11. Minutes trend
    if abs(minutes_trend_pct) >= MINUTES_TREND_THRESHOLD * 100:
        if minutes_trend_pct > 0:
            parts.append(f"Rol en expansion: +{minutes_trend_pct:.0f}% minutos L5 vs L10")
        else:
            parts.append(f"Rol en reduccion: {minutes_trend_pct:.0f}% minutos L5 vs L10")

    # 12. Rest days
    if not is_b2b and rest_days >= REST_FRESH_DAYS:
        parts.append(f"Descansado: {rest_days} dias de descanso (+{(REST_FRESH_BOOST-1)*100:.1f}% proyectado)")
    elif not is_b2b and rest_days >= REST_RUST_DAYS:
        parts.append(f"Posible oxido: {rest_days} dias sin jugar")

    # 13. Rotation risk
    if rotation_risk:
        parts.append(f"Rotacion irregular: varianza minutos ±{min_std:.0f} min — riesgo de banca")

    # 14. B2B
    if is_b2b:
        parts.append("Back-to-back — rendimiento suele bajar ~7%")

    # 10. Opponent history
    if opp_avg is not None and len(opp_vals) >= 2:
        parts.append(f"Vs {opp_abbr}: promedia {opp_avg:.1f} en {len(opp_vals)} partido(s)")

    # 11. Location split
    loc_vals = home_vals if is_home else away_vals
    loc_label = "local" if is_home else "visitante"
    if len(loc_vals) >= 3:
        parts.append(f"De {loc_label}: {sum(loc_vals)/len(loc_vals):.1f} promedio")

    return " | ".join(parts)
