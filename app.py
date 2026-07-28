"""
Microservico de transcricao (AIVEXOR) - roda na VPS, chamado pelo n8n.

Recebe um audio de reuniao e devolve a transcricao ja rotulada por falante,
usando a convencao de 2 canais do gravador:
  - canal L (esquerdo)  -> "Voce"          (seu microfone)
  - canal R (direito)   -> "Interlocutor"  (audio do sistema)

Se o audio for mono (ex.: gravado no celular), transcreve corrido, sem rotulo.
Usa VAD pra pular silencios (cada canal so processa quando ha voz).

Robustez (aprendida nos incidentes de 2026-07-13, 2026-07-16 e 2026-07-21):
  - endpoints sincronos (def): o FastAPI roda cada request numa thread do pool,
    entao o /health SEMPRE responde, mesmo com uma transcricao em andamento
    (antes, um request travado matava o servico inteiro pra sempre);
  - todo ffmpeg/ffprobe tem timeout: subprocess pendurado nao segura o request;
  - so 1 transcricao por vez (lock): um segundo request recebe 503 na hora em
    vez de disputar CPU/memoria e derrubar o container;
  - watchdog de tempo ocupado: o Whisper nao tem timeout interno, entao um bloco
    travado prenderia o lock pra sempre e o /health seguiria dizendo 200 (servico
    "zumbi", vivo por fora e travado por dentro, sem alerta: incidente 2026-07-21,
    7h30 preso). Se uma transcricao passa de OCUPADO_TETO_S, o processo se derruba
    pra o container reiniciar limpo; o /health expoe ocupado_s pra o watchdog do
    n8n ver o tempo, nao so se responde.

Endpoints:
  GET  /health           -> status + modelo carregado + se esta ocupado + fila
  POST /transcrever      -> multipart 'arquivo'; SINCRONO (gravador desktop).
                            Header 'X-Token' se TRANSCRITOR_TOKEN setado.
  POST /transcrever-async-> multipart do browser (tela /reuniao do CRM): aceita,
                            responde 202 na hora e devolve o texto por callback.
                            Autentica por ticket HMAC (UPLOAD_SECRET), nunca pelo
                            token do servico: quem sobe e o navegador do dono.

Por que o modo assincrono existe: o /transcrever segura a conexao por ~15 min
por pedaco, o que funciona no app desktop mas nao no celular (tela bloqueia,
rede troca, o request morre). No assincrono o telefone so faz o upload; a fila
roda na VPS e o CRM recebe o texto quando ficar pronto.
"""
import hashlib
import hmac
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel

MODELO = os.environ.get("WHISPER_MODEL", "medium")
TOKEN = os.environ.get("TRANSCRITOR_TOKEN", "")
THREADS = int(os.environ.get("WHISPER_THREADS", "0"))  # 0 = automatico
IDIOMA = os.environ.get("WHISPER_LANG", "pt")
BLOCO_S = int(os.environ.get("WHISPER_BLOCO_S", "600"))  # transcreve em blocos de N s: memoria constante mesmo em reuniao longa
# teto de tempo pra UMA transcricao. No fluxo sincrono (pedacos de 12 min) um
# request leva ~15 min, entao 40 min (2400s) so estoura em travamento real.
# No assincrono chega a reuniao inteira, e ai o teto vira proporcional a
# duracao do audio (senao o watchdog mataria um job legitimo de 1h30).
OCUPADO_TETO_S = int(os.environ.get("OCUPADO_TETO_S", "2400"))
OCUPADO_FATOR = float(os.environ.get("OCUPADO_FATOR", "4"))
# Segredo compartilhado com o CRM: assina o ticket de upload do browser.
UPLOAD_SECRET = os.environ.get("UPLOAD_SECRET", "")
# Onde os jobs esperam a vez. Fica em disco (nao so em memoria) pra fila
# sobreviver ao restart que o proprio watchdog provoca.
JOBS_DIR = os.environ.get("JOBS_DIR", os.path.join(tempfile.gettempdir(), "aivexor_jobs"))
MAX_TENTATIVAS = int(os.environ.get("MAX_TENTATIVAS", "2"))
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CORS_ORIGINS", "https://crm.aivexor.com.br,http://localhost:3000"
    ).split(",")
    if o.strip()
]

app = FastAPI(title="AIVEXOR Transcritor")
# O upload vem do browser (outro dominio), entao a resposta precisa liberar a
# origem do CRM. Sem credenciais: a autenticacao e o ticket assinado no form.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)
model = WhisperModel(MODELO, device="cpu", compute_type="int8", cpu_threads=THREADS)
_ocupado = threading.Lock()  # 1 transcricao por vez: protege memoria/CPU da VPS
_ocupado_desde = None  # time.monotonic() de quando o lock foi pego; None = livre
_teto_atual = OCUPADO_TETO_S  # teto do job em andamento (o async ajusta pela duracao)
_fila = queue.Queue()  # job_ids esperando o worker


def _watchdog_ocupado():
    """Rede de seguranca contra transcricao presa. O Whisper nao tem timeout
    interno: se model.transcribe pendura num bloco, o lock nunca liberaria e o
    /health seguiria 200 (servico zumbi, sem alerta). Aqui, passado o teto, o
    processo se derruba: o container reinicia limpo (restart policy/HEALTHCHECK),
    o lock some e o watchdog do n8n ve a transicao caiu/voltou. Um restart de
    ~1 min e sempre melhor que um zumbi de horas."""
    while True:
        time.sleep(30)
        inicio = _ocupado_desde
        teto = _teto_atual
        if inicio is not None and (time.monotonic() - inicio) > teto:
            preso = int(time.monotonic() - inicio)
            print(f"[watchdog] transcricao presa ha {preso}s (teto {teto}s); "
                  f"derrubando o processo pra reiniciar limpo e liberar o lock.", flush=True)
            os._exit(1)


threading.Thread(target=_watchdog_ocupado, daemon=True).start()


def _run(cmd, timeout_s):
    """subprocess com timeout: ffmpeg travado vira erro limpo, nao servico morto."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500,
                            detail=f"{os.path.basename(cmd[0])} excedeu {timeout_s}s")


def _num_canais(path: str) -> int:
    r = _run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of",
         "default=noprint_wrappers=1:nokey=1", path],
        timeout_s=60,
    )
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 1


def _extrair(path: str, canal_idx, saida: str):
    """Extrai um canal (ou faz downmix se canal_idx is None) pra wav 16kHz mono."""
    if canal_idx is None:
        filtro = ["-ac", "1"]
    else:
        filtro = ["-filter_complex", f"pan=mono|c0=c{canal_idx}"]
    r = _run(
        ["ffmpeg", "-y", "-i", path, *filtro, "-ar", "16000", saida],
        timeout_s=900,
    )
    if r.returncode != 0 or not os.path.exists(saida):
        raise HTTPException(status_code=422,
                            detail="audio invalido: ffmpeg nao conseguiu extrair o canal")


def _fontes(path: str):
    """Retorna [(rotulo, wav_path), ...] conforme o numero de canais."""
    ch = _num_canais(path)
    fontes = []
    if ch >= 2:
        for idx, rotulo in ((0, "Voce"), (1, "Interlocutor")):
            out = f"{path}.{idx}.wav"
            _extrair(path, idx, out)
            fontes.append((rotulo, out))
    else:
        out = f"{path}.0.wav"
        _extrair(path, None, out)
        fontes.append(("Fala", out))
    return fontes


def _duracao_s(path: str) -> float:
    r = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        timeout_s=60,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _corta_bloco(wav: str, inicio: float, dur: float, saida: str):
    """Recorta [inicio, inicio+dur) do wav pra um novo wav 16kHz mono."""
    _run(
        ["ffmpeg", "-y", "-ss", str(inicio), "-t", str(dur), "-i", wav,
         "-ar", "16000", "-ac", "1", saida],
        timeout_s=300,
    )


def _transcrever_fonte(rotulo: str, wav: str):
    """Transcreve um canal em blocos de BLOCO_S, um por vez, pra nao carregar a
    reuniao inteira na memoria de uma vez. Devolve (idioma, [segmentos]) com os
    tempos ja deslocados pro tempo real do audio."""
    dur_total = _duracao_s(wav)
    offset = 0.0
    idioma = None
    segs_out = []
    # audio curto (<= 1 bloco) cai no while uma vez so: mesmo comportamento de antes
    while offset < dur_total or (dur_total == 0.0 and offset == 0.0):
        bloco_dur = min(BLOCO_S, dur_total - offset) if dur_total else BLOCO_S
        bloco = f"{wav}.b{int(offset)}.wav"
        _corta_bloco(wav, offset, bloco_dur, bloco)
        try:
            segs, info = model.transcribe(bloco, language=IDIOMA, vad_filter=True)
            idioma = info.language
            for s in segs:
                txt = s.text.strip()
                if txt:
                    segs_out.append({
                        "inicio": round(s.start + offset, 2),
                        "fim": round(s.end + offset, 2),
                        "falante": rotulo,
                        "texto": txt,
                    })
        finally:
            try:
                os.remove(bloco)
            except OSError:
                pass
        if not dur_total:
            break
        offset += bloco_dur
    return idioma, segs_out


def _transcrever_arquivo(src: str):
    """Nucleo compartilhado pelos dois modos: separa os canais (ou faz downmix
    quando e mono, caso do gravador do celular), transcreve e devolve
    (idioma, segmentos ordenados no tempo)."""
    segmentos = []
    idioma_detectado = None
    for rotulo, wav in _fontes(src):
        idioma, segs = _transcrever_fonte(rotulo, wav)
        if idioma:
            idioma_detectado = idioma
        segmentos.extend(segs)
        try:
            os.remove(wav)
        except OSError:
            pass
    segmentos.sort(key=lambda x: x["inicio"])
    return idioma_detectado, segmentos


def _resposta(idioma, segmentos):
    texto = "\n".join(f"{s['falante']}: {s['texto']}" for s in segmentos)
    return {
        "idioma": idioma,
        "duracao_s": round(segmentos[-1]["fim"], 1) if segmentos else 0,
        "n_segmentos": len(segmentos),
        "texto": texto,
        "segmentos": segmentos,
    }


# ---------- fila assincrona (upload do celular pela tela /reuniao do CRM) ----------
def _job_json(job_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def _job_audio(job_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{job_id}.audio")


def _ler_job(job_id: str):
    try:
        with open(_job_json(job_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _salvar_job(job_id: str, meta: dict):
    os.makedirs(JOBS_DIR, exist_ok=True)
    tmp = _job_json(job_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    os.replace(tmp, _job_json(job_id))  # troca atomica: nunca deixa json pela metade


def _apagar_job(job_id: str):
    for p in (_job_json(job_id), _job_audio(job_id)):
        try:
            os.remove(p)
        except OSError:
            pass


def _valida_ticket(job_id: str, ticket: str):
    """Ticket = '<exp>.<assinatura>', emitido pelo CRM com o UPLOAD_SECRET.
    Vale so pra ESTE job e so ate expirar, por isso pode viajar no browser: nao
    abre o servico pra mais nada, e o texto so volta pro callback do CRM.
    Validacao local (HMAC), sem consultar banco nem rede."""
    if not UPLOAD_SECRET:
        raise HTTPException(status_code=500, detail="UPLOAD_SECRET nao configurado")
    try:
        exp_s, assinatura = ticket.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=401, detail="ticket invalido")
    if exp < time.time():
        raise HTTPException(status_code=401, detail="ticket expirado")
    esperado = hmac.new(
        UPLOAD_SECRET.encode(), f"{job_id}.{exp}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(esperado, assinatura):
        raise HTTPException(status_code=401, detail="ticket invalido")


def _callback(meta: dict, evento: str, **campos):
    """Avisa o CRM (started/done/error). Sem requests no container: urllib basta.
    3 tentativas, porque perder o 'done' significa perder a transcricao inteira."""
    url = meta.get("callback_url")
    if not url:
        return
    corpo = json.dumps({"job_id": meta.get("job_id"), "evento": evento, **campos}).encode("utf-8")
    req = urllib.request.Request(
        url, data=corpo, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Callback-Token": meta.get("callback_token", "")},
    )
    for tentativa in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                r.read()
            return
        except Exception as e:  # rede, CRM reiniciando, 5xx momentaneo
            print(f"[callback] {evento} falhou ({str(e)[:120]}); "
                  f"tentativa {tentativa + 1}/3", flush=True)
            time.sleep(10)


def _executar_job(job_id: str):
    global _ocupado_desde, _teto_atual
    meta = _ler_job(job_id)
    src = _job_audio(job_id)
    if meta is None or not os.path.exists(src):
        _apagar_job(job_id)
        return
    meta["tentativas"] = meta.get("tentativas", 0) + 1
    _salvar_job(job_id, meta)
    if meta["tentativas"] > MAX_TENTATIVAS:
        # Ja tentamos e o processo morreu no meio das duas vezes: parar aqui,
        # senao o job vira um loop de restart do container.
        _callback(meta, "error",
                  erro="transcricao falhou repetidas vezes (audio invalido ou travamento)")
        _apagar_job(job_id)
        return
    with _ocupado:  # divide a vez com o /transcrever do gravador desktop
        dur = _duracao_s(src)
        # Teto proporcional: reuniao inteira demora horas em CPU, e um teto fixo
        # de 40 min faria o watchdog matar um job legitimo.
        _teto_atual = max(OCUPADO_TETO_S, int(dur * OCUPADO_FATOR) + 900)
        _ocupado_desde = time.monotonic()
        try:
            _callback(meta, "started", duracao_s=round(dur))
            idioma, segmentos = _transcrever_arquivo(src)
            r = _resposta(idioma, segmentos)
            _callback(meta, "done", texto=r["texto"], idioma=r["idioma"],
                      duracao_s=r["duracao_s"], n_segmentos=r["n_segmentos"])
        except Exception as e:
            _callback(meta, "error", erro=str(e)[:300])
        finally:
            _ocupado_desde = None
            _teto_atual = OCUPADO_TETO_S
            _apagar_job(job_id)


def _worker():
    while True:
        job_id = _fila.get()
        try:
            _executar_job(job_id)
        except Exception as e:
            print(f"[fila] job {job_id} explodiu: {str(e)[:200]}", flush=True)
        finally:
            _fila.task_done()


def _reenfileirar_pendentes():
    """No boot, jobs que ficaram no disco voltam pra fila. E o que salva a ata
    quando o watchdog derruba o processo no meio de uma fila com varios audios."""
    try:
        nomes = sorted(os.listdir(JOBS_DIR))
    except OSError:
        return
    for nome in nomes:
        if nome.endswith(".json"):
            job_id = nome[:-5]
            if os.path.exists(_job_audio(job_id)):
                _fila.put(job_id)
                print(f"[fila] job pendente reenfileirado: {job_id}", flush=True)


threading.Thread(target=_worker, daemon=True).start()
_reenfileirar_pendentes()


@app.get("/health")
def health():
    # def sincrono: roda no threadpool, responde mesmo durante uma transcricao.
    # ocupado_s = ha quantos segundos esta preso numa transcricao (0 = livre);
    # o watchdog do n8n olha esse numero, nao so se o /health responde.
    inicio = _ocupado_desde
    ocupado_s = round(time.monotonic() - inicio, 1) if inicio is not None else 0
    return {"ok": True, "modelo": MODELO, "idioma": IDIOMA,
            "ocupado": _ocupado.locked(), "ocupado_s": ocupado_s,
            "teto_s": _teto_atual, "fila": _fila.qsize()}


@app.post("/transcrever")
def transcrever(arquivo: UploadFile = File(...), x_token: str = Header(default="")):
    global _ocupado_desde, _teto_atual
    if TOKEN and x_token != TOKEN:
        raise HTTPException(status_code=401, detail="token invalido")
    if not _ocupado.acquire(blocking=False):
        raise HTTPException(status_code=503,
                            detail="transcritor ocupado com outro audio; tente de novo em alguns minutos")
    _ocupado_desde = time.monotonic()
    _teto_atual = OCUPADO_TETO_S  # sincrono: pedaco de 12 min, teto fixo basta
    try:
        suffix = os.path.splitext(arquivo.filename or "audio")[1] or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(arquivo.file, tmp)
            src = tmp.name
        try:
            idioma, segmentos = _transcrever_arquivo(src)
        finally:
            try:
                os.remove(src)
            except OSError:
                pass
        return _resposta(idioma, segmentos)
    finally:
        _ocupado_desde = None
        _ocupado.release()


@app.post("/transcrever-async")
def transcrever_async(
    arquivo: UploadFile = File(...),
    job_id: str = Form(...),
    ticket: str = Form(...),
    callback_url: str = Form(""),
    callback_token: str = Form(""),
):
    """Recebe o audio do browser, responde 202 na hora e enfileira. O celular
    so precisa aguentar o upload; a transcricao segue sozinha na VPS mesmo com
    o telefone bloqueado, e o texto volta pro CRM pelo callback."""
    if not job_id or not job_id.replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="job_id invalido")
    _valida_ticket(job_id, ticket)
    # O callback_url vem do browser, entao so aceitamos destinos conhecidos: a
    # transcricao de uma reuniao nao pode ser entregue num endereco qualquer.
    if callback_url and not any(callback_url.startswith(o) for o in CORS_ORIGINS):
        raise HTTPException(status_code=400, detail="callback_url fora das origens permitidas")
    os.makedirs(JOBS_DIR, exist_ok=True)
    with open(_job_audio(job_id), "wb") as fh:
        shutil.copyfileobj(arquivo.file, fh)  # streaming: audio grande nao vai pra memoria
    _salvar_job(job_id, {
        "job_id": job_id,
        "filename": arquivo.filename or "audio",
        "callback_url": callback_url,
        "callback_token": callback_token,
        "tentativas": 0,
        "recebido_em": time.time(),
    })
    _fila.put(job_id)
    return JSONResponse(status_code=202,
                        content={"ok": True, "job_id": job_id, "na_fila": _fila.qsize()})
