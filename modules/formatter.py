from datetime import date
from modules.analyzer import Pick


CONFIDENCE_EMOJI = {
    "Alta": "🔥",
    "Media": "⚡",
    "Baja": "❄️",
}


def format_message(picks: list[Pick], history_stats: dict | None = None) -> str:
    today = date.today().strftime("%d/%m/%Y")
    lines = [
        f"🏀 *NBA DAILY DREAM BET* — {today}",
        f"{'─' * 32}",
    ]

    # Historical performance summary (when available)
    if history_stats and history_stats.get("total", 0) > 0:
        total = history_stats["total"]
        correct = history_stats["correct"]
        acc = history_stats["accuracy"] * 100
        l7 = history_stats.get("last_7", {})
        l7_str = ""
        if l7.get("total", 0) > 0:
            l7_str = f" | Últ. 7 días: {l7['correct']}-{l7['total']-l7['correct']} ({l7['accuracy']*100:.0f}%)"
        lines.append(f"📈 *Historial:* {correct}-{total-correct} ({acc:.0f}%){l7_str}")

    lines.append("")

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

        # Recent form context
        form_parts = _format_form(pick)
        if form_parts:
            lines.append(f"📉 *Forma reciente:* {form_parts}")

        # H2H summary
        if pick.h2h_games >= 3 and pick.h2h_summary:
            lines.append(f"⚔️ *H2H:* {pick.h2h_summary}")

        # Market spread
        if pick.market_spread != 0.0:
            lines.append(f"📉 *Spread mercado:* {pick.market_spread:+.1f} (local)")

        # Totals recommendation
        if pick.totals_bet:
            lines.append(f"🔢 *Totales:* {pick.totals_bet} — _{pick.totals_reasoning}_")

        # Fatigue alert
        if pick.home_back_to_back or pick.visitor_back_to_back:
            b2b_parts = []
            if pick.home_back_to_back:
                b2b_parts.append("🏠 Local en B2B")
            if pick.visitor_back_to_back:
                b2b_parts.append("✈️ Visitante en B2B")
            lines.append(f"⚠️ *Alerta fatiga:* {' | '.join(b2b_parts)}")

        # Player props
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


def _format_form(pick: Pick) -> str:
    """Build a compact recent-form string showing streak and win% for each team."""
    parts = []

    home_str = _streak_label(pick.home_streak)
    if home_str or pick.home_recent_win_pct > 0:
        win_pct = f"{pick.home_recent_win_pct*100:.0f}%"
        streak = f" {home_str}" if home_str else ""
        parts.append(f"{pick.home_team.split()[-1]} {win_pct}{streak}")

    visitor_str = _streak_label(pick.visitor_streak)
    if visitor_str or pick.visitor_recent_win_pct > 0:
        win_pct = f"{pick.visitor_recent_win_pct*100:.0f}%"
        streak = f" {visitor_str}" if visitor_str else ""
        parts.append(f"{pick.visitor_team.split()[-1]} {win_pct}{streak}")

    return " | ".join(parts) if parts else ""


def _streak_label(streak: int) -> str:
    """Convert streak int to a label like 🔥W5 or 🧊L3."""
    if streak >= 4:
        return f"🔥G{streak}"
    elif streak <= -4:
        return f"🧊P{abs(streak)}"
    elif streak > 0:
        return f"G{streak}"
    elif streak < 0:
        return f"P{abs(streak)}"
    return ""
