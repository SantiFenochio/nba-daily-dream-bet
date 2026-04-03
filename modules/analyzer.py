from dataclasses import dataclass, field

HIT_RATE_THRESHOLD = 0.60
MAX_PROPS = 5
FALLBACK_PROPS = 3


@dataclass
class Pick:
    game_label: str
    home_team: str
    visitor_team: str
    recommended_bet: str
    reasoning: str
    confidence: str
    props: list[dict] = field(default_factory=list)
    low_confidence_props: bool = False


def analyze_games(games: list[dict], stats: dict, props: dict) -> list[Pick]:
    picks = []
    for game in games:
        pick = _analyze_game(game, stats, props.get(game["id"], []))
        picks.append(pick)
    return picks


def _analyze_game(game: dict, stats: dict, game_props: list[dict]) -> Pick:
    home = game["home_team"]
    visitor = game["visitor_team"]
    home_stats = stats.get(home["id"], {})
    visitor_stats = stats.get(visitor["id"], {})

    home_pts = home_stats.get("pts", 0) or 0
    visitor_pts = visitor_stats.get("pts", 0) or 0
    home_fg = home_stats.get("fg_pct", 0) or 0
    visitor_fg = visitor_stats.get("fg_pct", 0) or 0

    home_score = home_pts * 0.6 + home_fg * 100 * 0.4
    visitor_score = visitor_pts * 0.6 + visitor_fg * 100 * 0.4

    margin = abs(home_score - visitor_score)
    if margin < 2:
        confidence = "Baja"
    elif margin < 5:
        confidence = "Media"
    else:
        confidence = "Alta"

    if home_score >= visitor_score:
        recommended_bet = f"{home['full_name']} gana como local"
        reasoning = (
            f"{home['full_name']} promedia {home_pts:.1f} pts "
            f"({home_fg*100:.1f}% FG) vs {visitor_pts:.1f} pts "
            f"({visitor_fg*100:.1f}% FG) de {visitor['full_name']}."
        )
    else:
        recommended_bet = f"{visitor['full_name']} gana como visitante"
        reasoning = (
            f"{visitor['full_name']} promedia {visitor_pts:.1f} pts "
            f"({visitor_fg*100:.1f}% FG) vs {home_pts:.1f} pts "
            f"({home_fg*100:.1f}% FG) de {home['full_name']}."
        )

    top_props, low_confidence_props = _extract_top_props(game_props)
    label = f"{visitor['full_name']} @ {home['full_name']}"
    print(
        f"[analyzer]   {label}: {len(top_props)} props selected "
        f"(low_confidence={low_confidence_props})"
    )

    return Pick(
        game_label=label,
        home_team=home["full_name"],
        visitor_team=visitor["full_name"],
        recommended_bet=recommended_bet,
        reasoning=reasoning,
        confidence=confidence,
        props=top_props,
        low_confidence_props=low_confidence_props,
    )


def _extract_top_props(bookmakers: list[dict]) -> tuple[list[dict], bool]:
    seen: dict[str, dict] = {}
    for bookmaker in bookmakers:
        for market in bookmaker.get("markets", []):
            key = market.get("key", "")
            for outcome in market.get("outcomes", []):
                player = outcome.get("description", "")
                point = outcome.get("point", "")
                name = outcome.get("name", "")
                price = outcome.get("price", 0)
                prop_key = f"{player}_{key}_{name}"
                if prop_key not in seen:
                    seen[prop_key] = {
                        "player": player,
                        "market": _market_label(key),
                        "line": point,
                        "side": name,
                        "price": price,
                        "hit_rate": _implied_prob(price),
                    }

    all_props = sorted(seen.values(), key=lambda x: x["hit_rate"], reverse=True)
    print(
        f"[analyzer]   Props pool: {len(all_props)} unique outcomes. "
        f"Top hit_rates: {[round(p['hit_rate'], 2) for p in all_props[:5]]}"
    )

    high_conf = [p for p in all_props if p["hit_rate"] >= HIT_RATE_THRESHOLD]
    if high_conf:
        return high_conf[:MAX_PROPS], False

    # Fallback: return top 3 even below threshold
    return all_props[:FALLBACK_PROPS], True


def _implied_prob(price) -> float:
    if not isinstance(price, (int, float)) or price == 0:
        return 0.5
    if price < 0:
        return abs(price) / (abs(price) + 100)
    return 100 / (price + 100)


def _market_label(key: str) -> str:
    labels = {
        "player_points": "Puntos",
        "player_rebounds": "Rebotes",
        "player_assists": "Asistencias",
    }
    return labels.get(key, key)
