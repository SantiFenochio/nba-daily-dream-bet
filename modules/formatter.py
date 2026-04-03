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
        lines += [
            f"*Partido {i}:* {pick.game_label}",
            f"📌 *Apuesta:* {pick.recommended_bet}",
            f"📊 *Análisis:* {pick.reasoning}",
            f"{emoji} *Confianza:* {pick.confidence}",
        ]

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
