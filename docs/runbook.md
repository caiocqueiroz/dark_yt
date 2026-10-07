# Runbook

Se `docker` não estiver no PATH:
`export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH`

| Ação | Comando |
|---|---|
| Status | `docker ps` |
| Saúde | `curl http://127.0.0.1:5678/healthz` |
| Logs | `docker logs -f pavanatto-n8n` |
| Restart | `docker compose restart` |
| Down / Up | `docker compose down` / `docker compose up -d` |
| LLM bridge | `curl http://127.0.0.1:8787/healthz` · logs em `logs/llm-bridge.log` · ver [llm-bridge.md](llm-bridge.md) |
| Backup | `scripts/backup.sh` (volume → `BACKUP_DIR`, workflows → `workflows/`) |
| Atualizar n8n | trocar a tag em `docker-compose.yml`, `scripts/backup.sh`, `docker compose up -d` |

## Workflows

Fonte de verdade: `workflows/*.json` no repo. Importar/atualizar no n8n:
```bash
docker cp workflows/01-pilot.json pavanatto-n8n:/tmp/wf.json
docker exec pavanatto-n8n n8n import:workflow --input=/tmp/wf.json
```
Executar pela CLI (porta do task broker precisa ser outra, a 5679 é do n8n principal):
```bash
docker exec -e N8N_RUNNERS_BROKER_PORT=5690 pavanatto-n8n n8n execute --id=pavanattoPilot01 --rawOutput
```
`PAVANATTO - 01 - PILOT`: no node CONFIG, `vizard_project_id` reaproveita um projeto Vizard
já processado (não gasta minutos). Deixe vazio para processar uma URL nova.
Links de MP4 do Vizard expiram em 7 dias; rodar de novo gera links novos.

## LLM bridge (Claude / Codex sem API key)

`bridge/server.mjs` expõe os CLIs `claude` e `codex` (logados nas assinaturas Pro) como
`POST http://host.docker.internal:8787/v1/complete` para o n8n. Escuta só em `127.0.0.1`.
Roda via launchd (`com.pavanatto.llm-bridge`), sobe no login e reinicia se cair.

| Ação | Comando |
|---|---|
| Instalar / reinstalar | `scripts/install-llm-bridge.sh` |
| Saúde | `curl http://127.0.0.1:8787/healthz` |
| Logs (só metadados, nunca prompts) | `tail -f logs/llm-bridge.log` |
| Parar | `launchctl bootout gui/$(id -u)/com.pavanatto.llm-bridge` |

Request (`Authorization: Bearer $LLM_BRIDGE_TOKEN`, credencial n8n "LLM Bridge (local)"):
```json
{ "prompt": "...", "system": "...", "schema": { "type": "object", ... }, "provider": "auto" }
```
`provider`: `auto` (Claude → fallback Codex), `claude` ou `codex`. Com `schema`, a resposta vem em `json`.
Para o Codex, o schema precisa de `additionalProperties: false` e todos os campos em `required`.
Erros: `LLM_RATE_LIMITED`, `LLM_TIMEOUT`, `LLM_INVALID_JSON`, `LLM_INVALID_OUTPUT`, `LLM_FAILED`.

Se der `LLM_FAILED` por login expirado: rode `claude` ou `codex login` no Mac mini e refaça o login.

## Restaurar backup

```bash
docker compose down
docker run --rm -v pavanatto_n8n_data:/data -v /Volumes/MacNVMe/pavanatto-cuts/backups:/backup alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/<arquivo>.tgz -C /data'
docker compose up -d
```

Precisa do mesmo `N8N_ENCRYPTION_KEY` do `.env`, senão as credenciais não abrem.
Guarde o `.env` fora do repositório (ex.: gerenciador de senhas).

## Problemas conhecidos

- **"Docker Desktop is unable to start" / falha ao instalar Rosetta**: desative o Rosetta
  (Settings → General → "Use Rosetta"). A imagem do n8n é arm64 nativa.

## Host (Mac mini)

- Docker Desktop → Settings → General → "Start Docker Desktop when you sign in".
- Ajustes → Energia: impedir repouso automático; "iniciar após queda de energia".
- Armazenamento: MP4s e backups ficam no disco externo (`DOWNLOADS_DIR`/`BACKUP_DIR` no `.env`,
  hoje `/Volumes/MacNVMe/pavanatto-cuts/`). O disco precisa estar montado antes do `docker compose up`.
- Dados do Docker (imagens, volume `pavanatto_n8n_data`) ficam em `/Volumes/MacNVMe/DockerData`
  (Docker Desktop → Settings → Resources → Disk image location). Sem o disco montado, o Docker não sobe.
- Faça backups com o container parado (`docker compose stop`) quando precisar de consistência total
  do SQLite; o `backup.sh` com ele rodando é suficiente para o dia a dia.
- Docker precisa da permissão macOS "Volumes Removíveis" (Privacidade e Segurança → Arquivos e Pastas);
  sem ela o `docker compose up` trava esperando aprovação.
