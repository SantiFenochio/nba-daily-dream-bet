---
description: Ejecuta el bot NBA Daily Dream Bet y muestra el análisis del día. Usa /run-bot para correr el pipeline completo, o /run-bot preview para ver el mensaje sin enviarlo a Telegram.
allowed-tools: Bash Read
---

Ejecutá el bot NBA Daily Dream Bet según el argumento recibido: "$ARGUMENTS"

## Si el argumento es "preview" o está vacío y querés modo preview:

Corré este comando para generar el análisis sin enviar a Telegram:

```bash
cd /home/user/nba-daily-dream-bet && python -c "
import asyncio
from dotenv import load_dotenv
from modules.fetch_games import get_today_games
from modules.fetch_stats import get_team_stats
from modules.fetch_props import get_player_props, get_game_odds
from modules.fetch_schedule import get_rest_days
from modules.analyzer import analyze_games
from modules.formatter import format_message
load_dotenv()
async def preview():
    games = get_today_games()
    if not games:
        print('No hay partidos hoy.')
        return
    stats = get_team_stats(games)
    props = get_player_props(games)
    game_odds = get_game_odds(games)
    rest_info = get_rest_days(games)
    picks = analyze_games(games, stats, props, game_odds, rest_info)
    print(format_message(picks))
asyncio.run(preview())
"
```

Mostrá el output completo al usuario.

## Si el argumento es "send" o "full":

Corré el bot completo incluyendo el envío a Telegram:

```bash
cd /home/user/nba-daily-dream-bet && python main.py
```

Mostrá el output al usuario y confirmá si el mensaje fue enviado correctamente.

## Si el argumento es "check" o "test":

Verificá que el entorno está configurado correctamente:

```bash
cd /home/user/nba-daily-dream-bet && python -c "
import os
from dotenv import load_dotenv
load_dotenv()
keys = {
    'TELEGRAM_BOT_TOKEN': os.getenv('TELEGRAM_BOT_TOKEN'),
    'TELEGRAM_CHAT_ID': os.getenv('TELEGRAM_CHAT_ID'),
    'ODDS_API_KEY': os.getenv('ODDS_API_KEY'),
}
for k, v in keys.items():
    status = '✅ configurada' if v and v != f'your_{k.lower()}_here' else '❌ falta o es placeholder'
    print(f'{k}: {status}')
"
```

Y verificá que las dependencias están instaladas:

```bash
cd /home/user/nba-daily-dream-bet && pip show requests python-telegram-bot python-dotenv aiohttp 2>&1 | grep -E "^(Name|Version|WARNING)"
```

Mostrá un resumen del estado del entorno.

## Default (sin argumento o argumento desconocido):

Tratalo como "preview".
