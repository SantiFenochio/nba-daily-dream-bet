# 🏀 NBA Daily Dream Bet

Bot de Telegram que analiza props de jugadores NBA con hit rate histórico, ajustes de contexto y backtesting automático. Envía picks diariamente via GitHub Actions.

---

## Cómo funciona

Cada día a las **14:00 hs Argentina (17:00 UTC)** el bot:

1. Obtiene los partidos del día (BallDontLie API)
2. Detecta equipos en back-to-back
3. Carga pace + DEF_RATING de los 30 equipos (SportsData.io)
4. Busca props en 12 mercados + spreads/totales (The Odds API)
5. Descarga los últimos 28 días de box scores de cada jugador (ESPN public API)
6. Consulta lesiones en tiempo real (ESPN + Rotowire fallback)
7. Analiza cada prop: hit rate L15/L10/L5, rachas, ajustes de contexto
8. Backteatea los picks del día anterior y calibra umbrales
9. Envía los mejores picks + parlays + escalera del día a Telegram

Si ningún pick supera el umbral de EV, entra en **modo fallback** y envía igual los mejores picks disponibles con una nota aclaratoria.

---

## Mercados analizados

| Mercado | Clave |
|---|---|
| Puntos | player_points |
| Rebotes | player_rebounds |
| Asistencias | player_assists |
| Triples | player_threes |
| Robos | player_steals |
| Tapas | player_blocks |
| PRA (Pts+Reb+Ast) | player_points_rebounds_assists |
| Puntos + Asistencias | player_points_assists |
| Puntos + Rebotes | player_points_rebounds |
| Rebotes + Asistencias | player_rebounds_assists |
| Tapas + Robos | player_blocks_steals |
| Pérdidas | player_turnovers |

---

## Modelo de análisis

### Estadísticas base por prop

Para cada jugador/línea se computan:
- **Hit rate L15** — en cuántos de los últimos 15 partidos superó la línea
- **Promedio L15 / L5** — para ver forma reciente vs histórico
- **Mínimo L10** — el piso de los últimos 10 partidos (consistencia)
- **Racha activa** — partidos consecutivos superando la línea

### Niveles de confianza

| Nivel | Criterio |
|---|---|
| ✅ Alta | Hit rate L15 ≥ 80% **y** promedio L15 ≥ línea × 1.10 |
| ⚡ Media | Hit rate L15 ≥ 67% **y** promedio L15 ≥ línea × 1.05 |
| ❄️ Baja | Por debajo de Media (solo incluidos si hay cupo) |

### Ajustes de contexto

| Factor | Efecto |
|---|---|
| Back-to-back | Baja confianza un nivel (Alta→Media, Media→Baja) |
| **Day-To-Day / Questionable** | **Baja confianza un nivel + penalty en score** |
| Pace del partido | Multiplicador dinámico vs promedio liga |
| DEF_RATING rival | Multiplicador dinámico en mercados ofensivos |
| Proyección SportsData.io | Multiplica si proyección supera/cae bajo la línea |
| **L5 vs L15 divergencia** | **Penaliza si forma reciente cayó vs histórico** |

### Filtros de calidad

- Mínimo 20 minutos promedio (filtra garbage time)
- Mínimo 5 juegos en el historial
- **Máximo 2 picks por jugador** (evita cascada si un jugador falla)
- Máximo 4 picks por partido, 15 en total
- EV mínimo: 2% (con fallback a 0% si ningún pick lo supera)

### Penalizaciones específicas por mercado

| Mercado | Regla |
|---|---|
| Robos Over ≥ 1.5 | L15 ≥ 73% **y** L5 ≥ 60% (stricter — alta varianza) |
| **Triples Over ≤ 1.0** | **Score × 0.88 — línea muy baja = señal débil** |

### Blowout risk

| Situación | Efecto |
|---|---|
| Bench player (<28 min avg) en partido con spread >12 | Score × 0.90 (general) |
| **Star player en spread >12 + mercado de asistencias** | **Score × 0.93 + baja confianza** |

> **Lección aprendida (10/04/2026):** Evan Mobley tenía racha de 10 seguidos en asistencias. El partido terminó en blowout (Hawks 124 - Cavaliers 102) y terminó con 1 asistencia. El game flow en blowouts destruye las asistencias incluso de estrellas.

### L5 divergence penalty

Si el hit rate reciente (L5) cayó significativamente vs el histórico (L15):

| Ratio L5/L15 | Efecto |
|---|---|
| < 0.60 (severe) | Score × 0.87 + baja confianza un nivel |
| < 0.75 (mild) | Score × 0.93 |

### Calibración por backtesting

El bot resuelve los picks del día anterior contra los box scores reales de ESPN y ajusta los umbrales de EV:
- Hit rate histórica ≥ 65% → umbral más permisivo (×0.80)
- Hit rate histórica ≤ 38% → umbral más estricto (×1.35)

---

## Parlays — reglas de elegibilidad

Los picks se excluyen de **todas las combinadas** si:
- El jugador está marcado como **Day-To-Day / Questionable** (riesgo de DNP)
- El jugador está en **Back-to-Back y confianza < Alta**

> **Lección aprendida (10/04/2026):** Stephon Castle (DTD) era una pata en las 4 combinadas del día. No jugó (DNP). Todas las combinadas se perdieron.

---

## Secciones del mensaje diario

1. **Picks por partido** — jugador, mercado, línea, stats L15/L5, precio, confianza
2. **Parlays del día** — 4 combinaciones armadas automáticamente:
   - La Segura (3 patas Alta)
   - El Balance (2 Alta + 1 Media)
   - La Arriesgada (4 patas)
   - Los Consistentes (nunca fallaron en L10)
3. **Escalera del día** — 3 líneas progresivas sobre el mejor jugador del día

---

## Estructura del proyecto

```
nba-daily-dream-bet/
├── main.py                        # Orquestador principal
├── modules/
│   ├── fetch_games.py             # Partidos del día (BallDontLie)
│   ├── fetch_props.py             # Props + spreads/totales (The Odds API)
│   ├── fetch_player_stats.py      # Box scores + lesiones (ESPN)
│   ├── fetch_context.py           # Pace + DEF_RATING (SportsData.io)
│   ├── fetch_projections.py       # Proyecciones de jugadores (SportsData.io)
│   ├── analyzer.py                # Motor de análisis (hit rate, EV, contexto)
│   ├── parlay_builder.py          # Construcción de parlays automáticos
│   ├── escalera.py                # Escalera del día (3 líneas progresivas)
│   ├── consistency_picks.py       # Picks de máxima consistencia histórica
│   ├── history.py                 # Backtesting + calibración de umbrales
│   ├── formatter.py               # Formateador HTML para Telegram
│   └── telegram_client.py         # Envío con splitting automático
├── agents/
│   ├── orchestrator.py            # Coordinador del pipeline multi-agent
│   ├── base_agent.py              # Clase base con tool use loop + retry
│   ├── system_prompts.py          # Prompts en español argentino
│   ├── subagent_data_validator.py
│   ├── subagent_projection.py     # Monte Carlo bootstrap + normal
│   ├── subagent_news_intelligence.py # ESPN/Rotoworld con tool_use
│   ├── subagent_ev_optimizer.py   # Cholesky MC para joint probs
│   ├── subagent_narrator.py       # Generador de mensaje Telegram
│   └── subagent_auto_calibrator.py
├── data/
│   ├── picks_history.json         # Historial de picks con resultados reales
│   └── injury_overrides.json      # Overrides manuales de lesiones por fecha
├── .github/workflows/
│   └── daily_nba_pick.yml         # GitHub Actions: 14:00 hs ARG (17:00 UTC)
└── requirements.txt
```

---

## APIs utilizadas

| API | Uso | Auth |
|---|---|---|
| [BallDontLie v1](https://www.balldontlie.io/) | Partidos del día | API Key |
| [The Odds API v4](https://the-odds-api.com/) | Props + spreads + totales | API Key |
| [ESPN public API](https://site.api.espn.com/) | Box scores + lesiones | Sin auth |
| [SportsData.io](https://sportsdata.io/) | Pace/DEF_RATING + proyecciones | API Key |
| [odds-api.io MCP](https://odds-api.io/) | Odds en tiempo real para análisis manual | API Key |
| [Rotowire](https://www.rotowire.com/) | Lesiones (fallback) | Sin auth |
| [Telegram Bot API](https://core.telegram.org/bots/api) | Envío de mensajes | Bot Token |

---

## Setup local

```bash
git clone https://github.com/SantiFenochio/nba-daily-dream-bet.git
cd nba-daily-dream-bet
pip install -r requirements.txt
```

Crear `.env` en la raíz:
```
BALLDONTLIE_API_KEY=tu_key
ODDS_API_KEY=tu_key
SPORTSDATA_API_KEY=tu_key
TELEGRAM_BOT_TOKEN=tu_token
TELEGRAM_CHAT_ID=tu_chat_id
ANTHROPIC_API_KEY=tu_key  # opcional — activa sistema multi-agent
```

Correr manualmente:
```bash
python main.py
```

---

## GitHub Actions (producción)

Secrets necesarios en `Settings → Secrets → Actions`:

| Secret | Descripción |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram |
| `TELEGRAM_CHAT_ID` | ID del chat/canal destino |
| `ODDS_API_KEY` | API key de The Odds API |
| `BALLDONTLIE_API_KEY` | API key de BallDontLie |
| `SPORTSDATA_API_KEY` | API key de SportsData.io |
| `ANTHROPIC_API_KEY` | API key de Anthropic (para el sistema multi-agent) |

> `ANTHROPIC_API_KEY` es opcional. Si no está seteada, el bot funciona con el formatter clásico.

---

## 🤖 Arquitectura Multi-Agent con Claude

El bot incorpora un sistema multi-agent impulsado por **Claude (Anthropic)** que refina el análisis cuantitativo con razonamiento cualitativo en tiempo real. Se activa automáticamente si `ANTHROPIC_API_KEY` está seteada.

### Principio: Claude refina, no reemplaza

El modelo cuantitativo (hit rates, EV, Monte Carlo, calibración histórica) sigue siendo el núcleo. Claude actúa como una **capa cualitativa** encima: interpreta noticias, detecta inconsistencias y explica los picks.

### Los 6 Subagents

```
main.py
  └── Orchestrator
        ├── [1] DataValidatorAgent      — Valida consistencia de picks y proyecciones
        ├── [2] NewsIntelligenceAgent   — Scraping ESPN/Rotoworld con tool_use
        ├── [3] ProjectionAgent         — Monte Carlo 1000 sims + ajuste cualitativo
        ├── [4] apply_refinements()     — Aplica factores News+MC a scores
        ├── [5] EVOptimizerAgent        — Cholesky MC (10K sims) para joint probs
        ├── [6] NarratorAgent           — Genera mensaje Telegram en español argentino
        └── [7] AutoCalibratorAgent     — Sugerencias de mejora al modelo
```

### Monte Carlo Cholesky para parlays

```python
ρ_same_team = 0.30   # Mismos compañeros → comparten pace/puntos del equipo
ρ_same_game = 0.08   # Mismo partido → comparten pace general
ρ_diff_game = 0.00   # Partidos distintos → independientes
```

### Degradación graceful

Si cualquier agente falla, el bot usa automáticamente `formatter.py` clásico. No hay punto de falla único.

---

## 📊 Historial de rendimiento

| Fecha | Picks válidos | ✅ | ❌ | % | Notas |
|---|---|---|---|---|---|
| 07/04/2026 | 3 | 2 | 1 | 67% | |
| 09/04/2026 | 13 | 7 | 6 | 54% | |
| 10/04/2026 | 11 | 6 | 5 | 55% | 4 DNP (Castle×3, Jokic) |
| 12/04/2026 | 8 | 5 | 3 | 63% | 1 DNP (Johnson rest) |

> Los DNP (jugadores que no jugaron) se contabilizan como void, no como miss.

---

## Changelog

### 2026-04-13

**Mejoras al modelo basadas en análisis de picks 10/04 y 12/04:**

- `analyzer.py` — **DTD/Questionable flag**: baja confianza un nivel + penalty en score para jugadores Day-To-Day
- `analyzer.py` — **Blowout risk extendido a asistencias de estrellas**: el spread >12 ahora penaliza mercados de asistencias incluso para titulares
- `analyzer.py` — **Per-player cap: máximo 2 picks por jugador** en el output final (evita cascada de fallos)
- `analyzer.py` — **L5 divergence penalty**: penaliza si la forma reciente (L5) cayó significativamente vs el histórico (L15)
- `analyzer.py` — **Triples low-line penalty**: Over ≤ 1.0 en triples recibe score × 0.88 por alta varianza
- `parlay_builder.py` — **DTD players excluidos de todas las combinadas** (`_is_parlay_eligible()`)
- `escalera.py` — **DTD penalty -5.0** en scoring de escalera (un DNP destruye todos los escalones)
- `formatter.py` — muestra `⚠️ DTD` explícito para jugadores Day-To-Day
- `consistency_picks.py` — recibe `injury_statuses` para filtrar OUTs
- `data/picks_history.json` — picks del 10/04 y 12/04 registrados con resultados reales y notas
