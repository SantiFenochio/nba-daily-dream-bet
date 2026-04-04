from datetime import date
from modules.analyzer import Pick


CONFIDENCE_EMOJI = {
    "Alta": "🔥",
    "Media": "⚡",
    "Baja": "❄️",
}


def format_message(picks: list[Pick]) -> str:
    today = date.today().strftime("%d/%m/%Y")
    lines = [
        f"🏀 *NBA DAILY DREAM BET* — {today}",
        f"{'─' * 32}",
        "",
    ]

    if not picks:
        lines.append("No hay partidos hoy.")
        return "\n".join(lines)

    for i, pick in enumerate(picks, start=1):
        emoji = CONFIDENCE_EMOJI.get(pick.confidence, "")
        edge_str = f" (ventaja vs libro: {pick.model_edge*100:.1f}%)" if pick.model_edge > 0 else ""
        lines += [
            f"*Partido {i}:* {pick.game_label}",
            f"📌 *Apuesta:* {pick.recommended_bet}",
            f"📊 *Análisis:* {pick.reasoning}",
            f"{emoji} *Confianza:* {pick.confidence}{edge_str}",
        ]

        if pick.market_spread != 0.0:
            spread_label = f"{pick.market_spread:+.1f} (local)"
            lines.append(f"📉 *Spread mercado:* {spread_label}")

        if pick.totals_bet:
            lines.append(f"🔢 *Totales:* {pick.totals_bet} — _{pick.totals_reasoning}_")

        if pick.home_back_to_back or pick.visitor_back_to_back:
            b2b_parts = []
            if pick.home_back_to_back:
                b2b_parts.append("🏠 Local en B2B")
            if pick.visitor_back_to_back:
                b2b_parts.append("✈️ Visitante en B2B")
            lines.append(f"⚠️ *Alerta fatiga:* {' | '.join(b2b_parts)}")

        if pick.props:
            lines.append("🎯 *Props destacados:*")
            for prop in pick.props:
                price_str = f" ({prop['price']:+d})" if isinstance(prop["price"], int) else ""
                lines.append(
                    f"  • {prop['player']} — {prop['market']} {prop['side']} {prop['line']}{price_str}"
                )

        lines.append("")

    lines.append("_Análisis generado automáticamente. Apostá con responsabilidad._")
    return "\n".join(lines)
