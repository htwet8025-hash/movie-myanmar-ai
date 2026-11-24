FROM python:3.10-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn python-telegram-bot==20.7 edge-tts python-dotenv python-multipart aiofiles requests google-generativeai

COPY . .

EXPOSE 8000

CMD ["uvicorn", "backend:app", "--host", "0.0.0.0", "--port", "8000"]
