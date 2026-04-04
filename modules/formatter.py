from datetime import datetime
from zoneinfo import ZoneInfo

from modules.analyzer import PlayerPick

ET = ZoneInfo("America/New_York")

CONFIDENCE_EMOJI = {
    "Alta": "🔥",
    "Media": "⚡",
    "Baja": "❄️",
    "Riesgosa": "🎲",
}

def format_message(picks_by_game: dict[str, list[PlayerPick]]) -> str:
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
    lines.append(f"<i>{total_picks} picks en {len(picks_by_game)} partido(s)</i>")
    lines.append("")

    for game_label, picks in picks_by_game.items():
        lines.append(f"<b>{_h(game_label)}</b>")
        lines.append("─" * 28)

        for pick in picks:
            lines += _format_pick(pick)
            lines.append("")

        lines.append("")

    lines.append("<i>Análisis basado en datos históricos reales. Apostá con responsabilidad.</i>")

    return "\n".join(lines)


def _format_pick(pick: PlayerPick) -> list[str]:
    emoji = CONFIDENCE_EMOJI.get(pick.confidence, "")
    price_str = f" ({pick.price:+d})" if pick.price else ""
    side_upper = pick.side.upper()

    lines = [
        f"{emoji} <b>{_h(pick.player)} — {_h(pick.market)} {side_upper} {pick.line}</b>{price_str}",
    ]

    # Headline sentences (natural language reasoning)
    for sentence in pick.headline.split(" | "):
        if sentence.strip():
            lines.append(f"  {_h(sentence)}")

    # Stats detail in monospace
    lines.append(f"  <code>{pick.detail}</code>")

    # Streak badge
    if pick.consecutive_streak >= 3:
        lines.append(
            f"  🔁 Racha: {pick.consecutive_streak} partidos consecutivos {pick.side}"
        )

    # Injury warning
    if pick.injury_status:
        lines.append(f"  🚨 <b>Lesión:</b> {_h(pick.injury_status)}")

    # B2B warning
    if pick.is_b2b:
        lines.append("  ⚠️ Back-to-back hoy")

    lines.append(f"  {emoji} <b>Confianza: {pick.confidence}</b>")

    return lines


def _h(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
