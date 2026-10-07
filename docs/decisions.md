# Decisões técnicas

## D1 — LLM via assinaturas (Claude Code / Codex CLI), sem API keys
Ver `docs/llm-bridge.md`. Consumo entra no limite das assinaturas; fallback automático Claude → Codex.

## D2 — Substituir o Vizard por um clipper local (proposta, 2026-10-07)

**Problema (Vizard no plano Free):** marca d'água, vinheta/propaganda no final, export em 720p,
limite de 1 req/min · 10/h e só ~4 candidatos para um vídeo de 9 min. Remover marca d'água
e ter 1080p+ exige plano pago (Creator/Business).

**O plano já previa a saída** (§57): "FFmpeg entra se Vizard limitar identidade, custo ou
qualidade". Os três aconteceram.

**Proposta — `clipper` local no Mac mini (mesmo padrão da LLM bridge):**

```
URL → yt-dlp (vídeo 1080p + áudio)
    → mlx-whisper (Metal, transcrição PT-BR com timestamp por palavra)
    → Editorial Judge escolhe trechos direto da transcrição (já existe)
    → FFmpeg: corte + reframe 9:16 com tracking de rosto (MediaPipe)
    → legendas ASS (palavra destacada, máx. 2 linhas) + identidade PAVANATTO AGORA
    → MP4 1080×1920 sem marca d'água
```

- Custo zero por vídeo; sem limite de requisições; identidade visual própria (ajuda contra a
  política de conteúdo reutilizado).
- Referências open source (MIT) para reaproveitar ideias/código: AutoClip
  (github.com/artbyjazi/autoclip), OpenShorts (github.com/mutonby/openshorts).
- O Editorial Judge e o resto do workflow não mudam: o clipper entrega o mesmo clip schema.
- Risco: download via yt-dlp depende do YouTube não bloquear; manter Vizard pago como plano B.
