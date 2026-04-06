# 🏀 NBA Daily Dream Bet

Bot de Telegram que analiza props de jugadores NBA con EV real, probabilidad Poisson + Bayesiana, y ajustes de contexto. Envía picks automáticamente dos veces por día via GitHub Actions.

---

## Cómo funciona

Cada día a las **9 AM ET** y **5 PM ET** el bot:

1. Obtiene los partidos del día (BallDontLie API)
2. Detecta equipos en back-to-back
3. Carga pace + DEF_RATING de los 30 equipos (stats.nba.com)
4. Busca props en 8 mercados (The Odds API)
5. Descarga los últimos 20 juegos de cada jugador (nba_api)
6. Consulta lesiones (ESPN public API)
7. Analiza cada prop y calcula EV, Kelly, Poisson, Bayes
8. Envía los mejores picks a Telegram con hora en horario ARG

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
| Pérdidas | player_turnovers |

---

## Modelo de análisis

**Proyección ajustada:**
- Base: 40% últimos 5 juegos + 35% últimos 10 + 25% últimos 20
- Ajustes: pace del partido, DEF_RATING rival, split local/visitante, historial vs ese rival
- Penalización back-to-back: −7%

**Probabilidad del modelo:**
- 50% Poisson (`scipy.stats.poisson`) sobre la proyección ajustada
- 50% Bayesiano (Laplace smoothing) sobre hit rate L10

**EV y stake:**
- Devig de dos lados (Over + Under) para probabilidad justa del mercado
- EV% = (prob_modelo × ganancia) − (prob_rival × 100)
- Stake: Kelly 1/4, capeado al 5% del bankroll

**Filtros:**
- Mínimo 18 minutos promedio (últimos 10 juegos)
- Mínimo 5 juegos en el historial
- EV% mínimo: 2%
- Máximo 6 picks por partido, 20 en total

---

## Niveles de confianza

| Nivel | Criterio |
|---|---|
| 🔥 Alta | EV ≥ 10% y hit rate L10 ≥ 65% |
| ⚡ Media | EV ≥ 5% y hit rate L10 ≥ 55% |
| ❄️ Baja | EV ≥ 2% |
| 🎲 Riesgosa | Por debajo del umbral mínimo |

---

## Estructura del proyecto

```
nba-daily-dream-bet/
├── main.py                        # Orquestador principal
├── modules/
│   ├── fetch_games.py             # Partidos del día (BallDontLie)
│   ├── fetch_props.py             # Props de jugadores (The Odds API)
│   ├── fetch_player_stats.py      # Historial + lesiones (nba_api + ESPN)
│   ├── fetch_context.py           # Pace + DEF_RATING (stats.nba.com)
│   ├── analyzer.py                # Motor de análisis (EV, Poisson, Bayes)
│   ├── formatter.py               # Formateador HTML para Telegram
│   └── telegram_client.py        # Envío con splitting automático
├── .github/workflows/
│   └── daily_nba_pick.yml         # GitHub Actions: 9 AM + 5 PM ET diarios
└── requirements.txt
```

---

## APIs utilizadas

| API | Uso | Auth |
|---|---|---|
| [BallDontLie v1](https://www.balldontlie.io/) | Partidos del día | API Key |
| [The Odds API v4](https://the-odds-api.com/) | Props + cuotas | API Key |
| [nba_api](https://github.com/swar/nba_api) | Historial de jugadores | Sin auth |
| [ESPN public API](https://site.api.espn.com/) | Lesiones en tiempo real | Sin auth |
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
TELEGRAM_BOT_TOKEN=tu_token
TELEGRAM_CHAT_ID=tu_chat_id
```

Correr manualmente:
```bash
python main.py
```

---

## GitHub Actions (producción)

Los secrets necesarios en `Settings → Secrets → Actions`:

| Secret | Descripción |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram |
| `TELEGRAM_CHAT_ID` | ID del chat/canal destino |
| `ODDS_API_KEY` | API key de The Odds API |
| `BALLDONTLIE_KEY` | API key de BallDontLie |

---

## Manejo de playoffs

A partir del 18 de abril el bot detecta automáticamente la fase de playoffs y usa esos game logs. Si un jugador tiene menos de 5 partidos de playoffs, complementa con datos de la temporada regular.
