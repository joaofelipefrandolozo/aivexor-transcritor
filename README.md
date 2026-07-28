# Transcritor AIVEXOR (Whisper self-hosted)

Microserviço que recebe um áudio de reunião e devolve a transcrição rotulada
por falante ("Você" / "Interlocutor"), aproveitando os 2 canais do gravador.
Roda na VPS (KVM4), chamado pelo n8n. Whisper local, sem custo por minuto.

## Endpoints

- `GET /health` → `{ ok, modelo, idioma, ocupado, ocupado_s, teto_s, fila }`
- `POST /transcrever` → **síncrono** (gravador desktop e n8n): multipart com campo
  `arquivo`; header `X-Token` se protegido. Segura a conexão até terminar.
  Resposta: `{ idioma, duracao_s, n_segmentos, texto, segmentos[] }`
- `POST /transcrever-async` → **assíncrono** (tela `/reuniao` do CRM, upload do
  celular): multipart com `arquivo`, `job_id`, `ticket`, `callback_url`,
  `callback_token`. Responde `202 { ok, job_id, na_fila }` assim que o upload
  termina e devolve o texto depois, por callback.

### Por que existe o modo assíncrono

O `/transcrever` segura a conexão por ~15 min por pedaço, o que funciona no app
desktop mas não no celular (a tela bloqueia, a rede troca, o request morre). No
assíncrono o telefone só faz o upload: a fila roda na VPS e o CRM recebe o texto
quando ficar pronto.

**Autenticação por ticket**, não pelo `X-Token`: quem sobe é o navegador do dono,
e o token do serviço não pode viajar até lá. O CRM assina `job_id` + validade com
o `UPLOAD_SECRET` (HMAC-SHA256) e o transcritor confere localmente. O ticket vale
só para aquele job, expira, e o resultado só volta para o callback do CRM.

**Callbacks** (POST no `callback_url`, header `X-Callback-Token`):

| evento    | corpo                                              |
|-----------|----------------------------------------------------|
| `started` | `{ job_id, evento, duracao_s }`                    |
| `done`    | `{ job_id, evento, texto, idioma, duracao_s, n_segmentos }` |
| `error`   | `{ job_id, evento, erro }`                         |

A fila fica em disco (`JOBS_DIR`), então um restart do container não perde os
áudios que estavam esperando a vez: no boot eles voltam para a fila. Um job que
falha duas vezes (`MAX_TENTATIVAS`) vira `error` em vez de virar loop de restart.

## Robustez (aprendida nos incidentes de 2026-07-13 e 2026-07-16)

- **`/health` responde sempre**, inclusive durante uma transcrição (endpoints
  síncronos rodam no threadpool; antes, um request travado matava o serviço
  inteiro e o health junto).
- **Todo ffmpeg/ffprobe tem timeout**: subprocess pendurado vira erro HTTP
  limpo, não serviço morto.
- **1 transcrição por vez**: um segundo POST recebe `503 ocupado` na hora
  (protege a memória da VPS). Quem chama deve tentar de novo depois.
- **Transcrição em blocos** (`WHISPER_BLOCO_S`, default 600s): memória
  constante em qualquer duração de reunião.
- **HEALTHCHECK no Dockerfile**: o Docker/EasyPanel marca o container
  unhealthy quando o HTTP para de responder (visível no painel).
- Um watchdog no n8n ("AIVEXOR Watchdog Transcritor") vigia o `/health` a
  cada 15 min e avisa no WhatsApp quando cai e quando volta.

## Deploy no EasyPanel

1. **Novo serviço** `transcritor` no **mesmo projeto** do n8n (pra falarem pela rede interna).
2. **Source**: aponte pro repositório Git com esta pasta (build via Dockerfile), OU
   use uma imagem publicada (ver abaixo). Build por Dockerfile é o caminho padrão.
3. **Porta interna**: `8000`. O domínio público é necessário desde o
   `/transcrever-async` (o navegador do celular sobe direto para cá); o n8n
   continua chamando pela rede interna.
4. **Variáveis de ambiente**:
   - `WHISPER_MODEL=medium`   (troque pra `large-v3` pra mais precisão)
   - `WHISPER_LANG=pt`
   - `WHISPER_THREADS=2`      (limita CPU; deixa folga pro Evolution)
   - `TRANSCRITOR_TOKEN=<um segredo>`  (o n8n manda no header X-Token)
   - `UPLOAD_SECRET=<segredo compartilhado com o CRM>` (assina os tickets do
     `/transcrever-async`; tem que ser idêntico ao do CRM)
   - `CORS_ORIGINS=https://crm.aivexor.com.br` (origens que podem subir áudio
     pelo browser; em dev, some `http://localhost:3000`)
   - `JOBS_DIR=/jobs` (opcional, mas recomendado com volume: a fila sobrevive a
     restart. Sem volume, cai no diretório temporário do container)
   - `OCUPADO_FATOR=4` (opcional: teto do watchdog = duração do áudio × fator)
5. **Recursos (importante)**: limite a **~2 vCPU** e **~4 GB RAM**. A transcrição é
   assíncrona; melhor lenta do que sufocar o WhatsApp.
6. **Volume persistente**: monte um volume em `/cache` (`HF_HOME`) pra não
   rebaixar o modelo a cada restart. O `medium` baixa ~1.5 GB no primeiro uso.
   Monte outro em `/jobs` (`JOBS_DIR`) pra fila de uploads sobreviver a restart.

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
