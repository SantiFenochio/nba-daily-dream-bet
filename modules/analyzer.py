from dataclasses import dataclass, field


HOME_COURT_BONUS = 2.5      # pts advantage for home team (statistically documented)
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

    # Factor: back-to-back fatigue
    home_b2b = rest_info.get(home["id"], {}).get("is_back_to_back", False)
    visitor_b2b = rest_info.get(visitor["id"], {}).get("is_back_to_back", False)
    home_rest = rest_info.get(home["id"], {}).get("rest_days", 3)
    visitor_rest = rest_info.get(visitor["id"], {}).get("rest_days", 3)

    home_pts_adj = home_pts * (1 - BACK_TO_BACK_PENALTY if home_b2b else 1)
    visitor_pts_adj = visitor_pts * (1 - BACK_TO_BACK_PENALTY if visitor_b2b else 1)

    # Factor: home court advantage
    home_score = home_pts_adj * 0.6 + home_fg * 100 * 0.4 + HOME_COURT_BONUS
    visitor_score = visitor_pts_adj * 0.6 + visitor_fg * 100 * 0.4

    predicted_margin = home_score - visitor_score

    # Factor: calibrate confidence with market spread
    market_spread = odds.get("spread", 0.0)  # negative = home is favored
    market_total = odds.get("total", 0.0)
    market_agrees = (
        (predicted_margin > 0 and market_spread <= 0) or
        (predicted_margin < 0 and market_spread > 0)
    ) if market_spread != 0 else True

    margin = abs(predicted_margin)
    if margin < 2:
        confidence = "Baja"
    elif margin < 5:
        confidence = "Media"
    else:
        confidence = "Alta" if market_agrees else "Media"

    # Winner recommendation
    if predicted_margin >= 0:
        recommended_bet = f"{home['full_name']} gana como local"
        winner_name = home["full_name"]
        winner_pts, winner_fg = home_pts, home_fg
        loser_name = visitor["full_name"]
        loser_pts, loser_fg = visitor_pts, visitor_fg
    else:
        recommended_bet = f"{visitor['full_name']} gana como visitante"
        winner_name = visitor["full_name"]
        winner_pts, winner_fg = visitor_pts, visitor_fg
        loser_name = home["full_name"]
        loser_pts, loser_fg = home_pts, home_fg

    # Build reasoning
    fatigue_parts = []
    if home_b2b:
        fatigue_parts.append(f"{home['full_name']} en B2B")
    if visitor_b2b:
        fatigue_parts.append(f"{visitor['full_name']} en B2B")
    fatigue_str = f" ⚠️ {', '.join(fatigue_parts)}." if fatigue_parts else ""

    rest_str = f" Descanso: local {home_rest}d / visitante {visitor_rest}d." if (home_rest != 3 or visitor_rest != 3) else ""

    reasoning = (
        f"{winner_name} promedia {winner_pts:.1f} pts ({winner_fg*100:.1f}% FG)"
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
    )


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
