"""
orchestrator.py — Cerebro central del sistema multi-agent NBA.

Coordina los 6 subagents en secuencia:

  1. DataValidatorAgent    → valida consistencia de picks
  2. NewsIntelligenceAgent → noticias de último momento
  3. ProjectionAgent       → Monte Carlo + ajuste cualitativo
  4. Aplicar refinements   → modifica scores/confidence de picks en-place
  5. EVOptimizerAgent      → Cholesky MC para parlays
  6. NarratorAgent         → genera mensaje Telegram final
  7. AutoCalibratorAgent   → sugerencias de calibración (al final)

Si ANTHROPIC_API_KEY no está seteada, el Orchestrator devuelve None
y main.py usa el formatter.py existente como fallback.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agents.subagent_data_validator import DataValidatorAgent
from agents.subagent_projection import ProjectionAgent
from agents.subagent_news_intelligence import NewsIntelligenceAgent
from agents.subagent_ev_optimizer import EVOptimizerAgent
from agents.subagent_narrator import NarratorAgent
from agents.subagent_auto_calibrator import AutoCalibratorAgent

if TYPE_CHECKING:
    from modules.analyzer import PlayerPick

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorResult:
    """Resultado completo del pipeline multi-agent."""
    message:            str                         # Mensaje Telegram generado por Narrator
    enhanced_parlays:   list[dict]                  # Parlays con MC joint probs
    best_parlay_key:    str | None                  # Parlay recomendado
    news_summary:       str                         # Resumen de noticias
    validation_report:  dict                        # Output del DataValidator
    projection_result:  dict                        # Output del ProjectionAgent
    calibration_result: dict                        # Output del AutoCalibrator


class Orchestrator:
    """
    Coordinador principal del sistema multi-agent NBA.

    Uso básico:
        orch = Orchestrator()
        result = orch.run(picks_by_game, player_logs, ...)
        # result.message → Telegram HTML
        # result.enhanced_parlays → parlays con Cholesky MC
    """

    def __init__(self) -> None:
        self.validator   = DataValidatorAgent()
        self.projector   = ProjectionAgent()
        self.news        = NewsIntelligenceAgent()
        self.ev_opt      = EVOptimizerAgent()
        self.narrator    = NarratorAgent()
        self.calibrator  = AutoCalibratorAgent()

    @staticmethod
    def is_available() -> bool:
        """Retorna True si ANTHROPIC_API_KEY está seteada en el entorno."""
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    def run(
        self,
        picks_by_game: dict[str, list["PlayerPick"]],
        player_logs: dict[str, list[dict]],
        injury_statuses: dict[str, str | None],
        projections: dict[str, dict],
        prop_records: list[dict],
        games: list[dict],
        game_lines: dict,
        parlays: list[dict],
        escalera_data: dict | None,
        consistency_picks: list | None,
        accuracy: dict,
        history: dict,
        date_str: str,
        fallback_mode: bool = False,
    ) -> OrchestratorResult:
        """
        Pipeline completo: valida → noticias → proyecta → optimiza → narra → calibra.

        Args:
            picks_by_game:    Output del analyzer.py (puede ser modificado in-place).
            player_logs:      Histórico de juegos ESPN.
            injury_statuses:  Estado de lesiones (ESPN + overrides).
            projections:      SportsData projections.
            prop_records:     Props crudos parseados.
            games:            Partidos del día (BallDontLie).
            game_lines:       Spreads/totals (Odds API).
            parlays:          Parlays del parlay_builder.py.
            escalera_data:    Output del escalera.py.
            consistency_picks: Output del consistency_picks.py.
            accuracy:         Output de get_calibration_factors.
            history:          picks_history.json completo.
            date_str:         Fecha YYYY-MM-DD.
            fallback_mode:    Si el analyzer usó EV threshold reducido.

        Returns:
            OrchestratorResult con message, parlays mejorados y metadata.
        """
        logger.info("[Orchestrator] Iniciando pipeline multi-agent para %s", date_str)

        all_picks_count = sum(len(v) for v in picks_by_game.values())
        logger.info("[Orchestrator] Picks a procesar: %d en %d juegos",
                    all_picks_count, len(picks_by_game))

        # ────────────────────────────────────────────────────────────────────
        # PASO 1: Validación de datos
        # ────────────────────────────────────────────────────────────────────
        logger.info("[Orchestrator] [1/6] DataValidator...")
        validation_report: dict = {}
        try:
            validation_report = self.validator.validate(
                picks_by_game=picks_by_game,
                projections=projections,
                injury_statuses=injury_statuses,
            )
            n_alerts = len(validation_report.get("alerts", []))
            n_excluded = len(validation_report.get("excluded_keys", []))
            logger.info("[Orchestrator] Validación: %d alertas | %d excluidos | calidad=%.2f",
                        n_alerts, n_excluded, validation_report.get("data_quality_score", 1.0))

            # Aplicar exclusiones recomendadas (solo errores graves)
            if validation_report.get("excluded_keys"):
                _apply_exclusions(picks_by_game, validation_report["excluded_keys"])

        except Exception as exc:
            logger.warning("[Orchestrator] DataValidator falló (no bloqueante): %s", exc)
            validation_report = {"alerts": [], "excluded_keys": [], "data_quality_score": 1.0}

        # ────────────────────────────────────────────────────────────────────
        # PASO 2: Noticias de último momento
        # ────────────────────────────────────────────────────────────────────
        logger.info("[Orchestrator] [2/6] NewsIntelligence...")
        news_result: dict = {}
        try:
            news_result = self.news.gather(
                picks_by_game=picks_by_game,
                date_str=date_str,
            )
            n_news = len(news_result.get("news_items", []))
            n_adj  = len(news_result.get("adjustments", {}))
            logger.info("[Orchestrator] Noticias: %d items | %d ajustes de picks", n_news, n_adj)
        except Exception as exc:
            logger.warning("[Orchestrator] NewsIntelligence falló (no bloqueante): %s", exc)
            news_result = {"adjustments": {}, "news_items": [], "summary": ""}

        # ────────────────────────────────────────────────────────────────────
        # PASO 3: Proyecciones Monte Carlo + ajuste cualitativo
        # ────────────────────────────────────────────────────────────────────
        logger.info("[Orchestrator] [3/6] ProjectionAgent (Monte Carlo)...")
        projection_result: dict = {}
        try:
            projection_result = self.projector.enhance(
                picks_by_game=picks_by_game,
                player_logs=player_logs,
                injury_statuses=injury_statuses,
            )
            logger.info("[Orchestrator] MC: %d probs calculadas | %d ajustes | %d flagged",
                        len(projection_result.get("mc_probs", {})),
                        len(projection_result.get("adjustments", [])),
                        len(projection_result.get("flagged", [])))
        except Exception as exc:
            logger.warning("[Orchestrator] ProjectionAgent falló (no bloqueante): %s", exc)
            projection_result = {"mc_probs": {}, "adjustments": [], "insights": [], "flagged": []}

        # ────────────────────────────────────────────────────────────────────
        # PASO 4: Aplicar refinements de News + Projection a los picks
        # ────────────────────────────────────────────────────────────────────
        logger.info("[Orchestrator] [4/6] Aplicando refinements a picks...")
        _apply_refinements(
            picks_by_game=picks_by_game,
            news_adjustments=news_result.get("adjustments", {}),
            projection_adjustments=projection_result.get("adjustments", []),
        )

        # ────────────────────────────────────────────────────────────────────
        # PASO 5: Optimización de parlays (Cholesky MC)
        # ────────────────────────────────────────────────────────────────────
        logger.info("[Orchestrator] [5/6] EVOptimizer (Cholesky MC)...")
        ev_result: dict = {}
        try:
            ev_result = self.ev_opt.optimize(
                picks_by_game=picks_by_game,
                existing_parlays=parlays,
                news_adjustments=news_result.get("adjustments"),
            )
            logger.info("[Orchestrator] EV: %d parlays optimizados | mejor=%s",
                        len(ev_result.get("enhanced_parlays", [])),
                        ev_result.get("best_parlay_key"))
        except Exception as exc:
            logger.warning("[Orchestrator] EVOptimizer falló (no bloqueante): %s", exc)
            ev_result = {"enhanced_parlays": parlays, "best_parlay_key": None,
                         "ev_ranking": [], "commentary": ""}

        enhanced_parlays = ev_result.get("enhanced_parlays") or parlays

        # ────────────────────────────────────────────────────────────────────
        # PASO 6: Generar mensaje Telegram (Narrator)
        # ────────────────────────────────────────────────────────────────────
        logger.info("[Orchestrator] [6/6] Narrator (generando mensaje)...")
        message = ""
        try:
            narrator_result = self.narrator.generate(
                picks_by_game=picks_by_game,
                parlays=enhanced_parlays,
                escalera_data=escalera_data,
                consistency_picks=consistency_picks,
                accuracy=accuracy,
                date_str=date_str,
                fallback_mode=fallback_mode,
                news_summary=news_result.get("summary", ""),
                validation_alerts=validation_report.get("alerts", []),
                projection_insights=projection_result.get("insights", []),
                best_parlay_key=ev_result.get("best_parlay_key"),
                ev_ranking=ev_result.get("ev_ranking", []),
                parlay_commentary=ev_result.get("commentary", ""),
            )
            message = narrator_result.get("message", "")
            logger.info("[Orchestrator] Mensaje generado: %d chars", narrator_result.get("char_count", 0))
        except Exception as exc:
            logger.error("[Orchestrator] Narrator falló: %s", exc)

        # ────────────────────────────────────────────────────────────────────
        # PASO 7: Auto-Calibración (al final, no bloqueante)
        # ────────────────────────────────────────────────────────────────────
        calibration_result: dict = {}
        try:
            logger.info("[Orchestrator] [7/7] AutoCalibrator...")
            calibration_result = self.calibrator.calibrate(
                history=history,
                accuracy=accuracy,
                date_str=date_str,
            )
        except Exception as exc:
            logger.warning("[Orchestrator] AutoCalibrator falló (no bloqueante): %s", exc)

        logger.info("[Orchestrator] Pipeline completado para %s", date_str)

        return OrchestratorResult(
            message=message,
            enhanced_parlays=enhanced_parlays,
            best_parlay_key=ev_result.get("best_parlay_key"),
            news_summary=news_result.get("summary", ""),
            validation_report=validation_report,
            projection_result=projection_result,
            calibration_result=calibration_result,
        )


# ── Funciones auxiliares del Orchestrator ─────────────────────────────────────

def _apply_exclusions(
    picks_by_game: dict[str, list["PlayerPick"]],
    excluded_keys: list[str],
) -> None:
    """Elimina picks marcados como excluidos por el DataValidator (solo severity=error)."""
    excluded_set = set(excluded_keys)
    for game, game_picks in picks_by_game.items():
        before = len(game_picks)
        picks_by_game[game] = [
            p for p in game_picks
            if f"{p.player}|{p.market_key}" not in excluded_set
        ]
        removed = before - len(picks_by_game[game])
        if removed:
            logger.info("[Orchestrator] Excluidos %d picks en %s por DataValidator", removed, game)


def _apply_refinements(
    picks_by_game: dict[str, list["PlayerPick"]],
    news_adjustments: dict[str, dict],
    projection_adjustments: list[dict],
) -> None:
    """
    Aplica refinements de News + Projection a los picks en-place.

    - News adjustments: por player_name → {"factor": float, "reason": str}
    - Projection adjustments: [{"key": "player|market_key", "factor": float}]

    Los factors se multiplican al score existente (nunca reemplazan la lógica base).
    Constraints: factor clampeado entre 0.75 y 1.25 para proteger la lógica original.
    """
    # Construir lookup de projection adjustments por key
    proj_map: dict[str, float] = {}
    for adj in projection_adjustments:
        key = adj.get("key", "")
        factor = float(adj.get("factor", 1.0))
        # Clamp conservador
        proj_map[key] = max(0.75, min(1.25, factor))

    total_adjusted = 0
    for game_picks in picks_by_game.values():
        for pick in game_picks:
            key = f"{pick.player}|{pick.market_key}"
            composite_factor = 1.0

            # News adjustment (por player_name)
            if pick.player in news_adjustments:
                news_factor = float(news_adjustments[pick.player].get("factor", 1.0))
                news_factor = max(0.75, min(1.25, news_factor))
                composite_factor *= news_factor
                if news_factor != 1.0:
                    logger.debug("[Orchestrator] News adjustment %s: ×%.3f (%s)",
                                 pick.player, news_factor,
                                 news_adjustments[pick.player].get("reason", ""))

            # Projection adjustment (por player|market_key)
            if key in proj_map:
                proj_factor = proj_map[key]
                composite_factor *= proj_factor

            if abs(composite_factor - 1.0) > 0.005:
                pick.score = round(pick.score * composite_factor, 4)
                total_adjusted += 1

    if total_adjusted:
        logger.info("[Orchestrator] %d picks ajustados por News + Projection refinements", total_adjusted)
