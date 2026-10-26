# Build the ordinary archive image first; this adds an isolated CPU runtime.
ARG ASTER_ARCHIVE_IMAGE=aster-conversation-archive:local
FROM python:3.12.14-slim-bookworm AS cognee-build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 UV_NO_CACHE=1
WORKDIR /build
COPY deploy/archive/cognee/requirements-lock.txt /build/requirements-lock.txt
RUN python -m pip install uv==0.13.4 \
    && python -m venv /opt/cognee-venv \
    && uv pip install --python /opt/cognee-venv/bin/python --torch-backend cpu -r /build/requirements-lock.txt

FROM ${ASTER_ARCHIVE_IMAGE}
COPY --from=cognee-build /opt/cognee-venv /opt/cognee-venv
ENV ASTER_ARCHIVE_COGNEE_PYTHON=/opt/cognee-venv/bin/python \
    ASTER_ARCHIVE_COGNEE_ROOT=/var/lib/aster-archive/cognee \
    ASTER_ARCHIVE_COGNEE_MODEL_ROOT=/var/lib/aster-archive-models/cognee
USER 10001:10001
# The ordinary archive interpreter owns DB receipts; only the child uses Cognee.
CMD ["python", "-m", "aster_archive.cognee_bridge", "status"]
