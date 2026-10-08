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
Rodar o piloto (webhook autenticado no n8n principal; `n8n execute` pela CLI NÃO carrega o
módulo de Data Tables):
```bash
scripts/run-pilot.sh 'https://www.youtube.com/watch?v=XXXX' --subject 'Nome Sobrenome' [--notes '...']
scripts/run-pilot.sh '...' --force       # reprocessa vídeo já processado
scripts/run-pilot.sh '...' --no-upload   # teste: renderiza + metadados, não envia (linha <id>:dryrun)
scripts/show-execution.py last                                       # acompanhar
```
Depois de editar `workflows/*.json`: `scripts/deploy-workflows.sh` (n8n 2.x executa a versão PUBLICADA;
`import:workflow` sozinho só atualiza o rascunho).

Persistência (Data Tables): `processed_videos` (status por vídeo: PROCESSING → SELECTED →
UPLOADED | ERROR com `error_reason`) e `execution_errors` (gravada pelo `PAVANATO - 99 - ERRORS`).
`PAVANATO - 01 - PILOT`: no node CONFIG, `vizard_project_id` reaproveita um projeto Vizard
já processado (não gasta minutos). Deixe vazio para processar uma URL nova.
Links de MP4 do Vizard expiram em 7 dias; rodar de novo gera links novos.

## Clipper local (substitui o Vizard)

`clipper/server.py` (launchd `com.pavanatto.clipper`, porta 8788, mesmo token da bridge):
download (yt-dlp, 1080p H.264) → transcrição (mlx-whisper large-v3-turbo, palavra a palavra)
→ render 1080×1920 (reframe por rosto/falante ativo via MediaPipe, legendas com palavra
destacada, marca PAVANATO AGORA, áudio normalizado). Venv, modelos e vídeos em
`/Volumes/MacNVMe/pavanatto-cuts/` (`clipper-venv`, `clipper-cache`, `work/<video_id>`).

| Ação | Comando |
|---|---|
| Instalar / reinstalar | `scripts/install-clipper.sh` |
| Reiniciar após mudar código | `launchctl kickstart -k gui/$(id -u)/com.pavanatto.clipper` |
| Saúde | `curl http://127.0.0.1:8788/healthz` |
| Logs | `tail -f logs/clipper.log` |
| Ver execução do n8n | `scripts/show-execution.py last ["NODE" ...]` |

Python precisa da permissão "Volumes Removíveis" (ou Acesso Total ao Disco), senão trava
no boot lendo o venv.

## Discovery (automático)

Workflow `RADAR PATRIOTA - 00 - DISCOVERY`, a cada 2h (06–22h): busca gratuita (yt-dlp: busca por data +
canais oficiais) → metadados via API (1 unidade) → classificação da fonte por IA → política do plano
(cortes de terceiros/reupload = IGNORE; NEWS/INSTITUTIONAL/UNKNOWN = REVIEW; fonte primária em que a
pessoa fala = PROCESS) → limite de uploads (padrão 2 por rodada, 5 por dia) → dispara Short/longo
(sempre PRIVATE). Tudo fica em `discovered_videos` (decision: PROCESS, REVIEW, DEFERRED, IGNORE).

- Pessoas monitoradas: Data Table `watchlist` no n8n (subject_name, subject_notes, queries, channel_urls,
  formats = short | long | both, misspellings, active).
- Rodar agora: `scripts/run-discovery.sh` (ou `--dry` para só classificar).
- **Fila de revisão:** `http://<TAILSCALE_IP>:8090/review` (mesma senha dos arquivos) — itens REVIEW/DEFERRED
  com botões Gerar Short / Gerar longo / Ignorar, e os vídeos gerados com link para o Studio
  (workflow `RADAR PATRIOTA - 03 - REVIEW`).
- Vídeos com falha técnica (status ERROR) são tentados de novo na rodada seguinte; REJECTED não.
- Pausar a automação: Data Table → `active=false` em todas as linhas, ou despublicar o workflow 00.

## Vídeo longo (16:9)

```bash
scripts/run-longform.sh '<url>' --subject 'Nome' --mode best_of --minutes 5      # melhores momentos
scripts/run-longform.sh '<url>' --subject 'Nome' --mode continuous --minutes 8   # corte contínuo
```
Workflow `RADAR PATRIOTA - 02 - LONGFORM`: planner (abertura + segmentos + capítulos) → revisão de
fidelidade (só bloqueia o que muda o sentido) → legendas revisadas → metadados → render 1920×1080
(cartão de título, tarjas de capítulo, legendas acima de marcas d'água, "inscreva-se") → descrição com
capítulos → upload PRIVATE + thumbnail (1280×720). Linha na tabela: `<id>:long`.

**Fotos de referência (rosto):** `/Volumes/MacNVMe/pavanatto-cuts/assets/people/<nome-slug>/*.jpg`
(2–3 fotos nítidas, de frente). Usadas para a thumbnail e a foto do topo dos Shorts mostrarem a pessoa
certa. Sem fotos, cai no "rosto que está falando".

## Acessar os vídeos de outro aparelho

1. **Navegador (Tailscale):** `http://<TAILSCALE_IP>:8090` — usuário `radar`, senha `FILES_PASSWORD` do
   `.env`. Somente leitura, assiste/baixa (suporta avanço no player). Só escuta no IP do Tailscale.
   Serviço launchd `com.pavanatto.files` (`scripts/install-files-server.sh`, log `logs/files-server.log`).
2. **Finder (SMB):** Ajustes → Geral → Compartilhamento → Compartilhamento de Arquivos, pasta
   `/Volumes/MacNVMe/pavanatto-cuts/downloads`; no MacBook: Finder → Cmd+K → `smb://<TAILSCALE_IP>`.

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
