# Pavanatto Cuts Automation

Pipeline n8n que transforma uma URL do YouTube em um Short (Vizard → IA editorial → YouTube).
Plano completo: `docs/plan.md` (local, não versionado). Operação: [docs/runbook.md](docs/runbook.md). IA via assinaturas Claude/Codex: [docs/llm-bridge.md](docs/llm-bridge.md).

## Status

| Milestone | Estado |
|---|---|
| M0 — Infra (Docker, n8n, persistência, runbook) | ✅ |
| LLM bridge (Claude/Codex via assinatura) | ✅ |
| M1 — Vizard | ⏳ |
| M2 — Editorial AI | ⏳ |
| M3 — YouTube | ⏳ |
| M4 — Pilot Done | ⏳ |

## Setup

```bash
cp .env.example .env                                   # preencha; gere a chave:
sed -i '' "s/^N8N_ENCRYPTION_KEY=$/N8N_ENCRYPTION_KEY=$(openssl rand -hex 32)/" .env
docker compose up -d
./scripts/install-llm-bridge.sh                        # IA (Claude Code / Codex)
open http://localhost:5678
```

O n8n escuta em `127.0.0.1` e, se `TAILSCALE_IP` estiver no `.env`, também nesse IP (acesso pela tailnet: `http://<TAILSCALE_IP>:5678`). Não é exposto na LAN. `downloads/` aparece como `/files` dentro do container.
