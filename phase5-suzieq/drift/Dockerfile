# Phase 5 Part B drift harness sibling container.
#
# Sibling to sq-poller / sq-coalescer / sq-rest-server in the
# same docker-compose.yml. Mounts the suzieq_parquet docker volume
# READ-ONLY (the drift harness only reads parquet, never writes
# back) and the drift code as a separate read-only mount so
# iteration during development does not need an image rebuild.
#
# Why a separate sibling and NOT a child of netenglabs/suzieq:
#   - The drift harness uses pyarrow directly, never imports the
#     suzieq python package - so the 781 MB suzieq base image is
#     dead weight.
#   - Decoupled from the suzieq image upgrade cadence - we bump
#     the suzieq pin in docker-compose.yml without rebuilding the
#     drift image, and vice versa.
#   - The drift harness is invoked as a one-shot CLI tool
#     (`docker compose run --rm drift python -m drift.cli ...`),
#     not a long-running daemon. Sibling-container-as-CLI is the
#     idiomatic compose pattern for this shape.
#
# Build from phase5-suzieq/ directory:
#   docker build -f drift/Dockerfile -t evpn-lab/phase5-drift:dev drift/
#
# Or via the compose file (drift service has `build:` set):
#   docker compose build drift

FROM python:3.11-slim

# Install runtime deps as a single layer. No --user, no venv -
# the container is single-purpose and short-lived.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Drift code lives at /drift inside the container, mounted from
# the host phase5-suzieq/drift/ directory. Set PYTHONPATH so
# `python -m drift.cli` resolves the package without us having
# to ship a setup.py or pyproject.toml.
ENV PYTHONPATH=/

WORKDIR /

# Run as a non-root user. uid 1000 matches the suzieq image's
# `suzieq` user (so the read-only volume mount permissions line
# up cleanly without needing :ro or chown gymnastics inside the
# drift container).
RUN useradd --uid 1000 --create-home drift
USER drift

# Default invocation prints the JSON drift report. Override at
# `docker compose run` time for --human or other flags.
ENTRYPOINT ["python", "-m", "drift.cli"]
CMD ["--json"]
