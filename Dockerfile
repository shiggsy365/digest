FROM python:3.12-slim

ARG UID=1000
ARG GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

RUN groupadd --gid "${GID}" digest \
    && useradd --uid "${UID}" --gid "${GID}" --create-home digest \
    && python -m venv /opt/venv

COPY pyproject.toml ./
COPY digest ./digest
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir . \
    && mkdir -p /data /library \
    && chown -R digest:digest /app /data /library

USER digest

EXPOSE 8000

CMD ["gunicorn", "digest.main:app", "--bind", "0.0.0.0:8000", "--worker-class", "uvicorn.workers.UvicornWorker", "--workers", "2", "--timeout", "120", "--access-logfile", "-"]
