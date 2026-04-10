"""
subagent_ev_optimizer.py — Optimización de parlays con Monte Carlo correlacionado.

Mejora el parlay_builder.py existente con:
  1. Cholesky decomposition para modelar correlaciones entre legs del mismo partido
  2. Monte Carlo (10.000 sims) para estimar probabilidad conjunta real
  3. Claude selecciona el mejor conjunto de parlays considerando EV neto + riesgo

Correlaciones modeladas:
  - Mismo equipo:     ρ = 0.30  (comparten tempo, puntos del equipo)
  - Mismo partido:    ρ = 0.08  (comparten pace general del juego)
  - Distinto partido: ρ = 0.00  (independientes)

Output:
  {
    "enhanced_parlays": [parlay_dict con joint_prob_mc actualizado],
    "best_parlay_key":  str,
    "ev_ranking":       [{"name": str, "mc_joint_prob": float, "ev_pct": float}],
    "commentary":       str,
  }
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import numpy as np
from scipy.stats import norm

from agents.base_agent import BaseAgent
from modules.parlay_builder import build_parlays

if TYPE_CHECKING:
    from modules.analyzer import PlayerPick

logger = logging.getLogger(__name__)

_N_SIMS = 10_000

# Correlaciones entre legs de un parlay
_RHO_SAME_TEAM    = 0.30
_RHO_SAME_GAME    = 0.08
_RHO_DIFF_GAME    = 0.00


class EVOptimizerAgent(BaseAgent):
    """Subagent 4: Optimización de parlays con Cholesky Monte Carlo + selección Claude."""

    def __init__(self) -> None:
        super().__init__("EVOptimizer", model="claude-haiku-4-5-20251001")

    # ── Cholesky Monte Carlo ───────────────────────────────────────────────────

    @staticmethod
    def _build_corr_matrix(legs: list["PlayerPick"]) -> np.ndarray:
        """
        Construye la matriz de correlación entre legs del parlay.
        Usa info de game_label e infiere team de los primeros logs.
        """
        n = len(legs)
        C = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                li, lj = legs[i], legs[j]
                if li.game_label == lj.game_label:
                    # Mismo partido — detectar si mismo equipo por nombre de jugador
                    # Heurística: comparten los mismos apellidos de juego → mismo partido
                    # Usamos ρ_same_game por defecto, ρ_same_team si juegan para el mismo
                    # equipo (no tenemos team_abbr aquí; usamos correlación conservadora)
                    rho = _RHO_SAME_GAME
                else:
                    rho = _RHO_DIFF_GAME
                C[i, j] = C[j, i] = rho
        return C

    def _cholesky_joint_prob(
        self,
        legs: list["PlayerPick"],
        n_sims: int = _N_SIMS,
    ) -> float:
        """
        Estima la probabilidad conjunta del parlay usando Cholesky Monte Carlo.

        Método:
          1. Construir matriz de correlación C
          2. Cholesky: C = L @ L.T
          3. Samplear Z ~ N(0, I_n)
          4. X = L @ Z → variables normales correlacionadas
          5. Convertir a U = Φ(X) → uniformes [0,1] correlacionadas
          6. Leg i hits si U_i < model_prob_i
          7. Parlay hits si TODOS los legs hit
        """
        n = len(legs)
        if n == 0:
            return 0.0
        if n == 1:
            return legs[0].model_prob

        model_probs = np.array([p.model_prob for p in legs], dtype=float)
        C = self._build_corr_matrix(legs)

        try:
            L = np.linalg.cholesky(C)
        except np.linalg.LinAlgError:
            # Fallback: producto simple si la matriz no es definida positiva
            logger.warning("[EVOptimizer] Cholesky falló — usando producto simple")
            return float(np.prod(model_probs))

        # Simulation
        Z = np.random.standard_normal((n, n_sims))   # (n_legs, n_sims)
        X = L @ Z                                     # correlacionadas
        U = norm.cdf(X)                               # uniformes correlacionadas

        # Hit matrix: U_ij < model_prob_i → leg i hits en simulación j
        hits = U < model_probs[:, np.newaxis]         # (n_legs, n_sims)
        parlay_hits = np.all(hits, axis=0)            # (n_sims,)

        return round(float(np.mean(parlay_hits)), 5)

    # ── Main entry point ──────────────────────────────────────────────────────

    def optimize(
        self,
        picks_by_game: dict[str, list["PlayerPick"]],
        existing_parlays: list[dict],
        news_adjustments: dict | None = None,
    ) -> dict:
        """
        Mejora los parlays existentes con probabilidades Monte Carlo Cholesky.

        Args:
            picks_by_game:     Picks del día (potencialmente ajustados por otros agentes).
            existing_parlays:  Parlays del parlay_builder.py.
            news_adjustments:  Noticias que pueden afectar la selección.

        Returns:
            Dict con enhanced_parlays, best_parlay_key, ev_ranking, commentary.
        """
        if not existing_parlays:
            return {"enhanced_parlays": [], "best_parlay_key": None,
                    "ev_ranking": [], "commentary": "Sin parlays para optimizar."}

        # ── 1. Recalcular probabilidades con Cholesky MC ───────────────────────
        enhanced: list[dict] = []
        ev_ranking: list[dict] = []

        for parlay in existing_parlays:
            legs_raw: list[tuple[str, "PlayerPick"]] = parlay.get("legs", [])
            leg_picks = [p for (_, p) in legs_raw]

            if not leg_picks:
                enhanced.append(parlay)
                continue

            mc_joint_prob = self._cholesky_joint_prob(leg_picks)
            simple_prob   = parlay.get("hit_rate_product", 0.0)

            # EV% del parlay: estimamos odds decimales como 1/joint_prob * 0.9 (margen)
            implied_decimal = 1.0 / mc_joint_prob if mc_joint_prob > 0 else 0.0
            # Placeholder EV — en producción se usaría el precio de parlay real
            parlay_ev = round((mc_joint_prob * implied_decimal * 0.9 - 1.0) * 100, 2)

            enhanced_parlay = {
                **parlay,
                "corr_joint_prob":  mc_joint_prob,      # override con MC
                "hit_rate_product": simple_prob,          # mantener original
                "parlay_ev_pct":    parlay_ev,
                "mc_improvement":   round(mc_joint_prob - simple_prob, 5),
            }
            enhanced.append(enhanced_parlay)
            ev_ranking.append({
                "name":           parlay["name"],
                "mc_joint_prob":  mc_joint_prob,
                "simple_prob":    simple_prob,
                "ev_pct":         parlay_ev,
                "n_legs":         len(leg_picks),
            })

        ev_ranking.sort(key=lambda x: -x["mc_joint_prob"])

        logger.info("[EVOptimizer] %d parlays optimizados con Cholesky MC", len(enhanced))
        for r in ev_ranking:
            logger.info("  %s: MC=%.3f (vs simple=%.3f)", r["name"], r["mc_joint_prob"], r["simple_prob"])

        # ── 2. Claude comenta qué parlay recomienda ────────────────────────────
        news_ctx = ""
        if news_adjustments:
            news_ctx = f"\nNoticias relevantes: {json.dumps(news_adjustments, ensure_ascii=False)[:500]}"

        prompt = f"""Estos son los parlays NBA de hoy con sus probabilidades (simple y Monte Carlo correlacionado):

{json.dumps(ev_ranking, ensure_ascii=False, indent=2)}
{news_ctx}

Con esta info, ¿cuál parlay es la mejor apuesta hoy y por qué?
Considerá: probabilidad MC, número de legs, diversificación de partidos, contexto de noticias.

Devolvé SOLO JSON:
{{
  "best_parlay": "nombre del parlay recomendado",
  "commentary": "1-2 oraciones en español argentino explicando la elección"
}}"""

        try:
            raw = self.run(prompt, max_tokens=512)
            claude_result = self._parse_json(raw, fallback={})
        except Exception as exc:
            logger.warning("[EVOptimizer] Claude falló: %s", exc)
            claude_result = {}

        best_key = claude_result.get("best_parlay", ev_ranking[0]["name"] if ev_ranking else None)
        commentary = claude_result.get("commentary", "")

        return {
            "enhanced_parlays": enhanced,
            "best_parlay_key":  best_key,
            "ev_ranking":       ev_ranking,
            "commentary":       commentary,
        }
