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
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
