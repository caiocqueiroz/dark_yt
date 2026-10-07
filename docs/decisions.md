# Decisões técnicas

## D1 — LLM via assinaturas (Claude Code / Codex CLI), sem API keys
Ver `docs/llm-bridge.md`. Consumo entra no limite das assinaturas; fallback automático Claude → Codex.

## D2 — Substituir o Vizard por um clipper local (implementado, 2026-10-07)

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
    → legendas ASS (palavra destacada, máx. 2 linhas) + identidade PAVANATO AGORA
    → MP4 1080×1920 sem marca d'água
```

- Custo zero por vídeo; sem limite de requisições; identidade visual própria (ajuda contra a
  política de conteúdo reutilizado).
- Referências open source (MIT) para reaproveitar ideias/código: AutoClip
  (github.com/artbyjazi/autoclip), OpenShorts (github.com/mutonby/openshorts).
- O Editorial Judge e o resto do workflow não mudam: o clipper entrega o mesmo clip schema.
- Risco: download via yt-dlp depende do YouTube não bloquear; manter Vizard pago como plano B.

**Validação (Folha, Sn3H5UzsDoY, 45 min):** prepare 5 min (download + transcrição), finder
10 propostas → 9 válidas, judge marcou 2 como HIGH risk, melhor = 75 (SELECTED), render
1080×1920 de 48s em 50s. Legendas e reframe conferidos visualmente.

Limitações conhecidas: tarja/letreiro da emissora (lower third) aparece no corte; não há
diarização (o finder infere o falante pelo contexto, o reframe usa movimento da boca).

## D3 — Ajustes editoriais do clipper (2026-10-07)

- **Qualidade:** fonte = maior bitrate até 1080p (VP9 "Premium" ~4,7 Mbps quando existir, vídeo
  baixado em paralelo à transcrição); reframe com Lanczos + nitidez leve; libx264 CRF 17.
- **Início/fim consistentes:** o finder trabalha com FRASES (montadas dos timestamps por
  palavra; frases > 8s quebradas na maior pausa), com a duração de cada frase visível. Bordas
  com folga no silêncio + fade de áudio. Quando a transcrição junta falas de pessoas diferentes,
  o finder informa as palavras a cortar (`start_trim_text`/`end_trim_text`).
- **Contexto:** pergunta do entrevistador incluída (rótulo PERGUNTA) quando curta e próxima;
  senão card CONTEXTO nos primeiros 6s. Judge penaliza contexto infiel e final abrupto.
- **Legendas revisadas:** LLM corrige só erros evidentes de ASR (nomes, números, datas), com
  alinhamento palavra a palavra nos tempos originais.
- **Faixa da emissora:** letreiro fixo detectado automaticamente (linhas que não mudam entre
  câmeras) + 5,5% acima para tarjas de nome; coberto por banner do canal com
  "CANAL FÃ • NÃO OFICIAL • FONTE: <canal>". Com banner, a marca do topo sai.
- **Nome:** a grafia correta é **Pavanato** (um T). Identificadores técnicos antigos
  (`pavanatto-n8n`, `com.pavanatto.*`, `pavanatto-cuts`) foram mantidos para não quebrar a infra.
