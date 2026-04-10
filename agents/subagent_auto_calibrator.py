"""
subagent_auto_calibrator.py — Calibración automática al final del día.

Analiza los picks de ayer (ya backtesteados) y genera sugerencias de mejora
al modelo en formato JSON, que se guarda en data/calibration_suggestions.json.

Analiza:
  - Hit rate real vs EV% esperado (¿el modelo está bien calibrado?)
  - Mercados con underperformance sistemática
  - Patrones: B2B, confianza, líneas altas vs bajas
  - Sugerencias concretas para ajustar thresholds

Output guardado en data/calibration_suggestions.json:
  {
    "date": str,
    "accuracy_yesterday": float,
    "insights": [str],
    "threshold_suggestions": {
      "ALTA_HIT_RATE": float,
      "MEDIA_HIT_RATE": float,
      ...
    },
    "market_notes": {"market_key": str},
  }
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)

_CALIBRATION_FILE = Path("data/calibration_suggestions.json")


class AutoCalibratorAgent(BaseAgent):
    """Subagent 6: Calibración automática basada en resultados históricos."""

    def __init__(self) -> None:
        super().__init__("AutoCalibrator", model="claude-haiku-4-5-20251001")

    def calibrate(
        self,
        history: dict,
        accuracy: dict,
        date_str: str,
    ) -> dict:
        """
        Analiza resultados históricos y genera sugerencias de calibración.

        Args:
            history:   picks_history.json completo.
            accuracy:  Output de get_calibration_factors (history.py).
            date_str:  Fecha de hoy.

        Returns:
            Dict con insights y suggestions (también guardado en disco).
        """
        if not history:
            return {"insights": [], "threshold_suggestions": {}, "market_notes": {}}

        # ── Preparar datos de los últimos 7 días para el análisis ─────────────
        recent_picks: list[dict] = []
        today = datetime.strptime(date_str, "%Y-%m-%d")

        for day_offset in range(1, 8):
            day = (today - timedelta(days=day_offset)).strftime("%Y-%m-%d")
            day_picks = history.get(day, [])
            for p in day_picks:
                if p.get("hit") is not None:  # solo picks ya resueltos
                    recent_picks.append({
                        "date":       day,
                        "player":     p.get("player"),
                        "market":     p.get("market"),
                        "market_key": p.get("market_key"),
                        "confidence": p.get("confidence"),
                        "ev_pct":     p.get("ev_pct"),
                        "model_prob": p.get("model_prob"),
                        "hit":        p.get("hit"),
                    })

        if len(recent_picks) < 5:
            logger.info("[AutoCalibrator] Insuficientes picks resueltos (%d) para calibrar", len(recent_picks))
            return {"insights": ["Datos insuficientes para calibrar (< 5 picks resueltos en últimos 7 días)"],
                    "threshold_suggestions": {}, "market_notes": {}}

        # Estadísticas agregadas por mercado y confianza
        by_market: dict[str, dict] = {}
        by_conf: dict[str, dict] = {}

        for p in recent_picks:
            mk = p.get("market_key", "unknown")
            conf = p.get("confidence", "Baja")

            if mk not in by_market:
                by_market[mk] = {"hits": 0, "total": 0}
            by_market[mk]["total"] += 1
            if p["hit"]:
                by_market[mk]["hits"] += 1

            if conf not in by_conf:
                by_conf[conf] = {"hits": 0, "total": 0, "ev_sum": 0}
            by_conf[conf]["total"] += 1
            if p["hit"]:
                by_conf[conf]["hits"] += 1
            by_conf[conf]["ev_sum"] += p.get("ev_pct", 0) or 0

        # Calcular hit rates
        market_stats = {
            mk: {
                "hit_rate": round(v["hits"] / v["total"], 3),
                "hits": v["hits"],
                "total": v["total"],
            }
            for mk, v in by_market.items() if v["total"] >= 3
        }
        conf_stats = {
            c: {
                "hit_rate": round(v["hits"] / v["total"], 3),
                "avg_ev":   round(v["ev_sum"] / v["total"], 2),
                "total":    v["total"],
            }
            for c, v in by_conf.items() if v["total"] >= 3
        }

        # Overall accuracy de ayer
        yesterday_acc = accuracy.get("yesterday_accuracy", {})
        overall_acc   = accuracy.get("overall_accuracy", {})

        prompt = f"""Analizá el performance histórico del modelo NBA props de los últimos 7 días.

ACCURACY GENERAL:
- Ayer: {yesterday_acc}
- Overall (últimos 60 días): {overall_acc}

HIT RATES POR MERCADO (últimos 7 días):
{json.dumps(market_stats, ensure_ascii=False, indent=2)}

HIT RATES POR CONFIANZA:
{json.dumps(conf_stats, ensure_ascii=False, indent=2)}

PARÁMETROS ACTUALES DEL MODELO:
- ALTA_HIT_RATE: 0.80 (con ALTA_EDGE >= 10%)
- MEDIA_HIT_RATE: 0.67 (con MEDIA_EDGE >= 5%)
- MIN_EV_THRESHOLD: ~3% (variable según calibración)
- STEALS_MIN_HIT_L15: 0.73
- BLOWOUT_SPREAD_THRESHOLD: 12.0

Generá sugerencias CONCRETAS y CONSERVADORAS de ajuste.
NO sugerís cambios dramáticos (max ±0.05 en thresholds de hit rate).

Devolvé SOLO JSON:
{{
  "insights": [
    "El mercado player_rebounds está underperformando (HR 52% vs esperado 67%)",
    "Picks Alta están calibrados correctamente"
  ],
  "threshold_suggestions": {{
    "ALTA_HIT_RATE": 0.80,
    "MEDIA_HIT_RATE": 0.65,
    "STEALS_MIN_HIT_L15": 0.75
  }},
  "market_notes": {{
    "player_rebounds": "Considerar subir threshold L15 a 70%",
    "player_steals":   "Performance sólido, mantener filtro actual"
  }},
  "overall_assessment": "1-2 oraciones en español con la evaluación general"
}}

Solo incluí en threshold_suggestions los parámetros que REALMENTE necesitan cambio."""

        try:
            raw = self.run(prompt, max_tokens=1024)
            result = self._parse_json(raw, fallback={})
        except Exception as exc:
            logger.warning("[AutoCalibrator] Claude falló: %s", exc)
            result = {}

        output = {
            "date":                  date_str,
            "accuracy_yesterday":    yesterday_acc,
            "accuracy_overall":      overall_acc,
            "recent_picks_analyzed": len(recent_picks),
            "insights":              result.get("insights", []),
            "threshold_suggestions": result.get("threshold_suggestions", {}),
            "market_notes":          result.get("market_notes", {}),
            "overall_assessment":    result.get("overall_assessment", ""),
        }

        # Guardar en disco
        try:
            _CALIBRATION_FILE.parent.mkdir(exist_ok=True)

            existing: list[dict] = []
            if _CALIBRATION_FILE.exists():
                try:
                    existing = json.loads(_CALIBRATION_FILE.read_text(encoding="utf-8"))
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []

            existing.append(output)
            # Mantener últimas 30 entradas
            existing = existing[-30:]
            _CALIBRATION_FILE.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("[AutoCalibrator] Sugerencias guardadas en %s", _CALIBRATION_FILE)
        except Exception as exc:
            logger.warning("[AutoCalibrator] No se pudo guardar en disco: %s", exc)

        insights = result.get("insights", [])
        logger.info("[AutoCalibrator] %d insights generados", len(insights))
        for ins in insights:
            logger.info("  → %s", ins)

        return output
