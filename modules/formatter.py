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
    fallback_mode: bool = False,
    parlays: list[dict] | None = None,
    accuracy: dict | None = None,
) -> str:
    today = datetime.now(ET).strftime("%d/%m/%Y")
    gt = game_times or {}

    if not picks_by_game:
        return f"🏀 <b>NBA DAILY DREAM BET — {today}</b>\n\nNo hay picks disponibles hoy."

    total_picks = sum(len(v) for v in picks_by_game.values())

    # ── SECCIÓN 1: Resumen compacto ──────────────────────────────────────────
    if fallback_mode:
        subtitle = f"<i>{total_picks} mejores picks del día — mercado ajustado (EV bajo umbral normal)</i>"
    else:
        subtitle = f"<i>{total_picks} picks con EV positivo en {len(picks_by_game)} partido(s)</i>"

    summary_lines = [
        f"🏀 <b>NBA DAILY DREAM BET — {today}</b>",
        subtitle,
    ]

    # Yesterday's performance line
    if accuracy:
        yday = accuracy.get("yesterday")
        overall = accuracy.get("overall")
        if yday and yday["total"] > 0:
            pct = round(yday["rate"] * 100)
            emoji = "✅" if pct >= 60 else "⚠️" if pct >= 40 else "❌"
            summary_lines.append(
                f"{emoji} <i>Ayer: {yday['hits']}/{yday['total']} picks ({pct}%)"
                + (f" | Histórico: {round(overall['rate']*100)}% ({overall['total']} picks)" if overall and overall["total"] >= 10 else "")
                + "</i>"
            )
        elif overall and overall["total"] >= 10:
            summary_lines.append(
                f"<i>Histórico: {round(overall['rate']*100)}% de acierto ({overall['total']} picks resueltos)</i>"
            )

    summary_lines.append("")

    for game_label, picks in picks_by_game.items():
        hora = gt.get(game_label)
        display_label = game_label.replace(" @ ", " vs ")
        header = f"<b>{_h(display_label)}</b>"
        if hora:
            header += f"  🕐 {hora}"
        summary_lines.append(header)
        summary_lines.append("─" * 28)

        for pick in picks:
            emoji = CONFIDENCE_EMOJI.get(pick.confidence, "")
            side_upper = pick.side.upper()
            summary_lines.append(
                f"{emoji} <b>{_h(pick.player)}</b> — {_h(pick.market)} {side_upper} {pick.line}"
            )

        summary_lines.append("")

    # ── SECCIÓN 2: Análisis detallado ────────────────────────────────────────
    detail_lines = [
        "━" * 30,
        "📋 <b>ANÁLISIS DETALLADO</b>",
        "━" * 30,
        "",
    ]

    for game_label, picks in picks_by_game.items():
        for pick in picks:
            detail_lines += _format_pick_detail(pick)
            detail_lines.append("")

    detail_lines.append(
        "<i>EV calculado via devig. Probabilidad: Poisson + Bayes. "
        "Apostá con responsabilidad.</i>"
    )

    # ── SECCIÓN 3: Combinadas recomendadas ────────────────────────────────────
    # Ordenadas por prob. conjunta realista (corr_joint_prob) si está disponible,
    # con fallback a hit_rate_product para compatibilidad con versiones anteriores.
    parlay_lines: list[str] = []
    if parlays:
        sorted_parlays = sorted(
            parlays,
            key=lambda p: p.get("corr_joint_prob", p["hit_rate_product"]),
            reverse=True,
        )
        parlay_lines += [
            "",
            "━" * 30,
            "🎰 <b>COMBINADAS RECOMENDADAS</b>",
            "━" * 30,
            "<i>Prob. con correlaciones (Monte Carlo) | 1 pick/partido</i>",
            "",
        ]
        for i, parlay in enumerate(sorted_parlays, 1):
            name   = parlay["name"]
            legs   = parlay["legs"]
            n_legs = len(legs)

            # Preferir probabilidad realista; fallback a naïve si el builder es v1
            corr_prob  = parlay.get("corr_joint_prob", parlay["hit_rate_product"])
            naive_prob = parlay["hit_rate_product"]
            ev_pct     = parlay.get("parlay_ev_pct")

            # Línea de cabecera con prob. realista + EV si disponible
            ev_str = ""
            if ev_pct is not None:
                ev_sign = "+" if ev_pct >= 0 else ""
                ev_str  = f" | EV: <b>{ev_sign}{ev_pct:.1f}%</b>"

            parlay_lines.append(
                f"<b>#{i} {_h(name)}</b> ({n_legs} patas){ev_str}"
            )
            # Prob. conjunta realista en línea propia (más legible en Telegram)
            parlay_lines.append(
                f"  📊 Prob. conjunta: <b>{corr_prob * 100:.1f}%</b>"
                + (
                    f"  <i>(naïve: {naive_prob * 100:.0f}%)</i>"
                    if abs(corr_prob - naive_prob) >= 0.005  # solo mostrar si hay diferencia >0.5pp
                    else ""
                )
            )

            for game_label, pick in legs:
                emoji   = CONFIDENCE_EMOJI.get(pick.confidence, "")
                hit_str = f"{pick.hit_count_l10}/{pick.games_l10}"
                parlay_lines.append(
                    f"  {emoji} <b>{_h(pick.player)}</b> — {_h(pick.market)} {pick.side.upper()} {pick.line}"
                    f"  <code>[{hit_str} L10]</code>"
                )
            parlay_lines.append("")

        parlay_lines.append(
            "<i>⚠️ Las combinadas multiplican riesgos. Apostá montos pequeños.</i>"
        )

    return "\n".join(summary_lines) + "\n" + "\n".join(detail_lines) + "\n".join(parlay_lines)


def _format_pick_detail(pick: PlayerPick) -> list[str]:
    """Full analysis block for a single pick — shown in the detail section."""
    emoji = CONFIDENCE_EMOJI.get(pick.confidence, "")
    side_upper = pick.side.upper()
    price_str = f" ({pick.price:+d})" if pick.price else ""

    lines = [
        f"{emoji} <b>{_h(pick.player)} — {_h(pick.market)} {side_upper} {pick.line}</b>{price_str}",
    ]

    # Stats line
    lines.append(
        f"  <code>L5: {pick.avg_l5:.1f} | L10: {pick.avg_l10:.1f} | L20: {pick.avg_l20:.1f}"
        f" | Hit L10: {pick.hit_count_l10}/{pick.games_l10} ({pick.hit_count_l10/pick.games_l10*100:.0f}%)</code>"
    )

    # EV + probability
    ev_bar = _ev_bar(pick.ev_pct)
    lines.append(
        f"  📊 <b>EV: +{pick.ev_pct:.1f}%</b> {ev_bar} | "
        f"Prob. modelo: {pick.model_prob*100:.0f}% | "
        f"Mercado justo: {pick.fair_prob*100:.0f}%"
    )

    # Projection
    sign = "+" if pick.edge >= 0 else ""
    lines.append(f"  🎯 Proyección: <b>{pick.projection}</b> ({sign}{pick.edge} vs línea {pick.line})")

    # Streak
    if pick.consecutive_streak >= 3:
        lines.append(
            f"  🔁 Racha: {pick.consecutive_streak} partidos consecutivos {pick.side}"
        )

    # Hot / cold form
    if pick.is_hot and pick.avg_l10 > 0:
        lines.append(
            f"  🔥 <b>En racha:</b> L5 {pick.avg_l5:.1f} vs L10 {pick.avg_l10:.1f}"
            f" (+{((pick.avg_l5/pick.avg_l10-1)*100):.0f}% sobre su media)"
        )
    elif pick.is_cold and pick.avg_l10 > 0:
        lines.append(
            f"  🥶 <b>Racha fría:</b> L5 {pick.avg_l5:.1f} vs L10 {pick.avg_l10:.1f}"
            f" (−{((1-pick.avg_l5/pick.avg_l10)*100):.0f}% bajo su media)"
        )

    # Minutes trend
    if abs(pick.minutes_trend_pct) >= 10.0:
        if pick.minutes_trend_pct > 0:
            lines.append(f"  📈 Rol en expansión: +{pick.minutes_trend_pct:.0f}% minutos (L5 vs L10)")
        else:
            lines.append(f"  📉 Rol en reducción: {pick.minutes_trend_pct:.0f}% minutos (L5 vs L10)")

    # Rest days
    if not pick.is_b2b and pick.rest_days >= 7:
        lines.append(f"  🦺 {pick.rest_days} días sin jugar — posible óxido")
    elif not pick.is_b2b and pick.rest_days >= 4:
        lines.append(f"  😴 Descansado: {pick.rest_days} días de descanso (+2.5% proy.)")

    # Context flags
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

    # Blowout risk
    if pick.blowout_risk:
        lines.append("  ⚡ <b>Riesgo paliza:</b> favorito 12+ pts — podría salir en el 4to")

    # Teammate absence
    if pick.absence_boost > 1.0:
        pct = round((pick.absence_boost - 1.0) * 100)
        lines.append(f"  📈 Compañero ausente — uso proyectado +{pct}%")

    # Rotation risk (irregular minutes)
    if pick.rotation_risk:
        lines.append(f"  🔄 <b>Rotación irregular:</b> varianza ±{pick.min_std:.0f} min (−7% proy.)")

    # Foul trouble
    if pick.foul_risk:
        foul_parts = [f"{pick.avg_fouls:.1f} PF/j (prom. L10)"]
        if pick.foul_out_count >= 2:
            foul_parts.append(f"{pick.foul_out_count} foul-outs en últ. 20j")
        if pick.foul_trouble_count >= 3:
            foul_parts.append(f"salió temprano por faltas {pick.foul_trouble_count}x")
        lines.append(f"  🟡 <b>Riesgo faltas:</b> {' | '.join(foul_parts)}")

    # Injury
    if pick.injury_status:
        lines.append(f"  🚨 <b>Lesión:</b> {_h(pick.injury_status)}")

    # B2B
    if pick.is_b2b:
        lines.append("  ⚠️ Back-to-back hoy (−7% proyectado)")

    # Confidence
    lines.append(f"  {emoji} Confianza: <b>{pick.confidence}</b>")

    return lines


def _ev_bar(ev_pct: float) -> str:
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
