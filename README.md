# Transcritor AIVEXOR (Whisper self-hosted)

Microserviço que recebe um áudio de reunião e devolve a transcrição rotulada
por falante ("Você" / "Interlocutor"), aproveitando os 2 canais do gravador.
Roda na VPS (KVM4), chamado pelo n8n. Whisper local, sem custo por minuto.

## Endpoints

- `GET /health` → `{ ok, modelo, idioma }`
- `POST /transcrever` → multipart com campo `arquivo`; header `X-Token` se protegido.
  Resposta: `{ idioma, duracao_s, n_segmentos, texto, segmentos[] }`

## Deploy no EasyPanel

1. **Novo serviço** `transcritor` no **mesmo projeto** do n8n (pra falarem pela rede interna).
2. **Source**: aponte pro repositório Git com esta pasta (build via Dockerfile), OU
   use uma imagem publicada (ver abaixo). Build por Dockerfile é o caminho padrão.
3. **Porta interna**: `8000`. **Não** exponha domínio público (só o n8n precisa acessar).
4. **Variáveis de ambiente**:
   - `WHISPER_MODEL=medium`   (troque pra `large-v3` pra mais precisão)
   - `WHISPER_LANG=pt`
   - `WHISPER_THREADS=2`      (limita CPU; deixa folga pro Evolution)
   - `TRANSCRITOR_TOKEN=<um segredo>`  (o n8n manda no header X-Token)
5. **Recursos (importante)**: limite a **~2 vCPU** e **~4 GB RAM**. A transcrição é
   assíncrona; melhor lenta do que sufocar o WhatsApp.
6. **Volume persistente**: monte um volume em `/cache` (`HF_HOME`) pra não
   rebaixar o modelo a cada restart. O `medium` baixa ~1.5 GB no primeiro uso.

Depois do deploy, o n8n acessa em `http://transcritor:8000` (nome do serviço na
rede interna do EasyPanel).

## Teste rápido (do próprio n8n ou via curl na VPS)

```
curl -F "arquivo=@reuniao.opus" -H "X-Token: <segredo>" http://transcritor:8000/transcrever
```

## Dimensionamento (KVM4: 4 vCPU / 16 GB, compartilhada com n8n + Evolution)

| Modelo    | RAM    | Qualidade PT | Velocidade (1h de reunião, ~2 vCPU) |
|-----------|--------|--------------|-------------------------------------|
| medium    | ~2 GB  | muito boa    | ~10-20 min                          |
| large-v3  | ~3-4 GB| excelente    | ~25-45 min                          |

Com VAD, os silêncios são pulados, então o tempo real costuma ficar abaixo disso.
