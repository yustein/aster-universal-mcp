FROM python:3.12.14-slim-bookworm AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE requirements-archive-lock.txt ./
COPY *.py ./
COPY aster_archive ./aster_archive
COPY deploy/archive/backup_state.py ./deploy/archive/backup_state.py
COPY deploy/archive/archive_operator.py ./deploy/archive/archive_operator.py
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r requirements-archive-lock.txt \
    && /opt/venv/bin/pip install --no-deps .

FROM python:3.12.14-slim-bookworm
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install --no-install-recommends -y libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 aster-archive \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin aster-archive \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/aster-archive /var/lib/aster-archive-auth /var/lib/aster-archive-models
COPY --from=build /opt/venv /opt/venv
COPY --from=build --chown=10001:10001 /app /app
WORKDIR /app
USER 10001:10001
CMD ["python", "-m", "aster_archive.service"]
