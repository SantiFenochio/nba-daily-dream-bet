"""
subagent_narrator.py — Generador del mensaje final de Telegram.

Toma toda la data procesada por los agentes anteriores y genera
un mensaje en español argentino natural, con HTML de Telegram,
emojis y mención explícita al EV real.

El Narrator es el único agente que usa claude-sonnet-4-6 (el más capaz)
porque la calidad del mensaje final es lo que ve el usuario.

Output:
  {
    "message": str,       # HTML para Telegram (max ~4000 chars)
    "char_count": int,
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

_MAX_TELEGRAM_CHARS = 3900  # margen por debajo de 4096


class NarratorAgent(BaseAgent):
    """Subagent 5: Generación del mensaje Telegram en español argentino."""

    def __init__(self) -> None:
        # Sonnet para calidad máxima en el output visible al usuario
        super().__init__("Narrator", model="claude-sonnet-4-6")

    def generate(
        self,
        picks_by_game: dict[str, list["PlayerPick"]],
        parlays: list[dict],
        escalera_data: dict | None,
        consistency_picks: list | None,
        accuracy: dict,
        date_str: str,
        fallback_mode: bool,
        # Outputs de otros agentes
        news_summary: str = "",
        validation_alerts: list[dict] | None = None,
        projection_insights: list[str] | None = None,
        best_parlay_key: str | None = None,
        ev_ranking: list[dict] | None = None,
        parlay_commentary: str = "",
    ) -> dict:
        """
        Genera el mensaje Telegram completo.

        Args:
            picks_by_game:       Picks finales (post-ajustes de todos los agentes).
            parlays:             Parlays optimizados (con MC joint probs).
            escalera_data:       Data de la escalera del día.
            consistency_picks:   Picks de consistencia.
            accuracy:            Dict de accuracy histórico (del history.py).
            date_str:            Fecha YYYY-MM-DD.
            fallback_mode:       Si estamos en modo fallback (EV threshold bajado).
            news_summary:        Resumen de noticias del NewsIntelligenceAgent.
            validation_alerts:   Alertas del DataValidatorAgent.
            projection_insights: Insights del ProjectionAgent.
            best_parlay_key:     Parlay recomendado por EVOptimizerAgent.
            ev_ranking:          Ranking de parlays por MC joint prob.
            parlay_commentary:   Comentario del EVOptimizerAgent.

        Returns:
            Dict con message (HTML Telegram) y char_count.
        """
        # ── Serializar picks para el prompt ───────────────────────────────────
        picks_data: list[dict] = []
        for game, game_picks in picks_by_game.items():
            for p in game_picks:
                hr15 = round(p.hit_count_l15 / p.games_l15, 3) if p.games_l15 > 0 else 0
                picks_data.append({
                    "game":    game,
                    "player":  p.player,
                    "market":  p.market,
                    "line":    p.line,
                    "price":   p.price,
                    "conf":    p.confidence,
                    "ev":      p.ev_pct,
                    "hr15":    hr15,
                    "hr5":     round(p.hit_count_l5 / p.games_l5, 3) if p.games_l5 > 0 else 0,
                    "streak":  p.consecutive_streak,
                    "avg15":   p.avg_l15,
                    "b2b":     p.is_b2b,
                    "inj":     p.injury_status,
                    "model_p": p.model_prob,
                })

        # Parlays resumidos
        parlays_data = []
        for parl in parlays:
            legs_str = " + ".join(
                f"{p.player} {p.market} O{p.line}" for (_, p) in parl.get("legs", [])
            )
            parlays_data.append({
                "name":       parl["name"],
                "legs":       legs_str,
                "joint_prob": parl.get("corr_joint_prob", parl.get("hit_rate_product", 0)),
                "ev_pct":     parl.get("parlay_ev_pct"),
                "is_best":    parl["name"] == best_parlay_key,
            })

        # Escalera resumida
        esc_str = ""
        if escalera_data:
            esc_str = (
                f"Jugador: {escalera_data.get('player')} | "
                f"Líneas: {escalera_data.get('lines', [])} | "
                f"Análisis: {escalera_data.get('analysis', '')[:200]}"
            )

        # Accuracy
        yesterday_acc = accuracy.get("yesterday_accuracy", {})
        overall_acc = accuracy.get("overall_accuracy", {})
        acc_str = ""
        if yesterday_acc.get("total", 0) > 0:
            acc_str = (
                f"Ayer: {yesterday_acc.get('hits',0)}/{yesterday_acc.get('total',0)} "
                f"({yesterday_acc.get('accuracy',0)*100:.0f}%)"
            )
        if overall_acc.get("total", 0) >= 10:
            acc_str += (
                f" | Histórico: {overall_acc.get('accuracy',0)*100:.0f}% "
                f"({overall_acc.get('total',0)} picks)"
            )

        prompt = f"""Generá el mensaje diario de NBA Daily Dream Bet para Telegram.

FECHA: {date_str}
MODO FALLBACK: {fallback_mode}

=== PICKS DEL DÍA ({len(picks_data)} picks) ===
{json.dumps(picks_data, ensure_ascii=False, indent=2)}

=== PARLAYS ===
{json.dumps(parlays_data, ensure_ascii=False, indent=2)}
Recomendación del optimizador: {parlay_commentary}

=== ESCALERA DEL DÍA ===
{esc_str or 'Sin escalera disponible'}

=== NOTICIAS DE ÚLTIMO MOMENTO ===
{news_summary or 'Sin novedades de último momento.'}

=== ALERTAS DE VALIDACIÓN ===
{json.dumps(validation_alerts or [], ensure_ascii=False)}

=== INSIGHTS DE PROYECCIÓN ===
{json.dumps(projection_insights or [], ensure_ascii=False)}

=== ACCURACY HISTÓRICO ===
{acc_str or 'Primeros datos'}

=== INSTRUCCIONES DE FORMATO ===
Generá un mensaje HTML para Telegram con esta estructura EXACTA:

1. Header: "🏀 NBA Daily Dream Bet — [fecha]" en bold
2. Si hay accuracy: línea con resultados de ayer
3. Sección "PICKS DEL DÍA" con cada game en bold, y por pick:
   - Jugador | Mercado Over X.X | Confianza emoji (🔥=Alta, ⚡=Media)
   - HR L15 / L5 | EV: X% | Precio: +/-XXX
   - Si B2B: ⚠️ B2B | Si hay lesión: 🩹 estado
   - Si racha >= 3: 🔥 racha de N
4. Sección "PARLAYS" con los 4 parlays (marca el mejor con ⭐)
5. Sección "ESCALERA DEL DÍA" si existe
6. Sección "NOTICIAS" si hay algo relevante
7. Footer con aviso de responsabilidad

HTML PERMITIDO: <b>, <i>, <code>, <pre>
NO USAR: <br>, <div>, <span>, <a>
SEPARADORES DE LÍNEA: usa \\n natural
MÁXIMO: {_MAX_TELEGRAM_CHARS} caracteres

ESTILO: Español argentino, profesional pero con carácter. Mostrá el EV% cuando sea >= 5%.
Si es fallback mode, aclaralo de forma sutil al inicio."""

        try:
            message = self.run(prompt, max_tokens=4096)
        except Exception as exc:
            logger.error("[Narrator] Falló la generación del mensaje: %s", exc)
            return {"message": "", "char_count": 0}

        # Truncar si supera el límite de Telegram
        if len(message) > _MAX_TELEGRAM_CHARS:
            cut = message[:_MAX_TELEGRAM_CHARS].rfind("\n\n")
            if cut > 2000:
                message = message[:cut] + "\n\n<i>...mensaje truncado</i>"

        char_count = len(message)
        logger.info("[Narrator] Mensaje generado: %d caracteres", char_count)

        return {"message": message, "char_count": char_count}
