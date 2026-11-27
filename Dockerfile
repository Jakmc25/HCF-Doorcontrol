FROM python:3.13-slim-bookworm
# tzdata: the scheduler needs time zone data (zoneinfo); the build fails here rather than at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/* \
    && python -c "from zoneinfo import ZoneInfo; ZoneInfo('America/New_York')" \
    && useradd --uid 10001 --create-home doors
WORKDIR /app
COPY run.py manage.py VERSION ./
COPY doorcontrol ./doorcontrol
RUN mkdir /data && chown doors:doors /data
USER doors
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8480
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8480/health',timeout=4)"
CMD ["python", "run.py"]
