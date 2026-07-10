# Logging — convenção desta lib

Esta biblioteca **emite** logs; o **host configura** (handlers, formato, nível,
contexto de tenant). Regras:

1. Use `logging.getLogger("cogno_observability.<módulo>")` no topo do módulo. Nada de
   handlers, formatters, `basicConfig` ou um `get_logger` próprio.
2. Mensagem = só o fato, em `key=value`, sempre lazy:
   `logger.warning("event=metrics_record_failed error=%s", exc)`.
   NÃO coloque tenant_id / timestamp / channel na mensagem — o host injeta via
   contextvars + Filter no root logger.
3. Níveis:
   - **ERROR**  → nunca aqui; erro fatal vira exceção e propaga.
   - **WARNING**→ condição recuperada/tratada: `record()` falhou (best-effort,
     observabilidade nunca quebra o turno), instrumentação HTTP pulada (extra
     `[http]` ausente), `PROMETHEUS_MULTIPROC_DIR` inexistente.
   - **INFO**   → marco raro: `event=metrics_mounted`, `event=http_instrumented`.
     NÃO por-turno (o `record()` é hot-path — silencioso).
   - **DEBUG**  → não usado.
4. Controle de nível é por pacote:
   `logging.getLogger("cogno_observability").setLevel(...)`.

O host anexa o handler (filtro de contexto + formatter) ao root logger real; esta
lib nunca o faz. Os **dados de métrica** vão para o Prometheus via `record()`, não
para o log — não duplicar (o log é para eventos de plumbing, não para telemetria).

## O que esta lib loga

| Logger | Evento | Nível |
|---|---|---|
| `cogno_observability.sink` | `metrics_record_failed` (record deu erro — engolido) | WARNING |
| `cogno_observability.instrument` | `metrics_mounted` / `http_instrumented` | INFO |
| `cogno_observability.instrument` | `http_instrument_skipped` (sem o extra `[http]`) | WARNING |
| `cogno_observability.instrument` | `multiproc_dir_missing` | WARNING |
