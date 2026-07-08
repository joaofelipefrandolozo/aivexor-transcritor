"""
Microservico de transcricao (AIVEXOR) - roda na VPS, chamado pelo n8n.

Recebe um audio de reuniao e devolve a transcricao ja rotulada por falante,
usando a convencao de 2 canais do gravador:
  - canal L (esquerdo)  -> "Voce"          (seu microfone)
  - canal R (direito)   -> "Interlocutor"  (audio do sistema)

Se o audio for mono (ex.: gravado no celular), transcreve corrido, sem rotulo.
Usa VAD pra pular silencios (cada canal so processa quando ha voz).

Endpoints:
  GET  /health       -> status + modelo carregado
  POST /transcrever  -> multipart 'arquivo'; header 'X-Token' se TRANSCRITOR_TOKEN setado
"""
import os
import subprocess
import tempfile

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from faster_whisper import WhisperModel

MODELO = os.environ.get("WHISPER_MODEL", "medium")
TOKEN = os.environ.get("TRANSCRITOR_TOKEN", "")
THREADS = int(os.environ.get("WHISPER_THREADS", "0"))  # 0 = automatico
IDIOMA = os.environ.get("WHISPER_LANG", "pt")

app = FastAPI(title="AIVEXOR Transcritor")
model = WhisperModel(MODELO, device="cpu", compute_type="int8", cpu_threads=THREADS)


def _num_canais(path: str) -> int:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of",
         "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
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
    subprocess.run(
        ["ffmpeg", "-y", "-i", path, *filtro, "-ar", "16000", saida],
        capture_output=True,
    )


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


@app.get("/health")
def health():
    return {"ok": True, "modelo": MODELO, "idioma": IDIOMA}


@app.post("/transcrever")
async def transcrever(arquivo: UploadFile = File(...), x_token: str = Header(default="")):
    if TOKEN and x_token != TOKEN:
        raise HTTPException(status_code=401, detail="token invalido")

    suffix = os.path.splitext(arquivo.filename or "audio")[1] or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await arquivo.read())
        src = tmp.name

    segmentos = []
    idioma_detectado = None
    try:
        for rotulo, wav in _fontes(src):
            segs, info = model.transcribe(wav, language=IDIOMA, vad_filter=True)
            idioma_detectado = info.language
            for s in segs:
                txt = s.text.strip()
                if txt:
                    segmentos.append({
                        "inicio": round(s.start, 2),
                        "fim": round(s.end, 2),
                        "falante": rotulo,
                        "texto": txt,
                    })
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
