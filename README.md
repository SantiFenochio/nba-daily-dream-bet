# 🏀 NBA Daily Dream Bet

Bot automatizado que analiza los partidos de NBA del día, genera picks con razonamiento estadístico y los envía a un grupo de Telegram. Corre diariamente vía GitHub Actions.

---

## ¿Qué hace?

Cada día a las 14:00 UTC (10:00 AM ET):

1. Fetchea los partidos de NBA del día (Ball Don't Lie API)
2. Obtiene promedios de temporada de cada equipo (pts, fg%, blk, stl)
3. Detecta si algún equipo juega en back-to-back consultando el calendario reciente
4. Obtiene spread, total y moneylines del mercado (The Odds API)
5. Analiza cada partido con el modelo de scoring propio
6. Genera picks con nivel de confianza calibrado contra la probabilidad implícita del libro
7. Envía el mensaje formateado al grupo de Telegram

---

## Modelo de análisis

### Fórmula de scoring

```
score = pts_adj * 0.5 + fg_pct * 100 * 0.3 + (blk + stl) * 0.5 * 0.2
```

Donde `pts_adj` aplica una penalización del **4%** si el equipo juega en back-to-back.

**Modificadores adicionales:**
- **Ventaja de local:** +2.5 pts al equipo de casa
- **Back-to-back fatigue:** -4% en scoring (scoring cae 3-5% en segundo partido consecutivo)

### Confianza calibrada con el mercado

El modelo convierte el margen predicho a probabilidad de victoria via sigmoid logístico y la compara contra la probabilidad implícita del libro (sin vig):

| Edge vs. libro | Confianza |
|---|---|
| ≥ 8% | 🔥 Alta |
| ≥ 4% | ⚡ Media |
| < 4% | ❄️ Baja |
| Libro más confiado | ❄️ Baja |

### Recomendación de totales

Cuando uno o ambos equipos juegan en back-to-back, el bot recomienda automáticamente **Under** en el total de puntos del partido. Basado en evidencia documentada: el scoring baja entre 3 y 10 puntos cuando hay equipos fatigados.

---

## Ejemplo de mensaje

```
🏀 NBA DAILY DREAM BET — 04/04/2026
────────────────────────────────

Partido 1: Golden State Warriors @ Los Angeles Lakers
📌 Apuesta: Los Angeles Lakers gana como local
📊 Análisis: LAL promedia 118.0 pts (47.3% FG, def. 13.3 blk+stl) vs 115.0 pts
   (46.1% FG) de GSW. Ventaja local aplicada (+2.5 pts). ⚠️ GSW en B2B.
   Descanso: local 2d / visitante 1d.
🔥 Confianza: Alta (ventaja vs libro: 11.9%)
📉 Spread mercado: -3.5 (local)
🔢 Totales: Under 224.5 — GSW en B2B → tendencia al Under por fatiga.
⚠️ Alerta fatiga: ✈️ Visitante en B2B
🎯 Props destacados:
  • LeBron James — Puntos Over 25.5 (-115)

_Análisis generado automáticamente. Apostá con responsabilidad._
```

---

## Setup

### 1. Clonar el repo

```bash
git clone https://github.com/SantiFenochio/nba-daily-dream-bet.git
cd nba-daily-dream-bet
```

### 2. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 3. Configurar variables de entorno

Copiá el archivo de ejemplo y completá tus credenciales:

```bash
cp .env.example .env
```

| Variable | Descripción | Requerida |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram ([@BotFather](https://t.me/BotFather)) | ✅ |
| `TELEGRAM_CHAT_ID` | ID del grupo/canal de Telegram | ✅ |
| `ODDS_API_KEY` | API key de [The Odds API](https://the-odds-api.com) | Opcional* |

> *Sin `ODDS_API_KEY` el bot funciona pero sin odds del mercado (spread, totales, moneylines). La confianza cae al modo basado en márgenes.

### 4. Ejecutar

```bash
python main.py
```

---

## Automatización con GitHub Actions

El workflow `.github/workflows/daily_nba_pick.yml` corre automáticamente todos los días a las **14:00 UTC**. También se puede disparar manualmente desde la pestaña Actions.

**Configurar los secrets en GitHub:**

`Settings → Secrets and variables → Actions → New repository secret`

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `ODDS_API_KEY`

---

## Slash command `/run-bot`

Si usás Claude Code, podés correr el bot directamente con:

| Comando | Acción |
|---|---|
| `/run-bot` o `/run-bot preview` | Genera el análisis y lo muestra **sin** enviar a Telegram |
| `/run-bot send` | Corre el pipeline completo con envío |
| `/run-bot check` | Verifica que las env vars y dependencias estén configuradas |

---

## Estructura del proyecto

```
nba-daily-dream-bet/
├── main.py                        # Orquestador principal
├── requirements.txt
├── .env.example
├── .github/
│   └── workflows/
│       └── daily_nba_pick.yml     # GitHub Actions (cron diario 14:00 UTC)
├── .claude/
│   └── skills/
│       └── run-bot/
│           └── SKILL.md           # Slash command /run-bot
└── modules/
    ├── fetch_games.py             # Partidos del día (Ball Don't Lie)
    ├── fetch_stats.py             # Promedios de temporada por equipo
    ├── fetch_schedule.py          # Días de descanso y back-to-back detection
    ├── fetch_props.py             # Odds del mercado y player props (The Odds API)
    ├── analyzer.py                # Modelo de scoring y generación de picks
    ├── formatter.py               # Formato del mensaje de Telegram
    └── telegram_client.py         # Envío del mensaje
```

---

## APIs utilizadas

| API | Uso | Tier requerido |
|---|---|---|
| [Ball Don't Lie](https://www.balldontlie.io) | Partidos, stats de equipos, calendario | Free |
| [The Odds API](https://the-odds-api.com) | Spread, totales, moneylines, player props | Free (500 req/mes) |
| Telegram Bot API | Envío de mensajes | Free |

---

## Stack

- **Python 3.11**
- `requests` — llamadas HTTP
- `python-telegram-bot` — cliente de Telegram async
- `python-dotenv` — manejo de variables de entorno
- `aiohttp` — HTTP async

---

> Análisis generado automáticamente. Apostá con responsabilidad.
