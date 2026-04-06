# 🏀 NBA Daily Dream Bet

Bot de Telegram que analiza props de jugadores NBA con EV real, probabilidad Poisson + Bayesiana y múltiples ajustes de contexto. Envía picks automáticamente una vez por día via GitHub Actions.

---

## Cómo funciona

Cada día a las **14:00 hs Argentina (17:00 UTC)** el bot:

1. Obtiene los partidos del día (BallDontLie API)
2. Detecta equipos en back-to-back
3. Carga pace + DEF_RATING de los 30 equipos (stats.nba.com)
4. Busca props + spreads + totales en 8 mercados (The Odds API)
5. Descarga los últimos 20 juegos de cada jugador (nba_api)
6. Consulta lesiones en tiempo real (ESPN public API)
7. Analiza cada prop con el motor de EV, Poisson y Bayes
8. Envía los mejores picks a Telegram con hora en horario ARG

Si ningún pick supera el umbral de EV, entra en **modo fallback** y envía igual los mejores picks disponibles del día con una nota aclaratoria.

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

### Proyección base — Decay exponencial
En lugar de promedios simples por ventana, cada partido tiene peso `0.85^i` (el más reciente pesa 1.00, hace 5 partidos pesa 0.52, hace 10 pesa 0.23). Esto da mucha más relevancia a la forma reciente.

### Ajustes multiplicativos sobre la proyección

| Factor | Efecto | Cuándo aplica |
|---|---|---|
| Back-to-back | −7% | Equipo jugó ayer |
| Días de descanso | +2.5% | 4+ días sin jugar (piernas frescas) |
| Óxido por pausa larga | −1% | 7+ días sin jugar |
| Pace del partido | Dinámico | Promedio pace ambos equipos vs liga |
| DEF_RATING rival | Dinámico | Defensa rival vs promedio liga |
| Split local/visitante | 15% peso | Mínimo 3 partidos en esa condición |
| Historial vs rival | 10% peso | Mínimo 2 partidos vs ese equipo |
| Riesgo de paliza | −9% | Favorito por 12+ pts (estrella puede salir en 4to) |
| Compañero ausente | +10% c/u | Titular confirmado Out (máx. 2 jugadores) |
| Riesgo de faltas | −5% | Jugador históricamente foul-prone |
| Tendencia de minutos | Dinámico | Rol expandiéndose o reduciéndose (L5 vs L10) |
| Hot streak | Hasta +15% | L5 supera L10 por 12%+ |
| Cold streak | Hasta −10% % | L5 bajo L10 por 12%+ |

### Detección de situaciones especiales

**Riesgo de faltas** — 3 señales desde los últimos 20 partidos (`PF` column, nba_api):
- Promedio ≥ 3.3 PF/partido (L10)
- 2+ foul-outs (6 faltas) en L20
- 3+ partidos con 4+ faltas y menos del 78% de sus minutos habituales

**Riesgo de paliza** — spread > 12 pts: el favorito puede no jugar el 4to cuarto

**Cascada de ausencias** — si un titular está confirmado Out, sus compañeros reciben boost de uso

**Hot/Cold form** — compara L5 vs L10 para detectar rachas de rendimiento positivas o negativas

**Tendencia de minutos** — si el entrenador le está dando más/menos cancha recientemente

### Probabilidad del modelo

- **50% Poisson** (`scipy.stats.poisson`) sobre la proyección ajustada
- **50% Bayesiano** (Laplace smoothing) sobre hit rate L10

### EV y stake

- **Devig de dos lados** (Over + Under) para calcular probabilidad justa del mercado sin vig
- **EV%** = (prob_modelo × ganancia) − (prob_rival × 100)
- **Kelly 1/4** calculado pero no mostrado en el mensaje (referencia interna)

### Filtros de calidad

- Mínimo 18 minutos promedio en L10 (filtra garbage time)
- Mínimo 5 juegos en el historial
- EV% mínimo: 2% (con fallback a 0% si ningún pick lo supera)
- Máximo 6 picks por partido, 20 en total

---

## Formato del mensaje

El mensaje de Telegram se divide en dos secciones:

**Sección 1 — Resumen rápido:** todos los picks del día en formato compacto
```
Cleveland Cavaliers vs Memphis Grizzlies  🕐 21:00 hs (ARG)
────────────────────────────
🔥 Nikola Jokic — PRA OVER 56.5
⚡ Jamal Murray — Puntos OVER 22.5
```

**Sección 2 — Análisis detallado:** stats, EV, proyección y todos los flags contextuales de cada pick

Si el mensaje supera el límite de Telegram (4096 caracteres) se divide automáticamente en partes numeradas.

---

## Niveles de confianza

| Nivel | Criterio |
|---|---|
| 🔥 Alta | EV ≥ 10% y hit rate L10 ≥ 65% |
| ⚡ Media | EV ≥ 5% y hit rate L10 ≥ 55% |
| ❄️ Baja | EV ≥ 2% |
| 🎲 Riesgosa | Fallback mode (por debajo del umbral normal) |

---

## Estructura del proyecto

```
nba-daily-dream-bet/
├── main.py                        # Orquestador principal
├── modules/
│   ├── fetch_games.py             # Partidos del día (BallDontLie)
│   ├── fetch_props.py             # Props + spreads/totales (The Odds API)
│   ├── fetch_player_stats.py      # Historial + lesiones (nba_api + ESPN)
│   ├── fetch_context.py           # Pace + DEF_RATING (stats.nba.com)
│   ├── analyzer.py                # Motor de análisis (EV, Poisson, Bayes, decay)
│   ├── formatter.py               # Formateador HTML en dos secciones para Telegram
│   └── telegram_client.py         # Envío con splitting automático
├── .github/workflows/
│   └── daily_nba_pick.yml         # GitHub Actions: 14:00 hs ARG (17:00 UTC)
└── requirements.txt
```

---

## APIs utilizadas

| API | Uso | Auth |
|---|---|---|
| [BallDontLie v1](https://www.balldontlie.io/) | Partidos del día | API Key |
| [The Odds API v4](https://the-odds-api.com/) | Props + spreads + totales | API Key (recomendado plan pago) |
| [nba_api](https://github.com/swar/nba_api) | Historial de jugadores | Sin auth |
| [ESPN public API](https://site.api.espn.com/) | Lesiones en tiempo real | Sin auth |
| [Telegram Bot API](https://core.telegram.org/bots/api) | Envío de mensajes | Bot Token |

> **Nota:** The Odds API en plan gratuito tiene 500 requests/mes. Con 1 corrida diaria se consumen ~150/mes pero en desarrollo se agotan rápido. Se recomienda el plan pago para uso en producción continuo.

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

Para actualizar la API key de The Odds API: editar `.env` (local) y el secret `ODDS_API_KEY` en GitHub. No hay que tocar código.

---

## Manejo de playoffs

A partir del 18 de abril el bot detecta automáticamente la fase de playoffs y usa esos game logs. Si un jugador tiene menos de 5 partidos de playoffs, complementa con datos de la temporada regular automáticamente.
