"""
subagent_news_intelligence.py — Inteligencia de noticias de última hora.

Busca noticias recientes (ESPN, Rotoworld) para los jugadores del día
y ajusta las proyecciones en función del buzz detectado.

Usa tool_use de Anthropic para que Claude decida qué jugadores buscar.

Output:
  {
    "adjustments": {
      "player_name": {"factor": float, "reason": str, "source": str}
    },
    "news_items": [{"player": str, "headline": str, "impact": str}],
    "summary": str,   # resumen en español para el mensaje Telegram
  }
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import requests
from bs4 import BeautifulSoup

from agents.base_agent import BaseAgent

if TYPE_CHECKING:
    from modules.analyzer import PlayerPick

logger = logging.getLogger(__name__)

_ESPN_NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news"
_ESPN_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NBABot/1.0)"}
_REQUEST_TIMEOUT = 8

# Tool definitions para Claude
_NEWS_TOOLS = [
    {
        "name": "fetch_espn_nba_news",
        "description": (
            "Obtiene las últimas noticias NBA de ESPN (últimas 24h). "
            "Retorna titulares y descripciones para filtrar por jugadores relevantes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Cantidad de noticias a traer (max 25)",
                    "default": 20,
                }
            },
            "required": [],
        },
    },
    {
        "name": "fetch_rotoworld_player_news",
        "description": (
            "Busca noticias específicas de un jugador en Rotoworld (NBC Sports). "
            "Ideal para updates de lesiones, minutos proyectados, coach comments."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_name": {
                    "type": "string",
                    "description": "Nombre completo del jugador (ej: 'LeBron James')",
                }
            },
            "required": ["player_name"],
        },
    },
    {
        "name": "fetch_espn_injury_report",
        "description": "Obtiene el injury report oficial de la NBA desde ESPN para el día de hoy.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


class NewsIntelligenceAgent(BaseAgent):
    """Subagent 3: Inteligencia de noticias con tool use."""

    def __init__(self) -> None:
        super().__init__("NewsIntelligence", model="claude-haiku-4-5-20251001")

    # ── Scrapers (tool handlers) ───────────────────────────────────────────────

    def _fetch_espn_nba_news(self, limit: int = 20) -> str:
        """Fetches latest NBA news from ESPN public API."""
        try:
            resp = requests.get(
                _ESPN_NEWS_URL,
                params={"limit": min(limit, 25), "sport": "basketball", "league": "nba"},
                headers=_ESPN_HEADERS,
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            articles = data.get("articles", [])
            items = []
            for a in articles[:limit]:
                items.append({
                    "headline": a.get("headline", ""),
                    "description": a.get("description", "")[:200],
                    "published": a.get("published", "")[:10],
                })
            return json.dumps(items, ensure_ascii=False)
        except Exception as exc:
            logger.warning("[NewsIntelligence] ESPN news fetch failed: %s", exc)
            return f"Error fetching ESPN news: {exc}"

    def _fetch_rotoworld_player_news(self, player_name: str) -> str:
        """Scrapes Rotoworld (NBC Sports) for player-specific news."""
        try:
            # Normalize name for URL
            slug = player_name.lower().replace(" ", "-").replace("'", "").replace(".", "")
            url = f"https://www.rotowire.com/basketball/player.php?id={slug}"
            # Fallback: search via Rotowire search API
            search_url = "https://www.rotowire.com/basketball/news.php"
            resp = requests.get(
                search_url,
                params={"player": player_name},
                headers=_ESPN_HEADERS,
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # Extract news items
            news_items = []
            # Look for news article blocks
            for item in soup.select(".news-update, .player-news-item, .news-item")[:5]:
                text = item.get_text(strip=True)[:300]
                if player_name.split()[0].lower() in text.lower() or \
                   player_name.split()[-1].lower() in text.lower():
                    news_items.append(text)

            if not news_items:
                # Try generic scrape for any mention
                paragraphs = soup.find_all("p")
                for p in paragraphs[:20]:
                    text = p.get_text(strip=True)
                    last = player_name.split()[-1].lower()
                    if last in text.lower() and len(text) > 30:
                        news_items.append(text[:300])
                        if len(news_items) >= 3:
                            break

            if news_items:
                return json.dumps({"player": player_name, "news": news_items}, ensure_ascii=False)
            return f"Sin noticias recientes encontradas para {player_name}"

        except Exception as exc:
            logger.warning("[NewsIntelligence] Rotoworld fetch failed for %s: %s", player_name, exc)
            return f"Error fetching news for {player_name}: {exc}"

    def _fetch_espn_injury_report(self) -> str:
        """Fetches NBA injury report from ESPN."""
        try:
            url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
            resp = requests.get(url, headers=_ESPN_HEADERS, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            teams = data.get("injuries", [])
            injuries = []
            for team in teams:
                team_name = team.get("team", {}).get("displayName", "")
                for player in team.get("injuries", [])[:3]:
                    injuries.append({
                        "team": team_name,
                        "player": player.get("athlete", {}).get("displayName", ""),
                        "status": player.get("status", ""),
                        "detail": player.get("shortComment", "")[:100],
                    })
            return json.dumps(injuries[:30], ensure_ascii=False)
        except Exception as exc:
            logger.warning("[NewsIntelligence] ESPN injury report failed: %s", exc)
            return f"Error fetching injury report: {exc}"

    # ── Tool dispatcher ───────────────────────────────────────────────────────

    def _tool_handler(self, tool_name: str, tool_input: dict) -> Any:
        if tool_name == "fetch_espn_nba_news":
            return self._fetch_espn_nba_news(tool_input.get("limit", 20))
        if tool_name == "fetch_rotoworld_player_news":
            return self._fetch_rotoworld_player_news(tool_input["player_name"])
        if tool_name == "fetch_espn_injury_report":
            return self._fetch_espn_injury_report()
        return f"Tool desconocida: {tool_name}"

    # ── Main entry point ──────────────────────────────────────────────────────

    def gather(
        self,
        picks_by_game: dict[str, list["PlayerPick"]],
        date_str: str,
    ) -> dict:
        """
        Busca noticias de última hora para los jugadores del día.

        Args:
            picks_by_game: Picks del analyzer.
            date_str:      Fecha del análisis (YYYY-MM-DD).

        Returns:
            Dict con adjustments, news_items, summary.
        """
        all_picks: list["PlayerPick"] = [
            p for picks in picks_by_game.values() for p in picks
        ]

        if not all_picks:
            return {"adjustments": {}, "news_items": [], "summary": "Sin picks para buscar noticias."}

        # Jugadores únicos con picks Alta/Media (priorizar los más importantes)
        priority_players = list({
            p.player for p in all_picks
            if p.confidence in ("Alta", "Media")
        })[:12]

        all_players = list({p.player for p in all_picks})

        prompt = f"""Sos el agente de inteligencia de noticias NBA. Hoy es {date_str}.

Tenemos picks para estos jugadores hoy:
- PRIORIDAD (Alta/Media confidence): {", ".join(priority_players)}
- Todos los jugadores: {", ".join(all_players[:20])}

INSTRUCCIONES:
1. Primero, buscá noticias generales NBA (fetch_espn_nba_news) para ver qué hay relevante
2. Buscá el injury report ESPN (fetch_espn_injury_report) para chequear estados
3. Para jugadores con señales de alerta, buscá noticias específicas (fetch_rotoworld_player_news)
4. Priorizá buscar info de los jugadores de PRIORIDAD

Después de buscar, devolvé SOLO JSON válido:
{{
  "adjustments": {{
    "Nombre Jugador": {{
      "factor": 1.05,
      "reason": "Regresó de lesión, full practice ayer",
      "source": "ESPN"
    }}
  }},
  "news_items": [
    {{
      "player": "Nombre",
      "headline": "Noticia breve",
      "impact": "positivo|negativo|neutro"
    }}
  ],
  "summary": "Resumen de 2-3 oraciones en español argentino del panorama de noticias"
}}

REGLAS:
- factor: entre 0.80 (muy negativo) y 1.20 (muy positivo), 1.0 = sin cambio
- Solo ajustá cuando la noticia DIRECTAMENTE impacta el rendimiento estadístico
- Lesión confirmada OUT: no debe estar en picks (ya filtrado), pero ponélo en news_items
- "Ramp-up" post-lesión: puede bajar o subir factor según cuánto falta para el juego
- Si no encontrás noticias relevantes, decí factor=1.0 para todos"""

        try:
            raw = self.run(
                prompt,
                tools=_NEWS_TOOLS,
                tool_handler=self._tool_handler,
                max_tokens=2048,
            )
            result = self._parse_json(raw, fallback={})
        except Exception as exc:
            logger.warning("[NewsIntelligence] Falló: %s", exc)
            result = {}

        news_items = result.get("news_items", [])
        logger.info(
            "[NewsIntelligence] %d noticias encontradas | %d ajustes",
            len(news_items),
            len(result.get("adjustments", {})),
        )

        return {
            "adjustments": result.get("adjustments", {}),
            "news_items": news_items,
            "summary": result.get("summary", "Sin noticias de último momento."),
        }
