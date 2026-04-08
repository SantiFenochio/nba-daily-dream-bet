"""
parlay_builder.py — Generador automático de combinadas recomendadas

Criterios de selección (sin cambios):
  - Máximo 1 pick por partido (evitar correlación intra-partido)
  - Prioriza picks con mayor hit rate en los últimos 10 juegos
  - Evita jugadores con lesión confirmada (Out)
  - Varía los picks entre combinadas para diversificar
  - 3 patas por defecto (4 para "La Arriesgada")

v2 — Probabilidad conjunta realista:
  - Monte Carlo + descomposición de Cholesky para modelar correlaciones entre
    tipos de mercado (Puntos↔PRA ~0.62, Steals↔Blocks ~0.38, etc.).
  - Legs del mismo partido reciben correlación adicional por contexto compartido.
  - EV% de la combinada calculado usando los precios americanos reales de cada leg.
  - Activable/desactivable con USE_CORRELATION (True por defecto).
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np
from scipy import stats as scipy_stats

from modules.analyzer import PlayerPick

# ── Configuración principal ────────────────────────────────────────────────────
USE_CORRELATION       = True    # False → multiplicación simple (backward compat)
N_SIMULATIONS         = 1_000  # iteraciones Monte Carlo — rápido y suficientemente preciso
CROSS_GAME_CORR_SCALE = 0.35   # factor de escala para legs de distintos partidos.
                                # La correlación real en juegos separados refleja
                                # solo el error de modelo compartido, no covarianza
                                # física. corr_efectiva = corr_within × 0.35
SAME_GAME_CORR_BONUS  = 0.10   # bonus adicional si dos legs son del mismo partido
                                # (ritmo, pace y contexto compartido)

CONF_WEIGHT = {"Alta": 1.0, "Media": 0.80, "Baja": 0.55, "Riesgosa": 0.35}

PARLAY_NAMES = [
    "La Segura",
    "El Balance",
    "Los UNDER",
    "Protagonistas",
    "La Arriesgada",
]

# ── Matriz de correlación entre mercados NBA (datos 2024-2026) ─────────────────
#
# Representa la correlación intra-jugador (mismo partido) entre el rendimiento
# en dos tipos de mercado. Fuente: análisis histórico de game logs NBA.
#
# Cómo afecta a la prob. conjunta de un parlay:
#   • Over + Over con corr. positiva  → prob. conjunta SUBE vs. producto simple
#     (cuando el contexto favorece un mercado correlacionado, favorece ambos)
#   • Over + Under con corr. positiva → prob. conjunta BAJA vs. producto simple
#   El framework Monte Carlo lo gestiona automáticamente vía thresholds.
#
# Para legs de distintos partidos se escala con CROSS_GAME_CORR_SCALE.
MARKET_CORRELATIONS: dict[tuple[str, str], float] = {
    # Puntos ↔ PRA (alta: puntos es la componente dominante del PRA)
    ("player_points",                  "player_points_rebounds_assists"): 0.62,
    ("player_points_rebounds_assists", "player_points"):                   0.62,
    # Puntos ↔ Rebotes (pívots y ala-pívots acumulan ambos)
    ("player_points",                  "player_rebounds"):                 0.45,
    ("player_rebounds",                "player_points"):                   0.45,
    # PRA ↔ Asistencias (asistencias es componente directa del PRA)
    ("player_points_rebounds_assists", "player_assists"):                  0.58,
    ("player_assists",                 "player_points_rebounds_assists"):  0.58,
    # Robos ↔ Tapas (ambos requieren actividad defensiva intensa)
    ("player_steals",                  "player_blocks"):                   0.38,
    ("player_blocks",                  "player_steals"):                   0.38,
    # Triples ↔ Puntos (los triples son componente directa de los puntos)
    ("player_threes",                  "player_points"):                   0.55,
    ("player_points",                  "player_threes"):                   0.55,
}
# Correlación por defecto para pares no especificados (misma categoría amplia
# o error de modelo compartido entre mercados genéricos)
DEFAULT_MARKET_CORR = 0.25


# ══════════════════════════════════════════════════════════════════════════════
# Helpers de odds
# ══════════════════════════════════════════════════════════════════════════════

def _american_to_decimal(american: int) -> float:
    """Convierte cuotas americanas a formato decimal (base 1)."""
    if american >= 100:
        return american / 100.0 + 1.0
    return 100.0 / abs(american) + 1.0


def _calc_parlay_ev_pct(joint_prob: float, legs: list[tuple[str, PlayerPick]]) -> float:
    """
    Calcula el EV% esperado de la combinada completa.

    Fórmula: EV% = (prob_conjunta × payout_decimal_total − 1) × 100

    El payout decimal se estima multiplicando las cuotas decimales de cada leg
    (equivalente a una combinada sin juice adicional sobre la parlay).
    Si una leg no tiene precio registrado, asume −110 (cuota justa estándar).

    Ejemplo: 3 legs a −110 → payout = 1.909³ ≈ 6.96x. Con joint_prob = 0.12:
             EV = (0.12 × 6.96 − 1) × 100 = −16.5%  ← negativo por el juice.
             Con picks EV+ que elevan joint_prob real, el número mejora.
    """
    parlay_decimal = 1.0
    for _, pick in legs:
        price = pick.price if pick.price else -110
        parlay_decimal *= _american_to_decimal(price)

    ev_pct = (joint_prob * parlay_decimal - 1.0) * 100.0
    return round(ev_pct, 1)


# ══════════════════════════════════════════════════════════════════════════════
# Construcción de la matriz de correlación
# ══════════════════════════════════════════════════════════════════════════════

def _get_market_corr(market_a: str, market_b: str) -> float:
    """
    Devuelve la correlación base (intra-jugador, mismo partido) entre dos mercados.
    Si son el mismo mercado (e.g., dos Puntos) se usa 0.30 para reflejar el
    error de modelo compartido entre players similares.
    """
    if market_a == market_b:
        return 0.30  # mismo tipo de stat → correlación de modelo moderada
    return MARKET_CORRELATIONS.get((market_a, market_b), DEFAULT_MARKET_CORR)


def _build_correlation_matrix(legs: list[tuple[str, PlayerPick]]) -> np.ndarray:
    """
    Construye la matriz de correlación n×n entre los legs de la combinada.

    Lógica de escala:
      • Mismo partido  → corr_base + SAME_GAME_CORR_BONUS  (contexto compartido)
      • Distintos partidos → corr_base × CROSS_GAME_CORR_SCALE
        (solo persiste el error de modelo compartido entre mercados similares)
      • Diagonal → 1.0 (autocorrelación perfecta)

    La matriz resultante es simétrica. Si no es definida positiva (ruido numérico),
    se regulariza antes de pasarla a Cholesky.
    """
    n   = len(legs)
    C   = np.eye(n, dtype=float)

    for i in range(n):
        for j in range(i + 1, n):
            game_i, pick_i = legs[i]
            game_j, pick_j = legs[j]

            corr_base = _get_market_corr(pick_i.market_key, pick_j.market_key)

            if game_i == game_j:
                # Mismo partido: correlación alta (pace, ritmo y contexto compartidos)
                corr = min(0.90, corr_base + SAME_GAME_CORR_BONUS)
            else:
                # Distintos partidos: escalar a error de modelo compartido
                corr = corr_base * CROSS_GAME_CORR_SCALE

            C[i, j] = corr
            C[j, i] = corr

    return C


# ══════════════════════════════════════════════════════════════════════════════
# Monte Carlo con descomposición de Cholesky
# ══════════════════════════════════════════════════════════════════════════════

def _cholesky_joint_prob(
    legs:        list[tuple[str, PlayerPick]],
    probs:       list[float],
    corr_matrix: np.ndarray,
    n_sim:       int = N_SIMULATIONS,
    seed:        int = 42,
) -> float:
    """
    Calcula la probabilidad conjunta de que todos los legs sean acertados,
    modelando explícitamente las correlaciones entre mercados.

    Algoritmo:
      1. Convierte cada prob. individual en un umbral en espacio normal estándar:
           - Over: hit si Z_i > ppf(1 − p_i)   [performance alta]
           - Under: hit si Z_i < ppf(p_i)       [performance baja]
      2. Descompone C = L L^T (Cholesky) para generar normales correlacionadas.
      3. Genera n_sim muestras Z_corr = Z_indep @ L^T con Z_indep ~ N(0, I).
      4. P(todos los legs hit) = fracción de simulaciones donde TODAS las
         condiciones se cumplen simultáneamente.

    Propiedades del resultado:
      • Over+Over con corr. positiva  → prob. conjunta SUBE vs. producto simple
      • Over+Under con corr. positiva → prob. conjunta BAJA vs. producto simple
      • Reproducible gracias al seed fijo (determinismo entre ejecuciones)

    Si la descomposición de Cholesky falla (C no es def. positiva tras
    regularización), cae en multiplicación simple como fallback seguro.
    """
    n = len(legs)

    # ── Regularizar C para garantizar definida positiva ──────────────────────
    min_eig = float(np.linalg.eigvalsh(corr_matrix).min())
    if min_eig < 1e-8:
        corr_matrix = corr_matrix + (abs(min_eig) + 1e-6) * np.eye(n)

    # ── Descomposición de Cholesky ───────────────────────────────────────────
    try:
        L = np.linalg.cholesky(corr_matrix)
    except np.linalg.LinAlgError:
        # Fallback a independencia si Cholesky falla
        return float(np.prod(probs))

    # ── Generar muestras correlacionadas ─────────────────────────────────────
    rng    = np.random.default_rng(seed)
    Z_indep = rng.standard_normal((n_sim, n))
    Z_corr  = Z_indep @ L.T                  # shape: (n_sim, n)

    # ── Evaluar condición de hit para cada leg ───────────────────────────────
    hits = np.ones(n_sim, dtype=bool)
    for i, ((_, pick), p) in enumerate(zip(legs, probs)):
        p_clip = max(0.001, min(0.999, p))   # evitar ±∞ en ppf
        if pick.side.lower() == "over":
            # Éxito si la performance es alta → Z_i > umbral
            threshold = scipy_stats.norm.ppf(1.0 - p_clip)
            hits &= Z_corr[:, i] > threshold
        else:
            # Éxito si la performance es baja → Z_i < umbral
            threshold = scipy_stats.norm.ppf(p_clip)
            hits &= Z_corr[:, i] < threshold

    return float(hits.mean())


# ══════════════════════════════════════════════════════════════════════════════
# Probabilidad conjunta — wrapper principal
# ══════════════════════════════════════════════════════════════════════════════

def _joint_prob(legs: list[tuple[str, PlayerPick]]) -> tuple[float, float]:
    """
    Calcula (prob_naïve, prob_realista) para los legs de una combinada.

    prob_naïve    = producto de hit rates históricos L10 (método original)
    prob_realista = Monte Carlo + Cholesky si USE_CORRELATION else prob_naïve

    Para el Monte Carlo se usa model_prob del analyzer (blend Poisson + Bayes)
    en lugar del hit rate L10, porque:
      - model_prob es más estable con muestras pequeñas (nunca es 0 por definición)
      - Incorpora contexto (pace, DVP, blowout risk, etc.) que el L10 ignora
      - Oscila menos ante rachas cortas de hits/misses

    Retorna ambas para que el formatter pueda mostrar la diferencia si se desea.
    """
    # Prob. naïve: multiplicación simple de hit rates históricos
    naive = 1.0
    for _, pick in legs:
        naive *= _hit_rate(pick)

    if not USE_CORRELATION or len(legs) < 2:
        return naive, naive

    # Prob. realista vía Monte Carlo correlacionado
    probs       = [pick.model_prob for _, pick in legs]
    corr_matrix = _build_correlation_matrix(legs)
    realistic   = _cholesky_joint_prob(legs, probs, corr_matrix)

    return naive, realistic


# ══════════════════════════════════════════════════════════════════════════════
# Score de selección de picks (sin cambios)
# ══════════════════════════════════════════════════════════════════════════════

def _hit_rate(pick: PlayerPick) -> float:
    if pick.games_l10 <= 0:
        return 0.0
    return pick.hit_count_l10 / pick.games_l10


def _pick_score(pick: PlayerPick) -> float:
    """Score compuesto para selección de legs: hit rate × confianza + bonus EV."""
    hit      = _hit_rate(pick)
    conf     = CONF_WEIGHT.get(pick.confidence, 0.6)
    ev_bonus = min(pick.ev_pct, 20) / 200   # cap en 10% de bonus
    return hit * conf + ev_bonus


def _is_out(pick: PlayerPick) -> bool:
    return bool(pick.injury_status and "out" in pick.injury_status.lower())


# ══════════════════════════════════════════════════════════════════════════════
# Constructor principal
# ══════════════════════════════════════════════════════════════════════════════

def build_parlays(
    picks_by_game: dict[str, list[PlayerPick]],
    n_parlays:    int = 5,
    default_legs: int = 3,
) -> list[dict]:
    """
    Genera combinadas recomendadas con probabilidad conjunta realista y EV estimado.

    Retorna lista de dicts:
        {
          "name":             str,
          "legs":             list[tuple[str, PlayerPick]],
          "hit_rate_product": float,   # prob. naïve (producto de hit rates L10)
          "corr_joint_prob":  float,   # prob. realista con correlaciones (Monte Carlo)
          "parlay_ev_pct":    float,   # EV% estimado de la combinada completa
        }

    La lógica de selección de legs es idéntica a v1:
      - 1 leg por partido (diversificación)
      - 1 leg por jugador (evitar duplicados)
      - Skippea jugadores confirmados OUT
      - Rota los picks más usados para variar entre combinadas
    """
    # ── Aplanar picks elegibles con su score ─────────────────────────────────
    candidates: list[tuple[float, str, PlayerPick]] = []
    for game, game_picks in picks_by_game.items():
        for pick in game_picks:
            if _is_out(pick):
                continue
            candidates.append((_pick_score(pick), game, pick))

    candidates.sort(key=lambda x: -x[0])

    usage:   dict[int, int] = defaultdict(int)
    parlays: list[dict]     = []

    for p_idx in range(n_parlays):
        # "La Arriesgada" (último parlay) tiene una pata extra
        legs_target = default_legs + (1 if p_idx == n_parlays - 1 else 0)

        # Re-ordenar: menos usados primero, luego por score descendente
        sorted_cands = sorted(candidates, key=lambda x: (usage[id(x[2])], -x[0]))

        parlay_legs:  list[tuple[str, PlayerPick]] = []
        games_used:   set[str] = set()
        players_used: set[str] = set()

        for score, game, pick in sorted_cands:
            if len(parlay_legs) >= legs_target:
                break
            if game   in games_used:
                continue
            if pick.player in players_used:
                continue
            parlay_legs.append((game, pick))
            games_used.add(game)
            players_used.add(pick.player)
            usage[id(pick)] += 1

        if len(parlay_legs) < 2:
            continue

        # ── Calcular probabilidades ──────────────────────────────────────────
        naive_prob, corr_prob = _joint_prob(parlay_legs)
        ev_pct = _calc_parlay_ev_pct(corr_prob, parlay_legs)

        name = PARLAY_NAMES[p_idx] if p_idx < len(PARLAY_NAMES) else f"Combinada {p_idx + 1}"
        parlays.append({
            "name":             name,
            "legs":             parlay_legs,
            "hit_rate_product": naive_prob,   # backward compat con formatter v1
            "corr_joint_prob":  corr_prob,    # prob. realista con correlaciones
            "parlay_ev_pct":    ev_pct,       # EV de la combinada completa
        })

    return parlays
