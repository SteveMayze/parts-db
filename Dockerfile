# Production image. Built on the server, as the rootless service user, from the committed revision (git archive).
FROM python:3.12-slim

ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.title="parts-db" \
      org.opencontainers.image.revision="${GIT_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Unprivileged user. The mounted data folders on the host belong to the service user, which keep-id maps to this uid.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --no-compile -r requirements.txt

# Application code only (no docs, infra, local backups or databases). Files stay root-owned and read-only for the app.
COPY manage.py healthcheck.py ./
COPY config ./config
COPY parts ./parts
COPY parsers ./parsers
# Collect the stylesheet for WhiteNoise at build time (development settings are enough: nothing secret is read).
RUN python manage.py collectstatic --noinput

USER 10001:10001
EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--worker-tmp-dir", "/dev/shm", "--access-logfile", "-", "--error-logfile", "-"]
