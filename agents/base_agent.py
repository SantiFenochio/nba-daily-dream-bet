"""
base_agent.py — Clase base para todos los subagents NBA.

Provee:
  - Anthropic client (lee ANTHROPIC_API_KEY del entorno)
  - Loop de tool use
  - Retry con backoff en rate limit / connection errors
  - Logging estructurado
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import anthropic

from agents.system_prompts import MASTER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Modelo por defecto: haiku para velocidad/costo en GitHub Actions
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class BaseAgent:
    """
    Clase base para todos los subagents del sistema NBA multi-agent.

    Subclases deben implementar su lógica específica llamando a `self.run()`.
    """

    def __init__(self, agent_name: str, model: str = _DEFAULT_MODEL) -> None:
        self.name = agent_name
        self.model = model
        # Lee ANTHROPIC_API_KEY automáticamente del entorno
        self.client = anthropic.Anthropic()

    # ── Core API call ──────────────────────────────────────────────────────────

    def run(
        self,
        user_prompt: str,
        tools: list[dict] | None = None,
        tool_handler: Callable[[str, dict], Any] | None = None,
        max_tokens: int = 2048,
        retries: int = 2,
    ) -> str:
        """
        Ejecuta un turno del agente con soporte completo de tool use.

        Args:
            user_prompt:   Prompt específico para esta tarea.
            tools:         Definiciones de tools en formato Anthropic (JSON schema).
            tool_handler:  Callable(tool_name, tool_input) → resultado como string.
            max_tokens:    Máximo de tokens en la respuesta.
            retries:       Reintentos en errores transitorios.

        Returns:
            Texto final de la respuesta (vacío si todos los reintentos fallan).
        """
        messages: list[dict] = [{"role": "user", "content": user_prompt}]

        for attempt in range(retries + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "system": MASTER_SYSTEM_PROMPT,
                    "messages": messages,
                }
                if tools:
                    kwargs["tools"] = tools

                response = self.client.messages.create(**kwargs)
                logger.debug("[%s] stop_reason=%s", self.name, response.stop_reason)

                # ── Tool use loop ──────────────────────────────────────────────
                while response.stop_reason == "tool_use" and tool_handler:
                    tool_results: list[dict] = []

                    for block in response.content:
                        if block.type == "tool_use":
                            logger.info(
                                "[%s] → tool: %s(%s)",
                                self.name,
                                block.name,
                                json.dumps(block.input)[:120],
                            )
                            try:
                                result = tool_handler(block.name, block.input)
                                result_str = (
                                    json.dumps(result) if not isinstance(result, str) else result
                                )
                            except Exception as tool_exc:
                                logger.warning("[%s] Tool error: %s", self.name, tool_exc)
                                result_str = f"ERROR al ejecutar {block.name}: {tool_exc}"

                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    # Truncate enormous results to avoid token overflow
                                    "content": result_str[:6000],
                                }
                            )

                    messages = messages + [
                        {"role": "assistant", "content": response.content},
                        {"role": "user", "content": tool_results},
                    ]
                    kwargs["messages"] = messages
                    response = self.client.messages.create(**kwargs)

                # ── Extraer texto ──────────────────────────────────────────────
                text_parts = [b.text for b in response.content if hasattr(b, "text")]
                return "\n".join(text_parts).strip()

            except anthropic.RateLimitError:
                if attempt < retries:
                    wait = 30 * (attempt + 1)
                    logger.warning("[%s] Rate limit — esperando %ds (intento %d)", self.name, wait, attempt + 1)
                    time.sleep(wait)
                else:
                    raise

            except anthropic.APIConnectionError as conn_err:
                if attempt < retries:
                    logger.warning("[%s] Connection error, reintentando: %s", self.name, conn_err)
                    time.sleep(5)
                else:
                    raise

            except Exception as exc:
                logger.error("[%s] Error inesperado: %s", self.name, exc)
                if attempt < retries:
                    time.sleep(5)
                else:
                    raise

        return ""

    # ── Helper: parse JSON safely ──────────────────────────────────────────────

    @staticmethod
    def _parse_json(text: str, fallback: Any = None) -> Any:
        """
        Extrae el primer bloque JSON válido del texto de la respuesta.
        Intenta parsear directamente, luego busca entre llaves/corchetes.
        """
        text = text.strip()
        # Direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Find first {...} or [...]
        for start_char, end_char in [('{', '}'), ('[', ']')]:
            start = text.find(start_char)
            end = text.rfind(end_char)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start: end + 1])
                except json.JSONDecodeError:
                    pass
        logger.warning("No se pudo parsear JSON de respuesta: %s…", text[:200])
        return fallback
