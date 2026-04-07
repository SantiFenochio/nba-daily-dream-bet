"""
parlay_builder.py — Generador automático de combinadas recomendadas

Criterios de selección:
  - Máximo 1 pick por partido (evitar correlación intra-partido)
  - Prioriza picks con mayor hit rate en los últimos 10 juegos
  - Evita jugadores con lesión confirmada (Out)
  - Varía los picks entre combinadas para diversificar
  - 3 patas por defecto (4 para "la arriesgada")
"""

from __future__ import annotations
from collections import defaultdict
from modules.analyzer import PlayerPick

CONF_WEIGHT = {"Alta": 1.0, "Media": 0.80, "Baja": 0.55, "Riesgosa": 0.35}

PARLAY_NAMES = [
    "La Segura",
    "El Balance",
    "Los UNDER",
    "Protagonistas",
    "La Arriesgada",
]


def _hit_rate(pick: PlayerPick) -> float:
    if pick.games_l10 <= 0:
        return 0.0
    return pick.hit_count_l10 / pick.games_l10


def _pick_score(pick: PlayerPick) -> float:
    """Composite score for parlay selection — hit rate is king, EV secondary."""
    hit = _hit_rate(pick)
    conf = CONF_WEIGHT.get(pick.confidence, 0.6)
    ev_bonus = min(pick.ev_pct, 20) / 200  # cap at 10% bonus
    return hit * conf + ev_bonus


def _is_out(pick: PlayerPick) -> bool:
    return bool(pick.injury_status and "out" in pick.injury_status.lower())


def build_parlays(
    picks_by_game: dict[str, list[PlayerPick]],
    n_parlays: int = 5,
    default_legs: int = 3,
) -> list[dict]:
    """
    Returns a list of parlay dicts:
        {
            "name": str,
            "legs": list[tuple[str, PlayerPick]],   # (game_label, pick)
            "hit_rate_product": float,               # joint probability estimate
        }
    """
    # Flatten all eligible picks with their scores
    candidates: list[tuple[float, str, PlayerPick]] = []
    for game, game_picks in picks_by_game.items():
        for pick in game_picks:
            if _is_out(pick):
                continue
            candidates.append((_pick_score(pick), game, pick))

    # Sort best-first
    candidates.sort(key=lambda x: -x[0])

    # Track usage count per pick object (by id)
    usage: dict[int, int] = defaultdict(int)

    parlays = []
    for p_idx in range(n_parlays):
        legs_target = default_legs + (1 if p_idx == n_parlays - 1 else 0)  # last one gets +1 leg

        # Re-sort candidates: least-used first, then by score
        sorted_cands = sorted(candidates, key=lambda x: (usage[id(x[2])], -x[0]))

        parlay_legs: list[tuple[str, PlayerPick]] = []
        games_used: set[str] = set()
        players_used: set[str] = set()

        for score, game, pick in sorted_cands:
            if len(parlay_legs) >= legs_target:
                break
            if game in games_used:
                continue
            if pick.player in players_used:
                continue
            parlay_legs.append((game, pick))
            games_used.add(game)
            players_used.add(pick.player)
            usage[id(pick)] += 1

        if len(parlay_legs) < 2:
            continue

        # Estimate joint hit rate (independent assumption)
        joint_prob = 1.0
        for _, pick in parlay_legs:
            joint_prob *= _hit_rate(pick)

        name = PARLAY_NAMES[p_idx] if p_idx < len(PARLAY_NAMES) else f"Combinada {p_idx + 1}"
        parlays.append({
            "name": name,
            "legs": parlay_legs,
            "hit_rate_product": joint_prob,
        })

    return parlays
