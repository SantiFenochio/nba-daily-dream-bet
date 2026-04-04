"""
Player prop analyzer.

Improvements implemented:
  - Mejora 1:  True EV via devig (no-vig probability stripping)
  - Mejora 10: Poisson distribution for probability estimation
  - Mejora 14: Bayesian Laplace smoothing for small samples
  - EV filter:  Skip picks where model prob <= market fair prob (no edge)
"""

from dataclasses import dataclass, field
from scipy.stats import poisson as poisson_dist

from modules.fetch_player_stats import get_stat_value, parse_minutes
from modules.fetch_props import MARKET_LABELS

# ── Tunable constants ────────────────────────────────────────────────────
MIN_MINUTES_THRESHOLD = 18.0   # Filter out low-usage / garbage-time players
MIN_GAMES_REQUIRED    = 5      # Minimum game log sample
MAX_PICKS_PER_GAME    = 3      # Top N picks per game in the message
OPP_HISTORY_WEIGHT    = 0.10   # Weight of opponent-specific historical adj
LOCATION_WEIGHT       = 0.15   # Weight of home/away historical split
B2B_PENALTY           = 0.93   # Performance multiplier on back-to-back nights
LAPLACE_PRIOR         = 0.50   # Bayesian prior probability (50/50)
LAPLACE_WEIGHT        = 4      # Equivalent prior game observations
MIN_EV_THRESHOLD      = 2.0    # Minimum EV% to include a pick (filter noise)
QUARTER_KELLY         = 0.25   # Fraction of full Kelly to recommend
LEAGUE_AVG_DEF_RATING = 113.5  # Current season approximation
LEAGUE_AVG_PACE       = 99.5   # Current season approximation
# ─────────────────────────────────────────────────────────────────────────


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
    projection: float          # Adjusted projection
    edge: float                # Stat-unit edge (projection - line)

    # ── New probability / EV fields ─────────────────────────────────────
    model_prob: float          # Poisson + Bayesian calibrated probability
    fair_prob: float           # No-vig (deviggged) market probability
    ev_pct: float              # Expected value as % of stake
    kelly_pct: float           # Quarter-Kelly recommended stake %
    # ────────────────────────────────────────────────────────────────────

    consecutive_streak: int
    confidence: str
    headline: str
    detail: str
    score: float
    is_b2b: bool
    injury_status: str | None = None

    # Context factors applied
    pace_factor: float = 1.0
    dvp_factor: float = 1.0


# ══════════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════════

def analyze_player_props(
    prop_records: list[dict],
    player_logs: dict[str, list[dict]],
    injury_statuses: dict[str, str | None],
    b2b_team_abbrs: set[str],
    games: list[dict],
    team_context: dict | None = None,  # {team_abbr: {pace, def_rating, ...}}
) -> dict[str, list["PlayerPick"]]:
    """
    Returns {game_label: [PlayerPick, ...]} sorted by EV, capped at MAX_PICKS_PER_GAME.
    team_context comes from fetch_context.get_team_context().
    """
    game_by_id = {g["id"]: g for g in games}
    tc = team_context or {}

    picks_by_game: dict[str, list[PlayerPick]] = {}

    # Group outcomes by (player, market_key, game_id) → analyse both Over & Under
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
        is_home = player_team_abbr == game["home_team"]["abbreviation"]
        opp_abbr = (
            game["visitor_team"]["abbreviation"] if is_home
            else game["home_team"]["abbreviation"]
        )
        is_b2b = player_team_abbr in b2b_team_abbrs
        game_label = f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}"

        # Build context factors for this matchup
        pace_factor, dvp_factor = _get_context_factors(
            player_team_abbr, opp_abbr, tc
        )

        best_pick = None
        for rec in outcomes:
            pick = _analyze_one_prop(
                player=player,
                market_key=market_key,
                line=rec["line"],
                side=rec["side"],
                price=rec["price"],
                logs=logs,
                is_home=is_home,
                is_b2b=is_b2b,
                opp_abbr=opp_abbr,
                game_label=game_label,
                injury_status=injury_statuses.get(player),
                pace_factor=pace_factor,
                dvp_factor=dvp_factor,
            )
            if pick is None:
                continue
            if best_pick is None or pick.score > best_pick.score:
                best_pick = pick

        if best_pick:
            picks_by_game.setdefault(game_label, []).append(best_pick)

    # Sort by score (incorporates EV), cap per game
    for label in picks_by_game:
        picks_by_game[label].sort(key=lambda p: p.score, reverse=True)
        picks_by_game[label] = picks_by_game[label][:MAX_PICKS_PER_GAME]

    total = sum(len(v) for v in picks_by_game.values())
    print(f"[analyzer] Total picks selected: {total} across {len(picks_by_game)} games")
    return picks_by_game


# ══════════════════════════════════════════════════════════════════════════
# Core analysis for a single prop outcome
# ══════════════════════════════════════════════════════════════════════════

def _analyze_one_prop(
    player: str,
    market_key: str,
    line: float,
    side: str,
    price: int,
    logs: list[dict],
    is_home: bool,
    is_b2b: bool,
    opp_abbr: str,
    game_label: str,
    injury_status: str | None,
    pace_factor: float,
    dvp_factor: float,
) -> "PlayerPick | None":

    # ── Minutes filter ────────────────────────────────────────────────────
    min_vals = [parse_minutes(g.get("MIN", 0)) for g in logs[:10]]
    if min_vals and (sum(min_vals) / len(min_vals)) < MIN_MINUTES_THRESHOLD:
        return None

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
    avg_l5  = sum(values[:5])        / min(5, n)
    avg_l10 = sum(values[:min(10,n)]) / min(10, n)
    avg_l20 = sum(values[:min(20,n)]) / min(20, n)

    # Recency-weighted base projection
    base_projection = 0.40 * avg_l5 + 0.35 * avg_l10 + 0.25 * avg_l20

    # ── Context adjustments ───────────────────────────────────────────────
    side_lower = side.lower()

    def _hit(v: float) -> bool:
        return v > line if side_lower == "over" else v < line

    slice_10 = values[:min(10, n)]
    slice_20 = values[:min(20, n)]
    hit_l10 = sum(1 for v in slice_10 if _hit(v))
    hit_l20 = sum(1 for v in slice_20 if _hit(v))
    rate_l10 = hit_l10 / len(slice_10)
    rate_l20 = hit_l20 / len(slice_20)

    # Home / Away historical split
    home_vals = [get_stat_value(g, market_key) for g in logs if "vs." in g.get("MATCHUP", "")]
    home_vals = [v for v in home_vals if v is not None]
    away_vals = [get_stat_value(g, market_key) for g in logs if " @ " in g.get("MATCHUP", "")]
    away_vals = [v for v in away_vals if v is not None]

    loc_adj = 0.0
    if is_home and len(home_vals) >= 3:
        loc_adj = (sum(home_vals)/len(home_vals) - base_projection) * LOCATION_WEIGHT
    elif not is_home and len(away_vals) >= 3:
        loc_adj = (sum(away_vals)/len(away_vals) - base_projection) * LOCATION_WEIGHT

    # Opponent-specific historical split
    opp_vals = [
        get_stat_value(g, market_key) for g in logs
        if opp_abbr and opp_abbr.upper() in g.get("MATCHUP", "").upper()
    ]
    opp_vals = [v for v in opp_vals if v is not None]
    opp_adj = 0.0
    if len(opp_vals) >= 2:
        opp_adj = (sum(opp_vals)/len(opp_vals) - base_projection) * OPP_HISTORY_WEIGHT

    # Apply all adjustments multiplicatively then add additive context
    adj_projection = (
        base_projection
        * (B2B_PENALTY if is_b2b else 1.0)
        * pace_factor
        * dvp_factor
        + loc_adj
        + opp_adj
    )
    adj_projection = max(0.0, round(adj_projection, 1))

    # Stat-unit edge
    if side_lower == "over":
        edge = round(adj_projection - line, 1)
    else:
        edge = round(line - adj_projection, 1)

    # ── Mejora 10: Poisson probability ───────────────────────────────────
    poisson_prob = _poisson_prob(adj_projection, line, side_lower)

    # ── Mejora 14: Bayesian Laplace-smoothed hit rate ────────────────────
    raw_hit_rate = rate_l10
    bayes_prob = _bayesian_prob(hit_l10, len(slice_10))

    # Blend Poisson + Bayesian (50/50) for final model probability
    model_prob = 0.50 * poisson_prob + 0.50 * bayes_prob

    # ── Mejora 1: Devig + EV ─────────────────────────────────────────────
    # Find the opposite side price (Over ↔ Under) from the same record
    # We don't have it here directly, so we use the single-side devig
    # with a typical vig assumption (-110 juice on each side) as fallback
    fair_prob = _devig_single(price)
    ev_pct = _expected_value_pct(model_prob, price)

    # EV filter: skip if model sees no edge over the fair market price
    if ev_pct < MIN_EV_THRESHOLD:
        return None

    # ── Quarter-Kelly stake suggestion ────────────────────────────────────
    kelly_pct = _quarter_kelly(model_prob, price)

    # ── Consecutive streak ────────────────────────────────────────────────
    streak = 0
    for v in values:
        if _hit(v):
            streak += 1
        else:
            break

    # ── Confidence tier (now EV-driven primarily) ─────────────────────────
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
        opp_avg=sum(opp_vals)/len(opp_vals) if opp_vals else None,
        opp_abbr=opp_abbr,
        is_home=is_home,
        home_vals=home_vals,
        away_vals=away_vals,
        pace_factor=pace_factor,
        dvp_factor=dvp_factor,
    )

    detail = (
        f"L5: {avg_l5:.1f} | L10: {avg_l10:.1f} | L20: {avg_l20:.1f} | "
        f"Hit L10: {hit_l10}/{len(slice_10)} ({rate_l10*100:.0f}%) | "
        f"Hit L20: {hit_l20}/{len(slice_20)} ({rate_l20*100:.0f}%)"
    )

    # Score = EV (primary) + hit rate bonus
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
    )


# ══════════════════════════════════════════════════════════════════════════
# Probability / EV helpers  (Mejoras 1, 10, 14)
# ══════════════════════════════════════════════════════════════════════════

def _poisson_prob(lambda_rate: float, line: float, side: str) -> float:
    """
    Mejora 10 — Poisson CDF probability for count stats.
    P(X > line) = 1 - P(X <= floor(line))
    P(X < line) = P(X <= floor(line - 1))
    Lines are typically x.5, so floor(line) = line - 0.5.
    """
    if lambda_rate <= 0:
        return 0.5
    floor_line = int(line)  # e.g. 24.5 → 24
    try:
        if side == "over":
            return float(1.0 - poisson_dist.cdf(floor_line, mu=lambda_rate))
        else:
            return float(poisson_dist.cdf(floor_line - 1, mu=lambda_rate))
    except Exception:
        return 0.5


def _bayesian_prob(hits: int, games: int) -> float:
    """
    Mejora 14 — Laplace-smoothed hit rate.
    Pulls extreme small-sample estimates toward 50% prior.
    Formula: (hits + prior*weight) / (games + weight)
    """
    return (hits + LAPLACE_PRIOR * LAPLACE_WEIGHT) / (games + LAPLACE_WEIGHT)


def american_to_implied_prob(american_odds: int) -> float:
    """Raw implied probability from American odds (includes vig)."""
    if american_odds > 0:
        return 100.0 / (american_odds + 100.0)
    return abs(american_odds) / (abs(american_odds) + 100.0)


def _devig_single(price: int) -> float:
    """
    Mejora 1 — Single-side devig.
    Without the opposite side price, assume a symmetric market
    (typical -110/-110 line) and devig accordingly.
    Returns fair (no-vig) probability for this side.
    """
    implied = american_to_implied_prob(price)
    # Assume symmetric vig: total overround split 50/50
    # fair_prob = implied / (implied + (1 - implied)) = implied (symmetric)
    # Better: assume other side is also implied. Total = implied + (1-implied) ≈ 1 + vig
    # For -110/-110: each side implied = 52.38%, total = 104.76%, vig = 4.76%
    # fair = 52.38 / 104.76 = 50%
    # Generalise: assume other side price mirrors this one
    other_implied = 1.0 - implied + (implied - 0.5) * 0.05  # slight asymmetry assumption
    total = implied + other_implied
    return implied / total


def _expected_value_pct(model_prob: float, american_odds: int) -> float:
    """
    Mejora 1 — EV as a percentage of stake.
    EV% = (model_prob × net_win) - ((1-model_prob) × 100)
    Net win for +150 = 150; for -115 = 100/1.15 ≈ 86.96
    """
    if american_odds >= 0:
        net_win = float(american_odds)
    else:
        net_win = 10000.0 / abs(american_odds)
    ev = (model_prob * net_win) - ((1.0 - model_prob) * 100.0)
    return round(ev, 2)


def _quarter_kelly(model_prob: float, american_odds: int) -> float:
    """
    Quarter-Kelly criterion stake as % of bankroll.
    f = (b*p - q) / b   where b = decimal_odds - 1
    Returns max(0, f * 0.25) capped at 5%.
    """
    if american_odds >= 0:
        decimal = american_odds / 100.0 + 1.0
    else:
        decimal = 100.0 / abs(american_odds) + 1.0
    b = decimal - 1.0
    if b <= 0:
        return 0.0
    p = model_prob
    q = 1.0 - p
    full_kelly = (b * p - q) / b
    qk = max(0.0, full_kelly * QUARTER_KELLY)
    return round(min(qk * 100.0, 5.0), 2)  # return as %, cap at 5%


# ══════════════════════════════════════════════════════════════════════════
# Context factors  (Mejoras 5 + 7)
# ══════════════════════════════════════════════════════════════════════════

def _get_context_factors(
    player_team: str,
    opp_abbr: str,
    team_context: dict,
) -> tuple[float, float]:
    """
    Returns (pace_factor, dvp_factor) for a player's matchup.

    pace_factor: how this game's expected pace compares to league average.
      > 1.0 means more possessions → more stat opportunities.

    dvp_factor: opponent defensive rating factor.
      > 1.0 means weak defense → easier to score.
    """
    player_pace = team_context.get(player_team, {}).get("pace", LEAGUE_AVG_PACE)
    opp_pace    = team_context.get(opp_abbr, {}).get("pace", LEAGUE_AVG_PACE)
    game_pace   = (player_pace + opp_pace) / 2.0
    pace_factor = round(game_pace / LEAGUE_AVG_PACE, 4)

    opp_def_rating = team_context.get(opp_abbr, {}).get("def_rating", LEAGUE_AVG_DEF_RATING)
    dvp_factor = round(LEAGUE_AVG_DEF_RATING / opp_def_rating, 4) if opp_def_rating > 0 else 1.0

    return pace_factor, dvp_factor


# ══════════════════════════════════════════════════════════════════════════
# Natural language headline builder
# ══════════════════════════════════════════════════════════════════════════

def _build_headline(
    player, market_label, side, line, values, streak,
    avg_l5, avg_l10, hit_l10, rate_l10, edge, adj_projection,
    ev_pct, model_prob,
    is_b2b, opp_vals, opp_avg, opp_abbr,
    is_home, home_vals, away_vals,
    pace_factor, dvp_factor,
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

    # 4. EV statement
    parts.append(f"EV: +{ev_pct:.1f}% | Prob. modelo: {model_prob*100:.0f}%")

    # 5. Pace
    if pace_factor >= 1.03:
        parts.append(f"Ritmo de juego elevado (+{(pace_factor-1)*100:.1f}% posesiones)")
    elif pace_factor <= 0.97:
        parts.append(f"Ritmo de juego lento (-{(1-pace_factor)*100:.1f}% posesiones)")

    # 6. DVPOP
    if dvp_factor >= 1.04:
        parts.append(f"Defensa rival débil — factor DvP: +{(dvp_factor-1)*100:.1f}%")
    elif dvp_factor <= 0.97:
        parts.append(f"Defensa rival sólida — factor DvP: {(dvp_factor-1)*100:.1f}%")

    # 7. B2B flag
    if is_b2b:
        parts.append("Back-to-back — rendimiento suele bajar ~7%")

    # 8. Opponent history
    if opp_avg is not None and len(opp_vals) >= 2:
        parts.append(f"Vs {opp_abbr}: promedia {opp_avg:.1f} en {len(opp_vals)} partido(s)")

    # 9. Location split
    loc_vals = home_vals if is_home else away_vals
    loc_label = "local" if is_home else "visitante"
    if len(loc_vals) >= 3:
        parts.append(f"De {loc_label}: {sum(loc_vals)/len(loc_vals):.1f} promedio")

    return " | ".join(parts)
