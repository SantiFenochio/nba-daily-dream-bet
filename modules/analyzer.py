from dataclasses import dataclass, field
from math import exp


HOME_COURT_BONUS = 2.5       # pts advantage for home team (statistically documented)
BACK_TO_BACK_PENALTY = 0.04  # 4% scoring reduction on back-to-back nights

# Recent form vs season average blend (research: 20-game window optimal, 60/40 split)
RECENT_WEIGHT = 0.60
SEASON_WEIGHT = 0.40

# H2H modifier cap: max ±2 pts regardless of historical dominance
H2H_MAX_MODIFIER = 2.0
# Minimum H2H games needed to apply a modifier
H2H_MIN_GAMES = 3


@dataclass
class Pick:
    game_label: str
    home_team: str
    visitor_team: str
    recommended_bet: str
    reasoning: str
    confidence: str
    props: list[dict] = field(default_factory=list)
    totals_bet: str = ""
    totals_reasoning: str = ""
    market_spread: float = 0.0
    market_total: float = 0.0
    home_back_to_back: bool = False
    visitor_back_to_back: bool = False
    model_edge: float = 0.0
    # New fields
    home_recent_win_pct: float = 0.0
    visitor_recent_win_pct: float = 0.0
    home_streak: int = 0
    visitor_streak: int = 0
    h2h_games: int = 0
    h2h_summary: str = ""


def analyze_games(
    games: list[dict],
    stats: dict,
    props: dict,
    game_odds: dict,
    rest_info: dict,
    recent_form: dict | None = None,
    h2h: dict | None = None,
) -> list[Pick]:
    picks = []
    for game in games:
        pick = _analyze_game(
            game,
            stats,
            props.get(game["id"], []),
            game_odds.get(game["id"], {}),
            rest_info,
            (recent_form or {}).get(game["home_team"]["id"], {}),
            (recent_form or {}).get(game["visitor_team"]["id"], {}),
            (h2h or {}).get(game["id"], {}),
        )
        picks.append(pick)
    return picks


def _analyze_game(
    game: dict,
    stats: dict,
    game_props: list[dict],
    odds: dict,
    rest_info: dict,
    home_form: dict,
    visitor_form: dict,
    h2h_data: dict,
) -> Pick:
    home = game["home_team"]
    visitor = game["visitor_team"]
    home_stats = stats.get(home["id"], {})
    visitor_stats = stats.get(visitor["id"], {})

    # --- Season averages ---
    home_pts_season = home_stats.get("pts", 0) or 0
    visitor_pts_season = visitor_stats.get("pts", 0) or 0
    home_fg_season = home_stats.get("fg_pct", 0) or 0
    visitor_fg_season = visitor_stats.get("fg_pct", 0) or 0
    home_def_season = (home_stats.get("blk", 0) or 0) + (home_stats.get("stl", 0) or 0)
    visitor_def_season = (visitor_stats.get("blk", 0) or 0) + (visitor_stats.get("stl", 0) or 0)

    # --- Recent form (last ~15-20 games) ---
    home_pts_recent = home_form.get("recent_pts", 0)
    visitor_pts_recent = visitor_form.get("recent_pts", 0)
    home_allowed_recent = home_form.get("recent_pts_allowed", 0)
    visitor_allowed_recent = visitor_form.get("recent_pts_allowed", 0)
    home_win_pct = home_form.get("recent_win_pct", 0.5)
    visitor_win_pct = visitor_form.get("recent_win_pct", 0.5)
    home_streak = home_form.get("streak", 0)
    visitor_streak = visitor_form.get("streak", 0)
    home_form_games = home_form.get("games_count", 0)
    visitor_form_games = visitor_form.get("games_count", 0)

    # Blend recent form with season (only when we have enough recent data)
    if home_form_games >= 5:
        home_pts = home_pts_recent * RECENT_WEIGHT + home_pts_season * SEASON_WEIGHT
    else:
        home_pts = home_pts_season

    if visitor_form_games >= 5:
        visitor_pts = visitor_pts_recent * RECENT_WEIGHT + visitor_pts_season * SEASON_WEIGHT
    else:
        visitor_pts = visitor_pts_season

    # FG% from season (recent form doesn't track fg%)
    home_fg = home_fg_season
    visitor_fg = visitor_fg_season

    # Defense: use recent pts_allowed as defensive proxy when available
    if home_form_games >= 5:
        home_def = (home_pts_season - home_allowed_recent) * RECENT_WEIGHT + home_def_season * SEASON_WEIGHT
    else:
        home_def = home_def_season

    if visitor_form_games >= 5:
        visitor_def = (visitor_pts_season - visitor_allowed_recent) * RECENT_WEIGHT + visitor_def_season * SEASON_WEIGHT
    else:
        visitor_def = visitor_def_season

    # --- Back-to-back fatigue ---
    home_b2b = rest_info.get(home["id"], {}).get("is_back_to_back", False)
    visitor_b2b = rest_info.get(visitor["id"], {}).get("is_back_to_back", False)
    home_rest = rest_info.get(home["id"], {}).get("rest_days", 3)
    visitor_rest = rest_info.get(visitor["id"], {}).get("rest_days", 3)

    home_pts_adj = home_pts * (1 - BACK_TO_BACK_PENALTY if home_b2b else 1)
    visitor_pts_adj = visitor_pts * (1 - BACK_TO_BACK_PENALTY if visitor_b2b else 1)

    # --- Scoring formula: offense + defense proxy + home court ---
    home_score = home_pts_adj * 0.5 + home_fg * 100 * 0.3 + max(home_def, 0) * 0.5 * 0.2 + HOME_COURT_BONUS
    visitor_score = visitor_pts_adj * 0.5 + visitor_fg * 100 * 0.3 + max(visitor_def, 0) * 0.5 * 0.2

    # --- H2H modifier (small, capped, requires minimum sample) ---
    h2h_modifier = 0.0
    h2h_games = h2h_data.get("h2h_games", 0)
    h2h_summary = ""
    if h2h_games >= H2H_MIN_GAMES:
        avg_margin = h2h_data.get("avg_margin_home", 0.0)
        # Scale: 5-pt historical margin → ~1 pt modifier; cap at H2H_MAX_MODIFIER
        raw_modifier = avg_margin * 0.2
        h2h_modifier = max(-H2H_MAX_MODIFIER, min(H2H_MAX_MODIFIER, raw_modifier))
        home_h2h_wins = h2h_data.get("home_wins", 0)
        visitor_h2h_wins = h2h_data.get("visitor_wins", 0)
        h2h_summary = (
            f"{home['full_name']} {home_h2h_wins}-{visitor_h2h_wins} en H2H "
            f"(últ. {h2h_games} partidos, margen prom. {avg_margin:+.1f})"
        )

    predicted_margin = (home_score + h2h_modifier) - visitor_score

    # --- Market data ---
    market_spread = odds.get("spread", 0.0)
    market_total = odds.get("total", 0.0)
    implied_prob_home = odds.get("implied_prob_home")

    # --- Confidence ---
    model_edge, confidence = _calculate_confidence(predicted_margin, implied_prob_home, market_spread)

    # --- Winner recommendation ---
    if predicted_margin >= 0:
        recommended_bet = f"{home['full_name']} gana como local"
        winner_name, loser_name = home["full_name"], visitor["full_name"]
        winner_pts, winner_fg = home_pts, home_fg
        loser_pts, loser_fg = visitor_pts, visitor_fg
    else:
        recommended_bet = f"{visitor['full_name']} gana como visitante"
        winner_name, loser_name = visitor["full_name"], home["full_name"]
        winner_pts, winner_fg = visitor_pts, visitor_fg
        loser_pts, loser_fg = home_pts, home_fg

    # --- Reasoning ---
    form_tag = ""
    if home_form_games >= 5 or visitor_form_games >= 5:
        form_tag = f" (blend últ. {max(home_form_games, visitor_form_games)} partidos)"

    fatigue_parts = []
    if home_b2b:
        fatigue_parts.append(f"{home['full_name']} en B2B")
    if visitor_b2b:
        fatigue_parts.append(f"{visitor['full_name']} en B2B")
    fatigue_str = f" ⚠️ {', '.join(fatigue_parts)}." if fatigue_parts else ""

    rest_str = (
        f" Descanso: local {home_rest}d / visitante {visitor_rest}d."
        if (home_rest != 3 or visitor_rest != 3) else ""
    )

    reasoning = (
        f"{winner_name} promedia {winner_pts:.1f} pts ({winner_fg*100:.1f}% FG)"
        f" vs {loser_pts:.1f} pts ({loser_fg*100:.1f}% FG) de {loser_name}{form_tag}."
        f" Ventaja local (+{HOME_COURT_BONUS} pts).{fatigue_str}{rest_str}"
    )

    # --- Totals recommendation ---
    totals_bet = ""
    totals_reasoning = ""
    if market_total > 0:
        if home_b2b and visitor_b2b:
            totals_bet = f"Under {market_total}"
            totals_reasoning = "Ambos equipos en B2B → fatiga reduce anotación (~6-10 pts menos)."
        elif home_b2b or visitor_b2b:
            b2b_team = home["full_name"] if home_b2b else visitor["full_name"]
            totals_bet = f"Under {market_total}"
            totals_reasoning = f"{b2b_team} en B2B → tendencia al Under por fatiga."

    top_props = _extract_top_props(game_props)

    return Pick(
        game_label=f"{visitor['full_name']} @ {home['full_name']}",
        home_team=home["full_name"],
        visitor_team=visitor["full_name"],
        recommended_bet=recommended_bet,
        reasoning=reasoning,
        confidence=confidence,
        props=top_props,
        totals_bet=totals_bet,
        totals_reasoning=totals_reasoning,
        market_spread=market_spread,
        market_total=market_total,
        home_back_to_back=home_b2b,
        visitor_back_to_back=visitor_b2b,
        model_edge=model_edge,
        home_recent_win_pct=home_win_pct,
        visitor_recent_win_pct=visitor_win_pct,
        home_streak=home_streak,
        visitor_streak=visitor_streak,
        h2h_games=h2h_games,
        h2h_summary=h2h_summary,
    )


def _margin_to_probability(margin: float) -> float:
    """Convert predicted score margin to win probability using logistic sigmoid.
    Divisor 8.0: ~8-pt advantage ≈ 73% win probability, consistent with NBA research.
    """
    return 1 / (1 + exp(-margin / 8.0))


def _calculate_confidence(
    margin: float,
    implied_prob_home: float | None,
    market_spread: float,
) -> tuple[float, str]:
    """Return (model_edge, confidence_label).

    When moneyline data is available, confidence is based on the edge between
    the model's win probability and the book's vig-free implied probability.
    Falls back to margin-based system when market data is missing.
    """
    model_prob_home = _margin_to_probability(margin)

    if implied_prob_home is not None:
        if margin >= 0:
            edge = model_prob_home - implied_prob_home
        else:
            edge = (1 - model_prob_home) - (1 - implied_prob_home)

        if edge >= 0.08:
            return edge, "Alta"
        elif edge >= 0.04:
            return edge, "Media"
        elif edge >= 0:
            return edge, "Baja"
        else:
            return 0.0, "Baja"
    else:
        market_agrees = (
            (margin > 0 and market_spread <= 0) or
            (margin < 0 and market_spread > 0)
        ) if market_spread != 0 else True

        abs_margin = abs(margin)
        if abs_margin < 2:
            return 0.0, "Baja"
        elif abs_margin < 5:
            return 0.0, "Media"
        else:
            return 0.0, "Alta" if market_agrees else "Media"


def _extract_top_props(bookmakers: list[dict]) -> list[dict]:
    seen = {}
    for bookmaker in bookmakers:
        for market in bookmaker.get("markets", []):
            key = market.get("key", "")
            for outcome in market.get("outcomes", []):
                player = outcome.get("description", "")
                point = outcome.get("point", "")
                name = outcome.get("name", "")
                prop_key = f"{player}_{key}_{name}"
                if prop_key not in seen:
                    seen[prop_key] = {
                        "player": player,
                        "market": _market_label(key),
                        "line": point,
                        "side": name,
                        "price": outcome.get("price", ""),
                    }

    return list(seen.values())[:5]


def _market_label(key: str) -> str:
    labels = {
        "player_points": "Puntos",
        "player_rebounds": "Rebotes",
        "player_assists": "Asistencias",
    }
    return labels.get(key, key)
