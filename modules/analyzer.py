from dataclasses import dataclass, field
from math import exp


HOME_COURT_BONUS = 2.5       # pts advantage for home team (statistically documented)
BACK_TO_BACK_PENALTY = 0.04  # 4% scoring reduction on back-to-back nights


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
    model_edge: float = 0.0   # model win prob minus book implied prob (positive = value)


def analyze_games(
    games: list[dict],
    stats: dict,
    props: dict,
    game_odds: dict,
    rest_info: dict,
) -> list[Pick]:
    picks = []
    for game in games:
        pick = _analyze_game(
            game,
            stats,
            props.get(game["id"], []),
            game_odds.get(game["id"], {}),
            rest_info,
        )
        picks.append(pick)
    return picks


def _analyze_game(
    game: dict,
    stats: dict,
    game_props: list[dict],
    odds: dict,
    rest_info: dict,
) -> Pick:
    home = game["home_team"]
    visitor = game["visitor_team"]
    home_stats = stats.get(home["id"], {})
    visitor_stats = stats.get(visitor["id"], {})

    home_pts = home_stats.get("pts", 0) or 0
    visitor_pts = visitor_stats.get("pts", 0) or 0
    home_fg = home_stats.get("fg_pct", 0) or 0
    visitor_fg = visitor_stats.get("fg_pct", 0) or 0

    # Defensive proxy: blocks + steals as quality signal (available in free tier)
    home_def = (home_stats.get("blk", 0) or 0) + (home_stats.get("stl", 0) or 0)
    visitor_def = (visitor_stats.get("blk", 0) or 0) + (visitor_stats.get("stl", 0) or 0)

    # Factor: back-to-back fatigue
    home_b2b = rest_info.get(home["id"], {}).get("is_back_to_back", False)
    visitor_b2b = rest_info.get(visitor["id"], {}).get("is_back_to_back", False)
    home_rest = rest_info.get(home["id"], {}).get("rest_days", 3)
    visitor_rest = rest_info.get(visitor["id"], {}).get("rest_days", 3)

    home_pts_adj = home_pts * (1 - BACK_TO_BACK_PENALTY if home_b2b else 1)
    visitor_pts_adj = visitor_pts * (1 - BACK_TO_BACK_PENALTY if visitor_b2b else 1)

    # Scoring formula: offense (pts + fg%) + defensive proxy + home court
    # Weights: pts 50%, fg% 30%, defense 20%
    home_score = home_pts_adj * 0.5 + home_fg * 100 * 0.3 + home_def * 0.5 * 0.2 + HOME_COURT_BONUS
    visitor_score = visitor_pts_adj * 0.5 + visitor_fg * 100 * 0.3 + visitor_def * 0.5 * 0.2

    predicted_margin = home_score - visitor_score

    # Market data
    market_spread = odds.get("spread", 0.0)
    market_total = odds.get("total", 0.0)
    implied_prob_home = odds.get("implied_prob_home")  # vig-free, or None if unavailable

    # Confidence via implied probability edge (preferred) or margin fallback
    model_edge, confidence = _calculate_confidence(predicted_margin, implied_prob_home, market_spread)

    # Winner recommendation
    if predicted_margin >= 0:
        recommended_bet = f"{home['full_name']} gana como local"
        winner_name = home["full_name"]
        winner_pts, winner_fg, winner_def = home_pts, home_fg, home_def
        loser_name = visitor["full_name"]
        loser_pts, loser_fg, loser_def = visitor_pts, visitor_fg, visitor_def
    else:
        recommended_bet = f"{visitor['full_name']} gana como visitante"
        winner_name = visitor["full_name"]
        winner_pts, winner_fg, winner_def = visitor_pts, visitor_fg, visitor_def
        loser_name = home["full_name"]
        loser_pts, loser_fg, loser_def = home_pts, home_fg, home_def

    # Build reasoning
    def_str = f", def. {winner_def:.1f} blk+stl" if winner_def > 0 else ""
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
        f"{winner_name} promedia {winner_pts:.1f} pts ({winner_fg*100:.1f}% FG{def_str})"
        f" vs {loser_pts:.1f} pts ({loser_fg*100:.1f}% FG) de {loser_name}."
        f" Ventaja local aplicada (+{HOME_COURT_BONUS} pts).{fatigue_str}{rest_str}"
    )

    # Totals recommendation based on back-to-back research
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
        # Edge from the perspective of our predicted winner (positive = we give higher prob than book)
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
            # Book is more confident than our model on this outcome → no value, flag as Baja
            return 0.0, "Baja"
    else:
        # Fallback: margin-based with market direction check
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
