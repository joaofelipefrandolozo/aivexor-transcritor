"""
Microservico de transcricao (AIVEXOR) - roda na VPS, chamado pelo n8n.

Recebe um audio de reuniao e devolve a transcricao ja rotulada por falante,
usando a convencao de 2 canais do gravador:
  - canal L (esquerdo)  -> "Voce"          (seu microfone)
  - canal R (direito)   -> "Interlocutor"  (audio do sistema)

Se o audio for mono (ex.: gravado no celular), transcreve corrido, sem rotulo.
Usa VAD pra pular silencios (cada canal so processa quando ha voz).

Robustez (aprendida nos incidentes de 2026-07-13 e 2026-07-16):
  - endpoints sincronos (def): o FastAPI roda cada request numa thread do pool,
    entao o /health SEMPRE responde, mesmo com uma transcricao em andamento
    (antes, um request travado matava o servico inteiro pra sempre);
  - todo ffmpeg/ffprobe tem timeout: subprocess pendurado nao segura o request;
  - so 1 transcricao por vez (lock): um segundo request recebe 503 na hora em
    vez de disputar CPU/memoria e derrubar o container.

Endpoints:
  GET  /health       -> status + modelo carregado + se esta ocupado
  POST /transcrever  -> multipart 'arquivo'; header 'X-Token' se TRANSCRITOR_TOKEN setado
"""
import os
import shutil
import subprocess
import tempfile
import threading

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from faster_whisper import WhisperModel

MODELO = os.environ.get("WHISPER_MODEL", "medium")
TOKEN = os.environ.get("TRANSCRITOR_TOKEN", "")
THREADS = int(os.environ.get("WHISPER_THREADS", "0"))  # 0 = automatico
IDIOMA = os.environ.get("WHISPER_LANG", "pt")
BLOCO_S = int(os.environ.get("WHISPER_BLOCO_S", "600"))  # transcreve em blocos de N s: memoria constante mesmo em reuniao longa

app = FastAPI(title="AIVEXOR Transcritor")
model = WhisperModel(MODELO, device="cpu", compute_type="int8", cpu_threads=THREADS)
_ocupado = threading.Lock()  # 1 transcricao por vez: protege memoria/CPU da VPS


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


@app.get("/health")
def health():
    # def sincrono: roda no threadpool, responde mesmo durante uma transcricao
    return {"ok": True, "modelo": MODELO, "idioma": IDIOMA, "ocupado": _ocupado.locked()}


@app.post("/transcrever")
def transcrever(arquivo: UploadFile = File(...), x_token: str = Header(default="")):
    if TOKEN and x_token != TOKEN:
        raise HTTPException(status_code=401, detail="token invalido")
    if not _ocupado.acquire(blocking=False):
        raise HTTPException(status_code=503,
                            detail="transcritor ocupado com outro audio; tente de novo em alguns minutos")
    try:
        suffix = os.path.splitext(arquivo.filename or "audio")[1] or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(arquivo.file, tmp)
            src = tmp.name

        segmentos = []
        idioma_detectado = None
        try:
            for rotulo, wav in _fontes(src):
                idioma, segs = _transcrever_fonte(rotulo, wav)
                if idioma:
                    idioma_detectado = idioma
                segmentos.extend(segs)
                try:
                    os.remove(wav)
                except OSError:
                    pass
        finally:
            try:
                os.remove(src)
            except OSError:
                pass

        segmentos.sort(key=lambda x: x["inicio"])
        texto = "\n".join(f"{s['falante']}: {s['texto']}" for s in segmentos)
        return {
            "idioma": idioma_detectado,
            "duracao_s": round(segmentos[-1]["fim"], 1) if segmentos else 0,
            "n_segmentos": len(segmentos),
            "texto": texto,
            "segmentos": segmentos,
        }
    finally:
        _ocupado.release()
