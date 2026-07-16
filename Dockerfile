FROM python:3.12-slim

# ffmpeg/ffprobe pra separar canais e converter audio
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# modelo: medium (bom pra PT). Troque pra large-v3 pra mais precisao.
ENV WHISPER_MODEL=medium
ENV WHISPER_LANG=pt
# cache dos modelos baixados (monte um volume aqui pra nao rebaixar a cada restart)
ENV HF_HOME=/cache

EXPOSE 8000

# healthcheck de verdade (HTTP no /health, nao "processo existe"): o Docker/EasyPanel
# marca unhealthy se o servico travar. start-period alto: carga do modelo no boot.
HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=8).status==200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
