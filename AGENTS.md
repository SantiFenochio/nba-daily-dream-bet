# AGENTS.md — Documentación del Sistema Multi-Agent NBA

Documentación técnica completa de cada subagent del sistema.
Ver también: `README.md` sección "Arquitectura Multi-Agent con Claude".

---

## Arquitectura general

```
Orchestrator (agents/orchestrator.py)
│
├── DataValidatorAgent        (agents/subagent_data_validator.py)
├── NewsIntelligenceAgent     (agents/subagent_news_intelligence.py)
├── ProjectionAgent           (agents/subagent_projection.py)
├── EVOptimizerAgent          (agents/subagent_ev_optimizer.py)
├── NarratorAgent             (agents/subagent_narrator.py)
└── AutoCalibratorAgent       (agents/subagent_auto_calibrator.py)
```

Todos heredan de `BaseAgent` (`agents/base_agent.py`) que provee:
- Anthropic client (lee `ANTHROPIC_API_KEY` del entorno)
- Loop de tool use con dispatcher
- Retry con backoff exponencial (rate limit + connection errors)
- `_parse_json()` para extraer JSON de respuestas mixtas

---

## BaseAgent (`agents/base_agent.py`)

### Propósito
Clase base compartida. Gestiona toda la complejidad de la API de Anthropic.

### Métodos clave

```python
def run(
    user_prompt: str,
    tools: list[dict] | None = None,
    tool_handler: Callable | None = None,
    max_tokens: int = 2048,
    retries: int = 2,
) -> str
```

- Envía `user_prompt` con `MASTER_SYSTEM_PROMPT` como system
- Si hay `tools`, entra en loop de tool use hasta `stop_reason != "tool_use"`
- Retry automático: 30s en rate limit, 5s en connection errors
- Retorna string de texto (vacío si todos los reintentos fallan)

```python
@staticmethod
def _parse_json(text: str, fallback=None) -> Any
```

- Intenta parsear JSON directamente
- Si falla, busca primer bloque `{...}` o `[...]` en el texto
- Útil para cuando Claude incluye texto antes/después del JSON

### Modelo por defecto
`claude-haiku-4-5-20251001` — rápido y económico para GitHub Actions.
El `NarratorAgent` usa `claude-sonnet-4-6` para mejor calidad de output.

---

## DataValidatorAgent (`agents/subagent_data_validator.py`)

### Propósito
Primer agente en el pipeline. Detecta inconsistencias antes de enviar el análisis.

### Input
```python
validate(
    picks_by_game: dict[str, list[PlayerPick]],
    projections: dict | None,
    injury_statuses: dict | None,
) -> dict
```

### Validaciones Python (pre-Claude)
1. **model_prob mismatch**: verifica que `model_prob ≈ 0.65 * HR_L15 + 0.35 * HR_L5` (threshold: 5%)
2. **Injury + Alta**: jugador Questionable con confianza Alta → señal de posible sobreestimación
3. **SportsData projection outlier**: proyección > 25% alejada de la línea

### Validaciones Claude
- Picks con EV% alto pero HR_L15 bajo (inconsistencia estadística)
- Falta de diversificación (muchos picks del mismo equipo)
- Datos potencialmente desactualizados

### Output
```json
{
  "alerts": [{"player": "...", "market": "...", "issue": "...", "severity": "warn|error"}],
  "excluded_keys": ["player|market_key"],
  "mismatch_count": 2,
  "data_quality_score": 0.92,
  "notes": "Resumen general"
}
```

### Efecto en el pipeline
- `severity=error` + `excluded_keys`: los picks son eliminados del `picks_by_game`
- Alertas alimentan el `NarratorAgent` para mención en el mensaje final

---

## NewsIntelligenceAgent (`agents/subagent_news_intelligence.py`)

### Propósito
Búsqueda de noticias de último momento usando **tool_use de Anthropic**.
Claude decide qué jugadores investigar y qué fuentes consultar.

### Input
```python
gather(
    picks_by_game: dict[str, list[PlayerPick]],
    date_str: str,
) -> dict
```

### Tools disponibles

| Tool | Fuente | Datos |
|---|---|---|
| `fetch_espn_nba_news` | ESPN API pública | Top 20 noticias NBA del día |
| `fetch_espn_injury_report` | ESPN API pública | Injury report oficial NBA |
| `fetch_rotoworld_player_news` | Rotowire (scrape) | Noticias por jugador específico |

### Flujo de tool use
1. Claude recibe la lista de jugadores del día
2. Claude llama `fetch_espn_nba_news` para panorama general
3. Claude llama `fetch_espn_injury_report` para estados de lesión
4. Claude llama `fetch_rotoworld_player_news` para jugadores con señales de alerta
5. Claude integra todo y devuelve JSON de ajustes

### Output
```json
{
  "adjustments": {
    "LeBron James": {"factor": 0.90, "reason": "Reportado como day-to-day por rodilla", "source": "ESPN"},
    "Jaylen Brown": {"factor": 1.08, "reason": "Regresó al 100% de práctica ayer", "source": "Rotowire"}
  },
  "news_items": [
    {"player": "LeBron James", "headline": "Doubt for tonight with knee", "impact": "negativo"}
  ],
  "summary": "LeBron es la gran incógnita del día. Brown con luz verde total."
}
```

### Factor constraints
- Rango permitido: `[0.75, 1.25]` (clampeado en `_apply_refinements`)
- Solo ajusta si la noticia **directamente impacta** el rendimiento estadístico

---

## ProjectionAgent (`agents/subagent_projection.py`)

### Propósito
Mejora las probabilidades del modelo con simulación Monte Carlo y ajuste cualitativo.

### Input
```python
enhance(
    picks_by_game: dict[str, list[PlayerPick]],
    player_logs: dict[str, list[dict]],
    injury_statuses: dict | None,
) -> dict
```

### Monte Carlo (implementación Python)

**Bootstrap MC** (60% del peso):
```python
# Resampleo con reemplazo de la distribución empírica histórica
simulated = np.random.choice(historical_values, size=1000, replace=True)
p_bootstrap = np.mean(simulated > line)
```

**Normal MC** (40% del peso):
```python
# Asume distribución normal con media y std empíricos
from scipy.stats import norm
p_normal = 1.0 - norm.cdf(line, loc=mu, scale=sigma)
```

**Combinación**: `mc_prob = p_bootstrap * 0.60 + p_normal * 0.40`

Ventaja del blend: bootstrap es robusto con distribuciones asimétricas (ej: un jugador que explota 3 veces en L20), normal es más estable con pocos datos.

### Divergencia threshold
Si `|mc_prob - model_prob| > 0.12` → el pick se marca como "flagged" para revisión de Claude.

### Output
```json
{
  "mc_probs": {"Luka Doncic|player_points": 0.743},
  "adjustments": [
    {"key": "Luka Doncic|player_points", "factor": 1.08, "reason": "MC sugiere upside real..."}
  ],
  "insights": ["La distribución de Doncic en puntos es positivamente sesgada en L20"],
  "flagged": ["Jayson Tatum|player_rebounds"]
}
```

---

## EVOptimizerAgent (`agents/subagent_ev_optimizer.py`)

### Propósito
Recalcula probabilidades conjuntas de parlays usando **Cholesky Monte Carlo correlacionado**
y selecciona el mejor parlay del día.

### Input
```python
optimize(
    picks_by_game: dict,
    existing_parlays: list[dict],
    news_adjustments: dict | None,
) -> dict
```

### Cholesky Monte Carlo

**Matriz de correlación**:
```
ρ_same_team = 0.30   # Comparten tempo, puntos del equipo, foul trouble colectivo
ρ_same_game = 0.08   # Comparten pace general del partido
ρ_diff_game = 0.00   # Partidos distintos → independientes
```

**Algoritmo** (10.000 simulaciones):
```python
C = build_corr_matrix(legs)   # (n x n)
L = cholesky(C)                # L @ L.T = C
Z = standard_normal(n, 10000)  # (n x 10000)
X = L @ Z                      # correlacionadas
U = norm.cdf(X)                # uniformes [0,1] correlacionadas
hits = U < model_probs[:, None]  # leg i hits en sim j
joint_prob = mean(all(hits, axis=0))
```

### Output
```json
{
  "enhanced_parlays": [{...parlay con corr_joint_prob actualizado...}],
  "best_parlay_key": "La Segura",
  "ev_ranking": [
    {"name": "La Segura", "mc_joint_prob": 0.412, "simple_prob": 0.384, "ev_pct": -9.1}
  ],
  "commentary": "La Segura tiene la mayor probabilidad real con correlación moderada positiva."
}
```

### mc_improvement
El campo `mc_improvement = mc_joint_prob - simple_prob` mide el efecto de la correlación:
- Positivo: legs negativamente correlacionados (diversificación real)
- Negativo: legs positivamente correlacionados (mismo equipo/partido reduce joint prob)

---

## NarratorAgent (`agents/subagent_narrator.py`)

### Propósito
Genera el mensaje final de Telegram. Es el único agente que usa `claude-sonnet-4-6`
porque la calidad del output es lo que el usuario ve directamente.

### Input
Recibe toda la data procesada por los agentes anteriores:
- `picks_by_game` (post-refinements)
- `parlays` (con MC joint probs)
- `escalera_data`, `consistency_picks`
- `accuracy` (histórico de backtesting)
- Summaries de cada agente anterior

### Output HTML Telegram
```html
<b>🏀 NBA Daily Dream Bet — 10 Abr 2026</b>

Ayer: 7/9 (78%) ✅ | Histórico: 64% (347 picks)

<b>Boston Celtics @ Miami Heat</b>
🔥 Jaylen Brown — Puntos O24.5
   HR L15: 80% | L5: 4/5 | EV: +8.3%
   📈 Promedio L15: 28.1 | Racha: 3 🔥

...

<b>PARLAYS DEL DÍA</b>
⭐ <b>La Segura</b> (prob. conjunta: 41.2%)
   ...
```

### Constraints del mensaje
- Máximo: 3900 chars (margen bajo el límite de 4096 de Telegram)
- Si supera el límite, corta en el último doble-salto de línea
- Solo HTML permitido: `<b>`, `<i>`, `<code>`, `<pre>`

---

## AutoCalibratorAgent (`agents/subagent_auto_calibrator.py`)

### Propósito
Corre al final del pipeline. Analiza los picks de los últimos 7 días (ya backtesteados)
y genera sugerencias concretas para mejorar los thresholds del modelo.

### Input
```python
calibrate(
    history: dict,        # picks_history.json completo
    accuracy: dict,       # output de get_calibration_factors()
    date_str: str,
) -> dict
```

### Análisis automático (Python)
- Hit rate real por mercado (últimos 7 días)
- Hit rate real por nivel de confianza
- Comparación con accuracy general (últimos 60 días)

### Output (guardado en `data/calibration_suggestions.json`)
```json
[
  {
    "date": "2026-04-10",
    "accuracy_yesterday": {"hits": 7, "total": 9, "accuracy": 0.78},
    "insights": [
      "player_rebounds underperformando: 52% HR vs 67% esperado",
      "Alta confianza bien calibrada: 79% HR en 14 picks"
    ],
    "threshold_suggestions": {
      "MEDIA_HIT_RATE": 0.68
    },
    "market_notes": {
      "player_rebounds": "Considerar subir threshold L15 a 70%"
    },
    "overall_assessment": "Modelo sólido, ajustar rebotes"
  }
]
```

El archivo mantiene las últimas 30 entradas (30 días de historial de sugerencias).

---

## Orchestrator (`agents/orchestrator.py`)

### Propósito
Coordinador central. Ejecuta el pipeline de 7 pasos y devuelve `OrchestratorResult`.

### Activación
```python
Orchestrator.is_available()  # True si ANTHROPIC_API_KEY está seteada
```

### Degradación graceful
Cada paso está en un bloque `try/except` independiente:
- Si un agente falla → se usa el resultado vacío/default del agente
- Si el Narrator falla → `message = ""` → `main.py` usa `formatter.py`
- Si el Orchestrator entero falla → `main.py` usa `formatter.py`

No existe punto de falla único. El bot siempre envía algo.

### `_apply_refinements()` (función auxiliar)
```python
def _apply_refinements(picks_by_game, news_adjustments, projection_adjustments):
    # Combina factores de News + Projection
    # Clamp: factor ∈ [0.75, 1.25]
    # Modifica pick.score in-place (nunca reemplaza model_prob ni ev_pct base)
```

---

## Sistema de prompts (`agents/system_prompts.py`)

### MASTER_SYSTEM_PROMPT
Prompt compartido por todos los agentes. Define:
- Rol: analista jefe NBA con 15 años de experiencia
- Prioridades: EV real > consistencia > gestión de riesgo > contexto cualitativo
- Metodología: razonamiento paso a paso, no sobreconfianza
- Outputs: español argentino, JSON válido, HTML de Telegram cuando aplique
- Restricciones: no inventar estadísticas, no confirmar lesiones sin datos

---

## Variables de entorno

| Variable | Uso | Obligatoria |
|---|---|---|
| `ANTHROPIC_API_KEY` | Activa el sistema multi-agent | No (degradación a formatter.py) |

---

## Testing local

```bash
# Con multi-agent activo
ANTHROPIC_API_KEY=sk-ant-... python main.py

# Sin multi-agent (modo clásico)
python main.py

# Solo un agente específico
python -c "
from agents.subagent_data_validator import DataValidatorAgent
# ... mockear picks_by_game ...
"

# Con fecha fija para tests
DATE_OVERRIDE=2026-04-09 ANTHROPIC_API_KEY=sk-ant-... python main.py
```

---

## Costos estimados (Anthropic API)

Con los modelos seleccionados y un análisis típico de 15 picks:

| Agente | Modelo | Tokens input | Tokens output | Costo aprox |
|---|---|---|---|---|
| DataValidator | Haiku | ~2K | ~500 | ~$0.001 |
| NewsIntelligence | Haiku | ~3K + tools | ~800 | ~$0.003 |
| ProjectionAgent | Haiku | ~3K | ~600 | ~$0.002 |
| EVOptimizer | Haiku | ~1K | ~300 | ~$0.001 |
| **Narrator** | **Sonnet** | ~4K | ~1.5K | **~$0.025** |
| AutoCalibrator | Haiku | ~2K | ~500 | ~$0.001 |
| **TOTAL/día** | | | | **~$0.033** |

Costo mensual estimado: ~$1 USD. Completamente viable para producción.
