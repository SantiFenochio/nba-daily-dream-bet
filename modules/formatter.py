from datetime import datetime
from zoneinfo import ZoneInfo

from modules.analyzer import Pick

ET = ZoneInfo("America/New_York")

CONFIDENCE_EMOJI = {
    "Alta": "🔥",
    "Media": "⚡",
    "Baja": "❄️",
}


def format_message(picks: list[Pick]) -> str:
    today = datetime.now(ET).strftime("%d/%m/%Y")
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
            if pick.low_confidence_props:
                lines.append("⚠️ *Props \\(Baja confianza — ninguno supera 60% prob\\):*")
            else:
                lines.append("🎯 *Props destacados:*")
            for prop in pick.props:
                hit_pct = f"{prop['hit_rate']*100:.0f}%"
                price_str = f" ({prop['price']:+d})" if isinstance(prop["price"], int) else ""
                lines.append(
                    f"  • {prop['player']} — {prop['market']} {prop['side']} "
                    f"{prop['line']}{price_str} \\[prob: {hit_pct}\\]"
                )

        lines.append("")

    lines.append("_Análisis generado automáticamente\\. Apostá con responsabilidad\\._")
    return "\n".join(lines)
