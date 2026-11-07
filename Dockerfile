FROM python:3.12-slim-bookworm AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/stopkadr
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd -g 10001 stopkadr \
    && useradd -u 10001 -g 10001 --no-create-home stopkadr

FROM dependencies AS test
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY app/ app/
COPY migrations/ migrations/
COPY alembic.ini ./
COPY tests/ tests/
USER 10001:10001

FROM dependencies AS runtime
COPY app/ app/
COPY migrations/ migrations/
COPY alembic.ini ./
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health/ready', timeout=4).close()"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
