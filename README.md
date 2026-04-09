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

### Ajustes de contexto

| Factor | Efecto |
|---|---|
| Back-to-back | Baja confianza un nivel (Alta→Media, Media→Baja) |
| Pace del partido | Multiplicador dinámico vs promedio liga |
| DEF_RATING rival | Multiplicador dinámico en mercados ofensivos |
| Proyección SportsData.io | Multiplica si proyección supera/cae bajo la línea |

### Niveles de confianza

| Nivel | Criterio |
|---|---|
| ✅ Alta | Hit rate L15 ≥ 80% **y** promedio L15 ≥ línea × 1.10 |
| ⚡ Media | Hit rate L15 ≥ 67% **y** promedio L15 ≥ línea × 1.05 |
| ❄️ Baja | Por debajo de Media (solo incluidos si hay cupo) |

### Filtros de calidad

- Mínimo 20 minutos promedio (filtra garbage time)
- Mínimo 5 juegos en el historial
- Máximo 4 picks por partido, 15 en total
- EV mínimo: 2% (con fallback a 0% si ningún pick lo supera)

### Calibración por backtesting

El bot resuelve los picks del día anterior contra los box scores reales de ESPN y ajusta los umbrales de EV:
- Hit rate histórica ≥ 65% → umbral más permisivo (×0.80)
- Hit rate histórica ≤ 38% → umbral más estricto (×1.35)

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
├── data/
│   ├── picks_history.json         # Historial de picks con resultados
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
| [Rotowire](https://www.rotowire.com/) | Lesiones (fallback) | Sin auth |
| [Telegram Bot API](https://core.telegram.org/bots/api) | Envío de mensajes | Bot Token |

> **Nota:** The Odds API en plan gratuito tiene 500 requests/mes. Con 1 corrida diaria se consumen ~6 requests/día (1 por partido). Se recomienda el plan pago para uso en producción continuo.

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
