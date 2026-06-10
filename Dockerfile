FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    pandoc \
    libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
    libcairo2 libgdk-pixbuf-xlib-2.0-0 \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY codex/ codex/
COPY static/ static/

RUN mkdir -p workspace

EXPOSE 8000
CMD ["uvicorn", "codex.main:app", "--host", "0.0.0.0", "--port", "8000"]
