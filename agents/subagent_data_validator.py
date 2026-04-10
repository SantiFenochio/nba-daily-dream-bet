"""
subagent_data_validator.py — Valida consistencia de picks antes del análisis.

Responsabilidades:
  - Verificar que model_prob sea consistente con hit_rate_l15/l5
  - Detectar jugadores con status de lesión inconsistente con sus stats
  - Alertar sobre mismatches > 5% entre proyección y línea
  - Identificar datos potencialmente desactualizados

Output:
  {
    "alerts": [{"player": str, "market": str, "issue": str, "severity": "warn"|"error"}],
    "excluded_keys": ["player|market_key"],   # picks que deben ser excluidos
    "mismatch_count": int,
    "data_quality_score": float,              # 0.0–1.0
  }
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from agents.base_agent import BaseAgent

if TYPE_CHECKING:
    from modules.analyzer import PlayerPick

logger = logging.getLogger(__name__)

_PROB_MISMATCH_THRESHOLD = 0.05   # 5% diferencia entre model_prob y calc


class DataValidatorAgent(BaseAgent):
    """Subagent 1: Validación de consistencia de datos y picks."""

    def __init__(self) -> None:
        super().__init__("DataValidator", model="claude-haiku-4-5-20251001")

    def validate(
        self,
        picks_by_game: dict[str, list["PlayerPick"]],
        projections: dict[str, dict] | None = None,
        injury_statuses: dict[str, str | None] | None = None,
    ) -> dict:
        """
        Valida la consistencia de todos los picks.

        Args:
            picks_by_game:    Output del analyzer.
            projections:      SportsData projections (opcional).
            injury_statuses:  ESPN injury report (opcional).

        Returns:
            Dict con alertas, picks excluidos y score de calidad.
        """
        all_picks: list["PlayerPick"] = [
            p for picks in picks_by_game.values() for p in picks
        ]

        if not all_picks:
            return {"alerts": [], "excluded_keys": [], "mismatch_count": 0, "data_quality_score": 1.0}

        # ── Pre-validation: Python-side mismatch checks ───────────────────────
        python_alerts: list[dict] = []

        for pick in all_picks:
            # 1. Verificar que model_prob sea consistente con hit rates
            expected_prob = round(
                (pick.hit_count_l15 / pick.games_l15) * 0.65
                + (pick.hit_count_l5 / pick.games_l5) * 0.35,
                4,
            ) if pick.games_l15 > 0 and pick.games_l5 > 0 else None

            if expected_prob is not None:
                diff = abs(pick.model_prob - expected_prob)
                if diff > _PROB_MISMATCH_THRESHOLD:
                    python_alerts.append({
                        "player": pick.player,
                        "market": pick.market,
                        "issue": f"model_prob={pick.model_prob:.3f} difiere de calc={expected_prob:.3f} (Δ={diff:.3f})",
                        "severity": "warn",
                    })

            # 2. Jugadores con injury status pero confianza Alta
            inj = (injury_statuses or {}).get(pick.player, "")
            if inj and "questionable" in inj.lower() and pick.confidence == "Alta":
                python_alerts.append({
                    "player": pick.player,
                    "market": pick.market,
                    "issue": f"Estado: {inj} pero confianza=Alta — posible sobreestimación",
                    "severity": "warn",
                })

            # 3. SportsData projection muy alejada de la línea
            if projections and pick.player in projections:
                proj = projections[pick.player]
                stat_map = {
                    "player_points": "pts", "player_rebounds": "reb",
                    "player_assists": "ast", "player_steals": "stl",
                    "player_blocks": "blk", "player_threes": "threes",
                    "player_points_rebounds_assists": "pra",
                }
                proj_key = stat_map.get(pick.market_key)
                if proj_key and proj_key in proj:
                    try:
                        proj_val = float(proj[proj_key])
                        if proj_val > 0 and pick.line > 0:
                            proj_diff = abs(proj_val - pick.line) / pick.line
                            if proj_diff > 0.25:
                                python_alerts.append({
                                    "player": pick.player,
                                    "market": pick.market,
                                    "issue": (
                                        f"Proyección SportsData={proj_val:.1f} vs línea={pick.line} "
                                        f"(diferencia {proj_diff*100:.0f}%)"
                                    ),
                                    "severity": "warn" if proj_val > pick.line else "error",
                                })
                    except (TypeError, ValueError):
                        pass

        # ── Prompt Claude con los picks para revisión cualitativa ─────────────
        picks_summary = []
        for p in all_picks[:20]:  # limitar a 20 picks para no exceder tokens
            hr15 = p.hit_count_l15 / p.games_l15 if p.games_l15 > 0 else 0
            hr5 = p.hit_count_l5 / p.games_l5 if p.games_l5 > 0 else 0
            picks_summary.append({
                "k": f"{p.player}|{p.market_key}",
                "player": p.player,
                "market": p.market,
                "line": p.line,
                "conf": p.confidence,
                "ev": p.ev_pct,
                "model_prob": p.model_prob,
                "hr15": round(hr15, 3),
                "hr5": round(hr5, 3),
                "streak": p.consecutive_streak,
                "b2b": p.b2b if hasattr(p, 'b2b') else p.is_b2b,
                "inj": (injury_statuses or {}).get(p.player),
            })

        prompt = f"""Revisá la consistencia de estos {len(all_picks)} picks NBA de hoy.
Python pre-validación encontró {len(python_alerts)} alertas.

PICKS (formato JSON):
{json.dumps(picks_summary, ensure_ascii=False, indent=2)}

ALERTAS PRE-VALIDADAS (Python):
{json.dumps(python_alerts, ensure_ascii=False, indent=2)}

Analizá:
1. ¿Algún pick tiene métricas inconsistentes (ev% alto pero hr15 bajo, o viceversa)?
2. ¿Hay picks que deberían excluirse por riesgo de datos desactualizados?
3. ¿El conjunto de picks tiene diversificación razonable (no más de 3 del mismo equipo)?

Respondé SOLO con JSON válido (sin markdown), estructura:
{{
  "alerts": [{{"player": "...", "market": "...", "issue": "...", "severity": "warn|error"}}],
  "excluded_keys": ["player|market_key"],
  "notes": "resumen breve en español"
}}"""

        try:
            raw = self.run(prompt, max_tokens=1024)
            claude_result = self._parse_json(raw, fallback={})
        except Exception as exc:
            logger.warning("[DataValidator] Claude falló, usando solo validación Python: %s", exc)
            claude_result = {}

        # ── Merge Python + Claude alerts ──────────────────────────────────────
        all_alerts = python_alerts + claude_result.get("alerts", [])
        excluded_keys: list[str] = claude_result.get("excluded_keys", [])

        # Solo excluir si severity="error" y explícitamente indicado por Claude
        mismatch_count = len([a for a in all_alerts if a.get("severity") == "error"])
        total_picks = len(all_picks)
        quality_score = round(max(0.0, 1.0 - (mismatch_count / max(total_picks, 1)) * 0.5), 3)

        result = {
            "alerts": all_alerts,
            "excluded_keys": excluded_keys,
            "mismatch_count": mismatch_count,
            "data_quality_score": quality_score,
            "notes": claude_result.get("notes", ""),
        }

        logger.info(
            "[DataValidator] %d picks validados | %d alertas | %d excluidos | calidad=%.2f",
            total_picks,
            len(all_alerts),
            len(excluded_keys),
            quality_score,
        )
        return result
