"""
consistency_picks.py — "Simple is smart" pick system.

Instead of EV/Poisson models, this module answers one question:
    "In how many of the last N games did this player clear this line?"

If a player has cleared a line in 13 of 15 games, that's the bet.
No Poisson, no Kelly, no bars. Just hit rate + average.

Logic:
 1. For every Over prop available today, check last N game logs.
 2. Count how many times the player exceeded the line.
 3. If hit_rate >= MIN_HIT_RATE, surface the pick.
 4. For each player/market combo, keep only the highest consistent line
    (most informative pick, avoids trivial lines).
 5. Sort by hit rate desc; return top MAX_PICKS.
"""

from modules.fetch_player_stats import get_stat_value
from modules.fetch_props import MARKET_LABELS

# ── Config ────────────────────────────────────────────────────────────────────
N_GAMES       = 15    # Lookback window
MIN_GAMES     = 8     # Minimum valid games to qualify
MIN_HIT_RATE  = 0.80  # 80%+ = worth surfacing (12/15, 10/12, etc.)
MAX_PICKS     = 8     # Cap on returned picks

# Only surface markets that are readable and predictable
ELIGIBLE_MARKETS = {
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_threes",
    "player_points_rebounds_assists",
    "player_steals",
    "player_blocks",
}


def generate_consistency_picks(
    player_logs: dict[str, list[dict]],
    prop_records: list[dict],
    n_games: int = N_GAMES,
    min_hit_rate: float = MIN_HIT_RATE,
    max_picks: int = MAX_PICKS,
) -> list[dict]:
    """
    Return the best consistency picks for today sorted by hit rate.

    Each returned dict:
        player, market, market_key, line, price,
        hits, games, hit_rate, avg, game_label
    """
    # Collect all distinct Over lines per (player, market_key, line)
    seen: set[tuple] = set()
    prop_index: list[dict] = []
    for r in prop_records:
        if r["side"] != "over":
            continue
        if r["market_key"] not in ELIGIBLE_MARKETS:
            continue
        key = (r["player"], r["market_key"], r["line"])
        if key not in seen:
            seen.add(key)
            prop_index.append(r)

    results: list[dict] = []

    for prop in prop_index:
        player     = prop["player"]
        market_key = prop["market_key"]
        line       = prop["line"]

        logs = player_logs.get(player, [])
        if not logs:
            continue

        recent = logs[:n_games]
        hits, total_stat, valid = 0, 0.0, 0

        for game in recent:
            val = get_stat_value(game, market_key)
            if val is None:
                continue
            valid      += 1
            total_stat += val
            if val > line:
                hits += 1

        if valid < MIN_GAMES:
            continue

        hit_rate = hits / valid
        if hit_rate < min_hit_rate:
            continue

        results.append({
            "player":     player,
            "market":     MARKET_LABELS.get(market_key, market_key),
            "market_key": market_key,
            "line":       line,
            "price":      prop.get("price", 0),
            "hits":       hits,
            "games":      valid,
            "hit_rate":   hit_rate,
            "avg":        total_stat / valid,
            "game_label": prop.get("game_label", ""),
        })

    # Per (player, market_key): keep the pick with the best hit_rate,
    # breaking ties by highest line (harder line = more impressive).
    best: dict[tuple, dict] = {}
    for r in results:
        key = (r["player"], r["market_key"])
        prev = best.get(key)
        if prev is None:
            best[key] = r
        elif r["hit_rate"] > prev["hit_rate"]:
            best[key] = r
        elif r["hit_rate"] == prev["hit_rate"] and r["line"] > prev["line"]:
            best[key] = r

    final = sorted(
        best.values(),
        key=lambda x: (x["hit_rate"], x["games"]),
        reverse=True,
    )
    return final[:max_picks]
