from datetime import datetime
from zoneinfo import ZoneInfo

from modules.analyzer import PlayerPick

ET = ZoneInfo("America/New_York")

CONFIDENCE_EMOJI = {
    "Alta":     "🔥",
    "Media":    "⚡",
    "Baja":     "❄️",
    "Riesgosa": "🎲",
}


def format_message(
    picks_by_game: dict[str, list[PlayerPick]],
    game_times: dict[str, str] | None = None,
) -> str:
    today = datetime.now(ET).strftime("%d/%m/%Y")
    lines = [
        f"🏀 <b>NBA DAILY DREAM BET — {today}</b>",
        "─" * 32,
        "",
    ]

    if not picks_by_game:
        lines.append("No hay picks disponibles hoy.")
        return "\n".join(lines)

    total_picks = sum(len(v) for v in picks_by_game.values())
    lines.append(f"<i>{total_picks} picks con EV positivo en {len(picks_by_game)} partido(s)</i>")
    lines.append("")

    gt = game_times or {}

    for game_label, picks in picks_by_game.items():
        hora = gt.get(game_label)
        game_header = f"<b>{_h(game_label)}</b>"
        if hora:
            game_header += f"  <i>🕐 {hora}</i>"
        lines.append(game_header)
        lines.append("─" * 28)

        for pick in picks:
            lines += _format_pick(pick)
            lines.append("")

        lines.append("")

    lines.append(
        "<i>EV calculado via devig. Probabilidad: Poisson + Bayes. "
        "Apostá con responsabilidad.</i>"
    )
    return "\n".join(lines)


def _format_pick(pick: PlayerPick) -> list[str]:
    emoji = CONFIDENCE_EMOJI.get(pick.confidence, "")
    price_str = f" ({pick.price:+d})" if pick.price else ""
    side_upper = pick.side.upper()

    lines = [
        f"{emoji} <b>{_h(pick.player)} — {_h(pick.market)} {side_upper} {pick.line}</b>{price_str}",
    ]

    # Natural language headline (split on pipe)
    for sentence in pick.headline.split(" | "):
        sentence = sentence.strip()
        if sentence:
            lines.append(f"  {_h(sentence)}")

    # Stats detail in monospace
    lines.append(f"  <code>{pick.detail}</code>")

    # EV + probability row
    ev_bar = _ev_bar(pick.ev_pct)
    lines.append(
        f"  📊 <b>EV: +{pick.ev_pct:.1f}%</b> {ev_bar} | "
        f"Prob. modelo: {pick.model_prob*100:.0f}% | "
        f"Mercado justo: {pick.fair_prob*100:.0f}%"
    )

    # Kelly stake suggestion
    if pick.kelly_pct > 0:
        lines.append(f"  💰 Kelly 1/4: <b>{pick.kelly_pct:.2f}% del bankroll</b>")

    # Streak badge
    if pick.consecutive_streak >= 3:
        lines.append(
            f"  🔁 Racha: {pick.consecutive_streak} partidos consecutivos {pick.side}"
        )

    # Context factor flags
    context_flags = []
    if pick.pace_factor >= 1.03:
        context_flags.append(f"ritmo elevado (+{(pick.pace_factor-1)*100:.1f}%)")
    elif pick.pace_factor <= 0.97:
        context_flags.append(f"ritmo lento ({(pick.pace_factor-1)*100:.1f}%)")
    if pick.dvp_factor >= 1.04:
        context_flags.append(f"defensa rival débil (+{(pick.dvp_factor-1)*100:.1f}%)")
    elif pick.dvp_factor <= 0.97:
        context_flags.append(f"defensa rival sólida ({(pick.dvp_factor-1)*100:.1f}%)")
    if context_flags:
        lines.append(f"  🧮 Contexto: {', '.join(context_flags)}")

    # Blowout risk warning
    if pick.blowout_risk:
        lines.append("  ⚡ <b>Riesgo paliza:</b> favorito 12+ pts — podría salir en el 4to")

    # Teammate absence boost
    if pick.absence_boost > 1.0:
        pct = round((pick.absence_boost - 1.0) * 100)
        lines.append(f"  📈 Compañero ausente — uso proyectado +{pct}%")

    # Injury warning
    if pick.injury_status:
        lines.append(f"  🚨 <b>Lesión:</b> {_h(pick.injury_status)}")

    # B2B warning
    if pick.is_b2b:
        lines.append("  ⚠️ Back-to-back hoy (−7% proyectado)")

    lines.append(f"  {emoji} <b>Confianza: {pick.confidence}</b>")
    return lines


def _ev_bar(ev_pct: float) -> str:
    """Mini visual bar for EV strength."""
    if ev_pct >= 15:
        return "█████"
    if ev_pct >= 10:
        return "████░"
    if ev_pct >= 7:
        return "███░░"
    if ev_pct >= 4:
        return "██░░░"
    return "█░░░░"


def _h(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
