# Production image. Built on the server, as the rootless service user, from the committed revision (git archive).
# Debian 13 (trixie), like the server: its own repository carries PostgreSQL 17, so the client tools below need no extra
# apt source. (python:3.12-slim without the suffix may follow a newer or older Debian release over time.)
FROM python:3.12-slim-trixie

ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.title="parts-db" \
      org.opencontainers.image.revision="${GIT_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Unprivileged user. The mounted data folders on the host belong to the service user, which keep-id maps to this uid.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app

# PostgreSQL client tools for pg_dump / pg_restore (backups). They must be the same major version as the server or newer
# (pg_dump refuses a newer server); the platform runs PostgreSQL 17. The build fails here, loudly, if the tools are
# missing or older than 17. psycopg ships its own libpq (psycopg[binary]), so libpq5 is not needed for the app itself.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/* \
    && pg_dump --version | grep -E ' (1[7-9]|[2-9][0-9])[. ]' \
    && pg_restore --version | grep -E ' (1[7-9]|[2-9][0-9])[. ]'

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

# Shown in the manifest of every backup.
ENV DJANGO_APP_COMMIT=${GIT_COMMIT}

USER 10001:10001
EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--worker-tmp-dir", "/dev/shm", "--access-logfile", "-", "--error-logfile", "-"]
