FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SQA_DATA_DIR=/srv/data \
    SQA_RUNS_DIR=/srv/runs

# ffmpeg 供 video 后端（采集卡）截图使用
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY sqa ./sqa
COPY agent ./agent
COPY webapp ./webapp
COPY run_agent.py .
COPY streamlit_app.py .

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

ENTRYPOINT ["streamlit", "run", "streamlit_app.py", \
    "--server.address=0.0.0.0", "--server.port=8501", \
    "--server.headless=true", "--browser.gatherUsageStats=false"]
