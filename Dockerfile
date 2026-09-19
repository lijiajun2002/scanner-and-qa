FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WEBAPP_SETTINGS=/data/settings.json

WORKDIR /srv

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY app ./app
COPY webapp ./webapp
COPY streamlit_app.py .
COPY serve.py .

EXPOSE 8501 8503
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

ENTRYPOINT ["python", "serve.py"]
