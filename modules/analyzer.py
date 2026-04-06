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
"""

from dataclasses import dataclass
from scipy.stats import poisson as poisson_dist

from modules.fetch_player_stats import get_stat_value, parse_minutes
from modules.fetch_props import MARKET_LABELS

# ── Tunable constants ────────────────────────────────────────────────────────
MIN_MINUTES_THRESHOLD = 18.0   # Filter garbage-time / low-usage players
MIN_GAMES_REQUIRED    = 5      # Minimum game log sample to analyze
MAX_PICKS_PER_GAME    = 6      # Top N picks per game
MAX_TOTAL_PICKS       = 20     # Hard cap across all games
OPP_HISTORY_WEIGHT    = 0.10   # Weight of opponent-specific historical adj
LOCATION_WEIGHT       = 0.15   # Weight of home/away split adjustment
B2B_PENALTY           = 0.93   # Performance multiplier on back-to-back nights
LAPLACE_PRIOR         = 0.50   # Bayesian prior (50/50 neutral)
LAPLACE_WEIGHT        = 4      # Equivalent prior observations
MIN_EV_THRESHOLD      = 2.0    # Minimum EV% to include a pick
QUARTER_KELLY         = 0.25   # Fraction of full Kelly to stake
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
) -> dict[str, list["PlayerPick"]]:
    """
    Returns {game_label: [PlayerPick, ...]} sorted by EV, capped at MAX_PICKS_PER_GAME.
    """
    game_by_id = {g["id"]: g for g in games}
    tc  = team_context or {}
    gl  = game_lines or {}
    tap = team_absent_players or {}

    league_avg_pace, league_avg_def = _calculate_league_averages(tc)

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
            player_team_abbr, opp_abbr, tc, league_avg_pace, league_avg_def
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
) -> "PlayerPick | None":

    # ── Minutes filter ────────────────────────────────────────────────────
    min_vals = [parse_minutes(g.get("MIN", 0)) for g in logs[:10]]
    if min_vals and (sum(min_vals) / len(min_vals)) < MIN_MINUTES_THRESHOLD:
        return None

    avg_minutes_l10 = (sum(min_vals) / len(min_vals)) if min_vals else 30.0

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

    # ── Averages ──────────────────────────────────────────────────────────
    avg_l5  = sum(values[:5])          / min(5, n)
    avg_l10 = sum(values[:min(10, n)]) / min(10, n)
    avg_l20 = sum(values[:min(20, n)]) / min(20, n)

    # Recency-weighted base projection
    base_projection = 0.40 * avg_l5 + 0.35 * avg_l10 + 0.25 * avg_l20

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
        * pace_factor
        * dvp_factor
        * (BLOWOUT_FAVORITE_PENALTY if blowout_risk else 1.0)   # blowout: star sits early
        * absence_boost                                           # teammate out: more usage
        * (FOUL_TROUBLE_PENALTY if foul_risk else 1.0)           # foul-prone: minutes risk
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

    if ev_pct < MIN_EV_THRESHOLD:
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
    )


# ══════════════════════════════════════════════════════════════════════════════
# Probability / EV helpers
# ══════════════════════════════════════════════════════════════════════════════

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

def _calculate_league_averages(team_context: dict) -> tuple[float, float]:
    """
    BUG FIX 4: Calculate actual league-average pace and DEF_RATING from
    the real team data instead of hardcoded constants.
    Falls back to defaults if team_context is empty.
    """
    if not team_context:
        return _DEFAULT_LEAGUE_AVG_PACE, _DEFAULT_LEAGUE_AVG_DEF_RATING

    paces = [v["pace"] for v in team_context.values() if v.get("pace")]
    def_ratings = [v["def_rating"] for v in team_context.values() if v.get("def_rating")]

    avg_pace = sum(paces) / len(paces) if paces else _DEFAULT_LEAGUE_AVG_PACE
    avg_def  = sum(def_ratings) / len(def_ratings) if def_ratings else _DEFAULT_LEAGUE_AVG_DEF_RATING

    print(f"[analyzer] Dynamic league averages: pace={avg_pace:.1f}, DEF_RATING={avg_def:.1f}")
    return avg_pace, avg_def


def _get_context_factors(
    player_team: str,
    opp_abbr: str,
    team_context: dict,
    league_avg_pace: float,
    league_avg_def: float,
) -> tuple[float, float]:
    """Returns (pace_factor, dvp_factor) for a player's matchup."""
    player_pace = team_context.get(player_team, {}).get("pace", league_avg_pace)
    opp_pace    = team_context.get(opp_abbr, {}).get("pace", league_avg_pace)
    game_pace   = (player_pace + opp_pace) / 2.0
    pace_factor = round(game_pace / league_avg_pace, 4) if league_avg_pace > 0 else 1.0

    opp_def_rating = team_context.get(opp_abbr, {}).get("def_rating", league_avg_def)
    dvp_factor = round(league_avg_def / opp_def_rating, 4) if opp_def_rating > 0 else 1.0

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

    # 9. B2B
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
