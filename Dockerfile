FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# CPU-only torch keeps the image several GB smaller than the default CUDA build
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# bake the whisper model into the image so the first request doesn't
# spend time downloading 461MB at runtime
RUN python -c "import whisper; whisper.load_model('small')"

COPY . .

CMD ["python", "bot/main.py"]
