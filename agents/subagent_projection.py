"""
subagent_projection.py — Proyecciones mejoradas con Monte Carlo + ajuste cualitativo.

Combina:
  1. Lógica cuantitativa existente (hit-rate, EV real del analyzer.py)
  2. Monte Carlo Bootstrap (1000 runs) sobre distribución empírica de cada jugador
  3. Ajuste cualitativo de Claude: lesiones ramp-up, blowout risk, variabilidad minutos

Output:
  {
    "mc_probs":    {"player|market_key": float},     # probabilidad Monte Carlo
    "adjustments": [{"key": str, "factor": float, "reason": str}],
    "insights":    [str],                            # observaciones clave
    "flagged":     ["player|market_key"],             # picks con divergencia alta
  }
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import numpy as np

from agents.base_agent import BaseAgent
from modules.fetch_player_stats import get_stat_value

if TYPE_CHECKING:
    from modules.analyzer import PlayerPick

logger = logging.getLogger(__name__)

_N_SIMULATIONS = 1000
_DIVERGENCE_THRESHOLD = 0.12   # Si MC difiere >12% de model_prob → flag


class ProjectionAgent(BaseAgent):
    """Subagent 2: Monte Carlo + ajuste cualitativo de proyecciones."""

    def __init__(self) -> None:
        super().__init__("ProjectionAgent", model="claude-haiku-4-5-20251001")

    # ── Monte Carlo ───────────────────────────────────────────────────────────

    @staticmethod
    def _mc_bootstrap_prob(values: list[float], line: float, n: int = _N_SIMULATIONS) -> float:
        """
        Estima P(stat > line) via bootstrap Monte Carlo.
        Resamplea de la distribución empírica histórica (sin asumir normalidad).
        """
        if len(values) < 5:
            return 0.0
        arr = np.array(values, dtype=float)
        # Bootstrap: resampleo con reemplazo
        simulated = np.random.choice(arr, size=n, replace=True)
        return float(np.mean(simulated > line))

    @staticmethod
    def _mc_normal_prob(values: list[float], line: float, n: int = _N_SIMULATIONS) -> float:
        """
        Estima P(stat > line) asumiendo distribución normal (media, std empíricos).
        Complementa al bootstrap para distribuciones con pocos datos.
        """
        if len(values) < 5:
            return 0.0
        mu = float(np.mean(values))
        sigma = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
        if sigma <= 0:
            return 1.0 if mu > line else 0.0
        # P(X > line) = 1 - Φ((line - mu) / sigma)
        from scipy.stats import norm
        return float(1.0 - norm.cdf(line, loc=mu, scale=sigma))

    def _compute_mc_prob(self, values: list[float], line: float) -> float:
        """Promedia bootstrap y normal para mayor robustez."""
        p_boot = self._mc_bootstrap_prob(values, line)
        p_norm = self._mc_normal_prob(values, line)
        # Media pesada: bootstrap 60%, normal 40%
        return round(p_boot * 0.60 + p_norm * 0.40, 4)

    # ── Main entry point ──────────────────────────────────────────────────────

    def enhance(
        self,
        picks_by_game: dict[str, list["PlayerPick"]],
        player_logs: dict[str, list[dict]],
        injury_statuses: dict[str, str | None] | None = None,
    ) -> dict:
        """
        Ejecuta Monte Carlo en todos los picks y pide a Claude ajustes cualitativos.

        Args:
            picks_by_game:   Picks del analyzer.
            player_logs:     Histórico de juegos por jugador.
            injury_statuses: Estados de lesión para contexto cualitativo.

        Returns:
            Dict con mc_probs, adjustments, insights, flagged.
        """
        all_picks: list["PlayerPick"] = [
            p for picks in picks_by_game.values() for p in picks
        ]

        if not all_picks:
            return {"mc_probs": {}, "adjustments": [], "insights": [], "flagged": []}

        # ── 1. Correr Monte Carlo para todos los picks ─────────────────────────
        mc_probs: dict[str, float] = {}
        mc_vs_model: list[dict] = []

        for pick in all_picks:
            key = f"{pick.player}|{pick.market_key}"
            logs = player_logs.get(pick.player, [])
            if not logs:
                continue

            values: list[float] = []
            for g in logs[:20]:
                val = get_stat_value(g, pick.market_key)
                if val is not None:
                    values.append(val)

            if len(values) < 5:
                continue

            mc_p = self._compute_mc_prob(values, pick.line)
            mc_probs[key] = mc_p

            divergence = abs(mc_p - pick.model_prob)
            mc_vs_model.append({
                "key": key,
                "player": pick.player,
                "market": pick.market,
                "line": pick.line,
                "model_prob": pick.model_prob,
                "mc_prob": mc_p,
                "divergence": round(divergence, 4),
                "ev_pct": pick.ev_pct,
                "confidence": pick.confidence,
                "is_b2b": pick.is_b2b,
                "injury": (injury_statuses or {}).get(pick.player),
                "streak": pick.consecutive_streak,
                # Contexto de minutos para evaluación de variabilidad
                "n_values": len(values),
                "avg_val": round(sum(values[:15]) / min(len(values), 15), 2),
            })

        flagged = [d["key"] for d in mc_vs_model if d["divergence"] > _DIVERGENCE_THRESHOLD]
        logger.info("[ProjectionAgent] MC corrido en %d picks | %d con divergencia >%.0f%%",
                    len(mc_probs), len(flagged), _DIVERGENCE_THRESHOLD * 100)

        # ── 2. Claude interpreta divergencias + factores cualitativos ─────────
        # Solo los más relevantes para ahorrar tokens
        relevant = sorted(mc_vs_model, key=lambda d: -d["divergence"])[:15]

        prompt = f"""Analizá las diferencias entre probabilidad del modelo (hit-rate histórico) \
y simulación Monte Carlo para estos picks NBA.

PICKS CON DIVERGENCIA (ordenados por mayor diferencia):
{json.dumps(relevant, ensure_ascii=False, indent=2)}

Para cada pick, considerá:
- Si mc_prob > model_prob: hay valor esperado mejor que el modelo base (upside)
- Si mc_prob < model_prob: la distribución tiene colas pesadas o outliers negativos (risk)
- Factores que SIEMPRE pesan: B2B (fatiga), injury status (carga reducida), racha activa
- Jugadores con "ramp-up" post-lesión (bajos en model_prob pero mc_prob puede subestimarlos)

Devolvé SOLO JSON válido (sin markdown):
{{
  "adjustments": [
    {{
      "key": "player|market_key",
      "factor": 1.05,
      "reason": "MC sugiere upside real por distribución positiva en L20"
    }}
  ],
  "insights": [
    "observación clave 1",
    "observación clave 2"
  ],
  "flagged_for_review": ["player|market_key"]
}}

REGLAS:
- factor entre 0.80 y 1.20 (nunca fuera de ese rango)
- Solo ajustá los que tengan divergencia > 0.08 o contexto cualitativo evidente
- Máximo 8 adjustments
- insights: máximo 4 observaciones, en español argentino"""

        try:
            raw = self.run(prompt, max_tokens=1024)
            claude_result = self._parse_json(raw, fallback={})
        except Exception as exc:
            logger.warning("[ProjectionAgent] Claude falló: %s", exc)
            claude_result = {}

        return {
            "mc_probs": mc_probs,
            "adjustments": claude_result.get("adjustments", []),
            "insights": claude_result.get("insights", []),
            "flagged": flagged + claude_result.get("flagged_for_review", []),
        }
