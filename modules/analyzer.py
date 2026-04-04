from dataclasses import dataclass, field
from modules.fetch_player_stats import get_stat_value, parse_minutes
from modules.fetch_props import MARKET_LABELS

MIN_MINUTES_THRESHOLD = 18.0   # Filter out low-usage players
MIN_GAMES_REQUIRED = 5         # Need at least this many games to analyze
MAX_PICKS_PER_GAME = 3         # Top N picks shown per game
OPP_HISTORY_WEIGHT = 0.10      # Weight for opponent-specific adjustment
LOCATION_WEIGHT = 0.15         # Weight for home/away adjustment
B2B_PENALTY = 0.93             # Performance factor on back-to-back nights


@dataclass
class PlayerPick:
    player: str
    game_label: str
    market_key: str
    market: str          # Human-readable label
    side: str            # "Over" / "Under"
    line: float
    price: int

    avg_l5: float
    avg_l10: float
    avg_l20: float
    hit_count_l10: int
    hit_count_l20: int
    games_l10: int
    games_l20: int
    projection: float
    edge: float           # Positive = projection favors the bet
    consecutive_streak: int

    confidence: str       # "Alta" / "Media" / "Baja" / "Riesgosa"
    headline: str         # One-line natural-language summary
    detail: str           # Supporting stats line
    score: float          # Sorting key (higher = better)
    is_b2b: bool
    injury_status: str | None = None


def analyze_player_props(
    prop_records: list[dict],
    player_logs: dict[str, list[dict]],
    injury_statuses: dict[str, str | None],
    b2b_team_abbrs: set[str],
    games: list[dict],
) -> dict[str, list[PlayerPick]]:
    """
    Main entry point. Returns {game_label: [PlayerPick, ...]} sorted by score.
    """
    # Build game_id → game dict for home/away lookups
    game_by_id = {g["id"]: g for g in games}

    picks_by_game: dict[str, list[PlayerPick]] = {}

    # Group prop records by (player, market_key, game_id) — pick one side per combo
    # Strategy: analyse both Over and Under, keep whichever is better supported
    grouped: dict[tuple, list[dict]] = {}
    for rec in prop_records:
        key = (rec["player"], rec["market_key"], rec["game_id"])
        grouped.setdefault(key, []).append(rec)

    for (player, market_key, game_id), outcomes in grouped.items():
        logs = player_logs.get(player, [])
        game = game_by_id.get(game_id)
        if not game or not logs:
            continue

        # Determine if player is home or away from most recent log
        player_team_abbr = logs[0].get("TEAM_ABBREVIATION", "") if logs else ""
        is_home = player_team_abbr == game["home_team"]["abbreviation"]
        opp_abbr = (
            game["visitor_team"]["abbreviation"] if is_home
            else game["home_team"]["abbreviation"]
        )

        # Is this player on a back-to-back?
        is_b2b = player_team_abbr in b2b_team_abbrs

        game_label = f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}"

        # Try each outcome (Over / Under), keep the best pick
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
            )
            if pick is None:
                continue
            if best_pick is None or pick.score > best_pick.score:
                best_pick = pick

        if best_pick:
            picks_by_game.setdefault(game_label, []).append(best_pick)

    # Sort each game's picks by score descending, cap at MAX_PICKS_PER_GAME
    for label in picks_by_game:
        picks_by_game[label].sort(key=lambda p: p.score, reverse=True)
        picks_by_game[label] = picks_by_game[label][:MAX_PICKS_PER_GAME]

    total = sum(len(v) for v in picks_by_game.values())
    print(f"[analyzer] Total picks selected: {total} across {len(picks_by_game)} games")
    return picks_by_game


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
) -> "PlayerPick | None":

    # Filter players with too few minutes (low usage / garbage time)
    min_vals = [parse_minutes(g.get("MIN", 0)) for g in logs[:10]]
    if min_vals and (sum(min_vals) / len(min_vals)) < MIN_MINUTES_THRESHOLD:
        return None

    # Extract stat values from game logs
    values: list[float] = []
    for g in logs:
        v = get_stat_value(g, market_key)
        if v is not None:
            values.append(v)

    if len(values) < MIN_GAMES_REQUIRED:
        return None

    # --- Core averages ---
    n = len(values)
    avg_l5  = sum(values[:5]) / min(5, n)
    avg_l10 = sum(values[:min(10, n)]) / min(10, n)
    avg_l20 = sum(values[:min(20, n)]) / min(20, n)

    # Weighted projection (recency-biased)
    projection = 0.40 * avg_l5 + 0.35 * avg_l10 + 0.25 * avg_l20

    # --- Hit rates ---
    side_lower = side.lower()

    def _hit(v: float) -> bool:
        return v > line if side_lower == "over" else v < line

    slice_10 = values[:min(10, n)]
    slice_20 = values[:min(20, n)]
    hit_l10 = sum(1 for v in slice_10 if _hit(v))
    hit_l20 = sum(1 for v in slice_20 if _hit(v))
    rate_l10 = hit_l10 / len(slice_10)
    rate_l20 = hit_l20 / len(slice_20)

    # --- Consecutive streak (most recent games) ---
    streak = 0
    for v in values:
        if _hit(v):
            streak += 1
        else:
            break

    # --- Context adjustments ---
    # Home / Away split from historical logs
    home_vals = [get_stat_value(g, market_key) for g in logs if "vs." in g.get("MATCHUP", "")]
    home_vals = [v for v in home_vals if v is not None]
    away_vals = [get_stat_value(g, market_key) for g in logs if " @ " in g.get("MATCHUP", "")]
    away_vals = [v for v in away_vals if v is not None]

    loc_adj = 0.0
    if is_home and len(home_vals) >= 3:
        home_avg = sum(home_vals) / len(home_vals)
        loc_adj = (home_avg - projection) * LOCATION_WEIGHT
    elif not is_home and len(away_vals) >= 3:
        away_avg = sum(away_vals) / len(away_vals)
        loc_adj = (away_avg - projection) * LOCATION_WEIGHT

    # Opponent-specific split (small weight)
    opp_vals = [
        get_stat_value(g, market_key) for g in logs
        if opp_abbr and opp_abbr.upper() in g.get("MATCHUP", "").upper()
    ]
    opp_vals = [v for v in opp_vals if v is not None]
    opp_adj = 0.0
    if len(opp_vals) >= 2:
        opp_avg = sum(opp_vals) / len(opp_vals)
        opp_adj = (opp_avg - projection) * OPP_HISTORY_WEIGHT

    # Back-to-back penalty applied to projection
    adj_projection = projection * (B2B_PENALTY if is_b2b else 1.0) + loc_adj + opp_adj
    adj_projection = round(adj_projection, 1)

    # Edge: how much the projection clears (or falls short of) the line
    if side_lower == "over":
        edge = round(adj_projection - line, 1)
    else:
        edge = round(line - adj_projection, 1)

    # --- Confidence tier ---
    if rate_l10 >= 0.70 and edge >= 1.5:
        confidence = "Alta"
    elif rate_l10 >= 0.60 and edge >= 0.3:
        confidence = "Media"
    elif rate_l10 >= 0.50 or edge >= 0.5:
        confidence = "Baja"
    else:
        confidence = "Riesgosa"

    # --- Natural language headline ---
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
        is_b2b=is_b2b,
        opp_vals=opp_vals,
        opp_avg=sum(opp_vals) / len(opp_vals) if opp_vals else None,
        opp_abbr=opp_abbr,
        is_home=is_home,
        home_vals=home_vals,
        away_vals=away_vals,
    )

    detail = (
        f"L5: {avg_l5:.1f} | L10: {avg_l10:.1f} | L20: {avg_l20:.1f} | "
        f"Hit L10: {hit_l10}/{len(slice_10)} ({rate_l10*100:.0f}%) | "
        f"Hit L20: {hit_l20}/{len(slice_20)} ({rate_l20*100:.0f}%)"
    )

    # Sorting score: blend of hit rate and normalized edge
    normalized_edge = edge / max(line, 1.0)
    score = rate_l10 * 0.55 + normalized_edge * 0.30 + rate_l20 * 0.15

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
        consecutive_streak=streak,
        confidence=confidence,
        headline=headline,
        detail=detail,
        score=score,
        is_b2b=is_b2b,
        injury_status=injury_status,
    )


def _build_headline(
    player, market_label, side, line, values, streak,
    avg_l5, avg_l10, hit_l10, rate_l10, edge, adj_projection,
    is_b2b, opp_vals, opp_avg, opp_abbr, is_home, home_vals, away_vals,
) -> str:
    n10 = min(10, len(values))
    parts = []

    # 1. Lead with streak if strong
    if streak >= 4:
        parts.append(
            f"Lleva {streak} partidos consecutivos {side} {line} en {market_label}"
        )
    elif streak >= 2:
        parts.append(
            f"{streak} partidos seguidos {side} {line}"
        )

    # 2. Hit rate headline if no prominent streak
    if not parts or streak < 4:
        if rate_l10 >= 0.70:
            parts.append(
                f"Fue {side} en {hit_l10}/{n10} últimos partidos "
                f"({rate_l10*100:.0f}%) — promedio L10: {avg_l10:.1f}"
            )
        elif rate_l10 >= 0.60:
            parts.append(
                f"{hit_l10}/{n10} veces {side} en últimos partidos — "
                f"promedio L10: {avg_l10:.1f}"
            )
        else:
            parts.append(
                f"Promedio L5: {avg_l5:.1f} | L10: {avg_l10:.1f} vs línea {line}"
            )

    # 3. Projection edge
    if edge > 0:
        parts.append(f"Proyección: {adj_projection} (+{edge} sobre la línea)")
    elif edge < 0:
        parts.append(f"Proyección: {adj_projection} ({edge} bajo la línea) ⚠️")

    # 4. Contextual flags
    if is_b2b:
        parts.append("Back-to-back — rendimiento suele bajar ~7%")

    if opp_avg is not None and len(opp_vals) >= 2:
        parts.append(
            f"Vs {opp_abbr}: promedia {opp_avg:.1f} en {len(opp_vals)} partido(s)"
        )

    loc_label = "local" if is_home else "visitante"
    loc_vals = home_vals if is_home else away_vals
    if len(loc_vals) >= 3:
        loc_avg = sum(loc_vals) / len(loc_vals)
        parts.append(f"De {loc_label}: {loc_avg:.1f} promedio")

    return " | ".join(parts)
