"""
formatter.py — Telegram message formatter (simple mode).

Per-pick block:
  ═══════════════════════
  🏀 JUGADOR | STAT | OVER línea
  📊 L15: X/15 (XX%) | Prom: XX.X (+X.X vs línea)
  📈 L5: X/5 | Racha actual: X seguidos
  💰 Precio: -110
  ✅ Confianza: ALTA
  ═══════════════════════

Per-parlay block:
  🎯 NOMBRE (N patas)
    └─ Jugador | Over X.X Stat  L15: 12/15 (80%)
    └─ ...
  📊 Prob. estimada: ~XX% (multiplicación de hit rates)
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from modules.analyzer import PlayerPick

ET = ZoneInfo("America/New_York")

CONFIDENCE_EMOJI = {
    "Alta":  "✅",
    "Media": "⚡",
    "Baja":  "❄️",
}

PARLAY_EMOJI = {
    "La Segura":        "🛡️",
    "El Balance":       "⚖️",
    "La Arriesgada":    "🎯",
    "Los Consistentes": "💎",
}


def format_message(
    picks_by_game: dict[str, list[PlayerPick]],
    game_times: dict[str, str] | None = None,
    fallback_mode: bool = False,
    parlays: list[dict] | None = None,
    accuracy: dict | None = None,
    escalera_data: dict | None = None,
    consistency_picks: list[dict] | None = None,  # kept for API compat, not shown
) -> str:
    today = datetime.now(ET).strftime("%d/%m/%Y")
    gt = game_times or {}

    if not picks_by_game:
        return f"🏀 <b>NBA DAILY DREAM BET — {today}</b>\n\nNo hay picks disponibles hoy."

    total_picks = sum(len(v) for v in picks_by_game.values())
    alta_count  = sum(1 for picks in picks_by_game.values() for p in picks if p.confidence == "Alta")
    media_count = sum(1 for picks in picks_by_game.values() for p in picks if p.confidence == "Media")

    lines: list[str] = [
        f"🏀 <b>NBA DAILY DREAM BET — {today}</b>",
        f"<i>{total_picks} picks hoy · {alta_count} ✅ Alta · {media_count} ⚡ Media</i>",
    ]

    if fallback_mode:
        lines.append("<i>⚠️ Modo fallback — EV bajo umbral normal</i>")

    # Yesterday accuracy
    if accuracy:
        yday    = accuracy.get("yesterday")
        overall = accuracy.get("overall")
        if yday and yday["total"] > 0:
            pct   = round(yday["rate"] * 100)
            emoji = "✅" if pct >= 60 else "⚠️" if pct >= 40 else "❌"
            hist  = (
                f" | Histórico: {round(overall['rate']*100)}% ({overall['total']} picks)"
                if overall and overall["total"] >= 10 else ""
            )
            lines.append(f"{emoji} <i>Ayer: {yday['hits']}/{yday['total']} ({pct}%){hist}</i>")
        elif overall and overall["total"] >= 10:
            lines.append(
                f"<i>Histórico: {round(overall['rate']*100)}% ({overall['total']} picks resueltos)</i>"
            )

    lines.append("")

    # ── PICKS POR PARTIDO ────────────────────────────────────────────────────
    for game_label, picks in picks_by_game.items():
        hora    = gt.get(game_label)
        display = game_label.replace(" @ ", " vs ")
        header  = f"<b>🏀 {_h(display)}</b>"
        if hora:
            header += f"  🕐 {hora}"
        lines.append(header)
        lines.append("━" * 28)

        for pick in picks:
            lines += _format_pick(pick)
            lines.append("")

    # ── COMBINADAS ───────────────────────────────────────────────────────────
    if parlays:
        lines += [
            "━" * 30,
            "🎰 <b>COMBINADAS DEL DÍA</b>",
            "━" * 30,
            "",
        ]
        for parlay in parlays:
            lines += _format_parlay(parlay)
            lines.append("")

        lines.append("<i>⚠️ Las combinadas multiplican riesgos. Apostá montos pequeños.</i>")

    # ── ESCALERA (optional) ──────────────────────────────────────────────────
    if escalera_data:
        lines += _format_escalera(escalera_data)

    lines.append("")
    lines.append("<i>Apostá con responsabilidad. Solo con plata que puedas perder.</i>")

    return "\n".join(lines)


def _format_pick(pick: PlayerPick) -> list[str]:
    conf_emoji = CONFIDENCE_EMOJI.get(pick.confidence, "")
    hit_pct    = pick.hit_count_l15 / pick.games_l15 * 100 if pick.games_l15 > 0 else 0.0
    edge       = round(pick.avg_l15 - pick.line, 1)
    edge_sign  = "+" if edge >= 0 else ""
    price_str  = f"{pick.price:+d}" if pick.price else "-110"

    out = [
        "═══════════════════════",
        f"🏀 <b>{_h(pick.player)}</b> | {_h(pick.market)} OVER {pick.line}",
        (
            f"📊 L15: {pick.hit_count_l15}/{pick.games_l15} ({hit_pct:.0f}%)"
            f" | Prom: <b>{pick.avg_l15}</b> ({edge_sign}{edge} vs línea)"
        ),
        f"📈 L5: {pick.hit_count_l5}/{pick.games_l5} | Racha actual: {pick.consecutive_streak} seguidos",
        f"💰 Precio: <code>{price_str}</code>",
        f"{conf_emoji} Confianza: <b>{pick.confidence.upper()}</b>",
    ]

    if pick.is_b2b:
        out.append("⚠️ Back-to-back hoy — confianza reducida")

    if pick.injury_status and "out" not in pick.injury_status.lower():
        out.append(f"🚨 Estado: {_h(pick.injury_status)}")

    # Never-miss signal
    if pick.min_l10 > pick.line:
        out.append(f"💎 No falló ni una vez en los últimos 10 (mín: {pick.min_l10})")

    return out


def _format_parlay(parlay: dict) -> list[str]:
    name   = parlay["name"]
    legs   = parlay["legs"]
    prob   = parlay.get("corr_joint_prob", parlay["hit_rate_product"])
    emoji  = PARLAY_EMOJI.get(name, "🎯")
    n_legs = len(legs)

    out = [f"{emoji} <b>{_h(name)}</b> ({n_legs} patas)"]

    for game_label, pick in legs:
        hit_pct  = pick.hit_count_l15 / pick.games_l15 * 100 if pick.games_l15 > 0 else 0.0
        conf_e   = CONFIDENCE_EMOJI.get(pick.confidence, "")
        out.append(
            f"  └─ {conf_e} <b>{_h(pick.player)}</b>"
            f" | Over {pick.line} {_h(pick.market)}"
            f"  <code>L15: {pick.hit_count_l15}/{pick.games_l15} ({hit_pct:.0f}%)</code>"
        )

    out.append(
        f"  📊 Prob. estimada: ~<b>{prob * 100:.0f}%</b>"
        f"  <i>(multiplicación de hit rates)</i>"
    )
    return out


def _format_escalera(escalera_data: dict) -> list[str]:
    player    = escalera_data["player"]
    stat_name = escalera_data["stat_name"]
    esc_lines = escalera_data["lines"]
    analysis  = escalera_data["analysis"]

    out = [
        "",
        "━" * 30,
        "🏆 <b>ESCALERA DEL DÍA</b> 🪜",
        "━" * 30,
        "",
        f"<b>{_h(player)} · escalera de {_h(stat_name)}</b>",
    ]
    for entry in esc_lines:
        line_val = entry["line"]
        decimal  = entry["decimal"]
        units    = entry["units"]
        odds_str = f"{decimal:.2f}" if decimal < 10 else f"{decimal:.1f}"
        out.append(f"Over {line_val} {_h(stat_name)} | {odds_str} ({units} unidades)")

    out.append("")
    out.append(f"<i>{_h(analysis)}</i>")
    out.append("")
    return out


def _h(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
