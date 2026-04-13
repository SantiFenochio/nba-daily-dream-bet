"""
parlay_builder.py — Simple 4-parlay builder.

4 named parlays with different composition logic:

  1. "La Segura"        (3 legs): top-3 by L15 hit rate, all Alta, diff games
  2. "El Balance"       (3 legs): 2 Alta + 1 Media, diff games
  3. "La Arriesgada"    (4 legs): best 4 picks (Alta + Media), diff games
  4. "Los Consistentes" (3 legs): picks whose min in L10 > line (never failed);
                                  fallback: failed at most once in L10

Joint probability = simple product of L15 hit rates (no Monte Carlo, no Cholesky).
"""

from __future__ import annotations

from modules.analyzer import PlayerPick


def _hit_rate_l15(pick: PlayerPick) -> float:
    if pick.games_l15 <= 0:
        return 0.0
    return pick.hit_count_l15 / pick.games_l15


def _select_legs(
    pool: list[PlayerPick],
    n_legs: int,
    used_players: set[str] | None = None,
) -> list[PlayerPick]:
    """
    Select up to n_legs from pool:
      - At most 1 leg per game_label
      - Skip players in used_players to avoid repeats across parlays when possible
    """
    games_used: set[str] = set()
    players_used: set[str] = set(used_players or [])
    selected: list[PlayerPick] = []

    for pick in pool:
        if len(selected) >= n_legs:
            break
        if pick.game_label in games_used:
            continue
        if pick.player in players_used:
            continue
        selected.append(pick)
        games_used.add(pick.game_label)
        players_used.add(pick.player)

    return selected


def _joint_prob(legs: list[PlayerPick]) -> float:
    """Product of L15 hit rates."""
    prob = 1.0
    for pick in legs:
        prob *= _hit_rate_l15(pick)
    return round(prob, 4)


def _make_parlay(name: str, legs: list[PlayerPick]) -> dict:
    prob = _joint_prob(legs)
    return {
        "name":             name,
        "legs":             [(p.game_label, p) for p in legs],
        "hit_rate_product": prob,
        "corr_joint_prob":  prob,   # same value — no Monte Carlo in simple mode
        "parlay_ev_pct":    None,
    }


def _is_parlay_eligible(pick: PlayerPick) -> bool:
    """
    A pick should not appear in any parlay if:
      - Player is Day-To-Day / Questionable (DNP risk kills the whole ticket)
      - Player is on B2B AND confidence < Alta (double uncertainty)
    """
    if getattr(pick, "is_dtd", False):
        return False
    if pick.is_b2b and pick.confidence != "Alta":
        return False
    return True


def build_parlays(
    picks_by_game: dict[str, list[PlayerPick]],
    n_parlays: int = 4,   # kept for API compat with main.py, ignored internally
    **kwargs,
) -> list[dict]:
    """
    Build 4 named parlays from today's picks.

    DTD / Questionable players are excluded from all parlays (DNP risk).
    B2B players below Alta confidence are also excluded.

    Returns list of dicts compatible with formatter.py:
      {
        "name":             str,
        "legs":             list[tuple[str, PlayerPick]],
        "hit_rate_product": float,
        "corr_joint_prob":  float,
        "parlay_ev_pct":    None,
      }
    """
    all_picks = [p for picks in picks_by_game.values() for p in picks]

    # Filter out risky picks for parlay eligibility
    eligible = [p for p in all_picks if _is_parlay_eligible(p)]
    excluded = len(all_picks) - len(eligible)
    if excluded:
        print(f"[parlay_builder] {excluded} picks excluded from parlays (DTD/B2B-Media risk)")

    alta  = sorted([p for p in eligible if p.confidence == "Alta"],
                   key=lambda p: -_hit_rate_l15(p))
    media = sorted([p for p in eligible if p.confidence == "Media"],
                   key=lambda p: -_hit_rate_l15(p))
    alta_media = sorted(alta + media, key=lambda p: -_hit_rate_l15(p))

    parlays: list[dict] = []

    # ── 1. La Segura: top-3 Alta by hit rate ─────────────────────────────────
    if len(alta) >= 3:
        legs = _select_legs(alta, 3)
        if len(legs) >= 2:
            parlays.append(_make_parlay("La Segura", legs))

    # ── 2. El Balance: 2 Alta + 1 Media ──────────────────────────────────────
    balance_alta   = _select_legs(alta, 2)
    already_used   = {p.player for p in balance_alta}
    balance_media  = _select_legs(media, 1, used_players=already_used)
    balance_legs   = balance_alta + balance_media
    if len(balance_legs) >= 2:
        parlays.append(_make_parlay("El Balance", balance_legs))

    # ── 3. La Arriesgada: top-4 from Alta+Media ───────────────────────────────
    if len(alta_media) >= 3:
        legs = _select_legs(alta_media, 4)
        if len(legs) >= 3:
            parlays.append(_make_parlay("La Arriesgada", legs))

    # ── 4. Los Consistentes: never missed in L10 ─────────────────────────────
    never_missed = sorted(
        [p for p in alta_media if p.min_l10 > p.line],
        key=lambda p: -_hit_rate_l15(p),
    )

    if len(never_missed) >= 3:
        cons_legs = _select_legs(never_missed, 3)
    else:
        # Fallback: missed at most once in L10
        almost = sorted(
            [p for p in alta_media
             if p not in never_missed
             and p.hit_count_l10 >= max(p.games_l10 - 1, 1)],
            key=lambda p: -_hit_rate_l15(p),
        )
        cons_legs = _select_legs(never_missed + almost, 3)

    if len(cons_legs) >= 2:
        parlays.append(_make_parlay("Los Consistentes", cons_legs))

    return parlays
