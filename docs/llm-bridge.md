# LLM Bridge

O n8n não chama APIs de LLM diretamente. Ele chama `bridge/server.mjs`, que roda no Mac
(fora do Docker) e executa os CLIs oficiais já logados nas assinaturas:

- **Claude Code** (`claude -p`, assinatura Claude Pro) — principal, modelo `sonnet`
- **Codex** (`codex exec`, login ChatGPT) — fallback automático

Sem API keys. O consumo entra nos limites das assinaturas.

## Contrato

`POST http://host.docker.internal:8787/v1/complete` (de dentro do n8n)
Credencial n8n: **LLM Bridge (local)** (Header Auth).

```json
{ "prompt": "...", "system": "...", "schema": { "type": "object", ... }, "provider": "auto" }
```

- `provider`: `auto` (Claude → Codex), `claude` ou `codex`.
- `schema`: JSON Schema da resposta. Para o Codex aceitar: `additionalProperties: false`
  e todas as propriedades em `required`.

Resposta OK (200): `{ ok, provider, model, text, json, duration_ms, attempts }`
Falha (502): `{ ok: false, error_code, error_message, attempts }` com
`error_code` ∈ `LLM_RATE_LIMITED | LLM_TIMEOUT | LLM_INVALID_JSON | LLM_INVALID_OUTPUT | LLM_FAILED | LLM_SPAWN_FAILED`.

## Isolamento

Cada chamada roda num diretório temporário vazio, sem ferramentas, sem MCP, sem CLAUDE.md,
sem sessão persistida. `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` são removidas do ambiente para
forçar o login da assinatura. O log (`logs/llm-bridge.log`) guarda só metadados, nunca prompts.

A bridge escuta só em `127.0.0.1` e exige `Authorization: Bearer $LLM_BRIDGE_TOKEN`.

## Operação

| Ação | Comando |
|---|---|
| Instalar/reinstalar | `scripts/install-llm-bridge.sh` |
| Status | `curl http://127.0.0.1:8787/healthz` |
| Logs | `tail -f logs/llm-bridge.log` |
| Parar | `launchctl bootout gui/$(id -u)/com.pavanatto.llm-bridge` |

Se `claude`/`codex` deslogarem: rode `claude` (e `/login`) ou `codex login` no Mac mini.
