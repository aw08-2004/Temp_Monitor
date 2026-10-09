# FleetHub hub as a container image. Build context is hub/ itself:
#
#   docker build -t fleethub-hub hub
#
# **The image is the release.** HUB_UPDATE_MODE=image turns off every in-place update path
# (app.py's watcher and POST /api/hub/update, wsgi.py's boot-time self-heal): anything they
# wrote would land in this container's own layer and vanish on the next recreate, leaving the
# hub silently back on the image's version. A new hub version is a new image tag, pulled and
# recreated -- see deploy/docker/compose.yaml. tests/test_hub_update_mode.py fails if the
# ENV below goes missing.
#
# Rejected: mounting hub/ as a volume so the existing self-updater keeps working. The image
# would stop describing what runs, and pip would still write into this image's site-packages.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HUB_UPDATE_MODE=image \
    HUB_STATE_DIR=/state

# requirements.txt carries no pins, so the image build is the pin: one image, one dependency
# set, for as long as that tag is deployed. Installed before the code is copied so a code-only
# change reuses this layer.
COPY requirements.txt /app/hub/requirements.txt
RUN pip install -r /app/hub/requirements.txt

# Non-root. /state is created here and owned by the hub user, so a fresh named volume mounted
# over it inherits that ownership; .env, logs/ (the SQLite DB and spools) and data/
# (recordings) all live under it -- see app.py's STATE_ROOT. The .env must stay writable: the
# hub appends BACKUP_MASTER_KEY to it and the Settings pages write sign-in and TURN values.
RUN useradd --system --uid 10001 --home-dir /state --shell /usr/sbin/nologin fleethub \
    && mkdir -p /state \
    && chown fleethub:fleethub /state \
    && chmod 700 /state

COPY --chown=root:root . /app/hub
WORKDIR /app/hub
USER fleethub
VOLUME ["/state"]
EXPOSE 3001

# The same probe install.ps1 makes after starting the Windows service. Python rather than curl
# because the slim base has no curl, and adding one for a health check is not worth a layer.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3001/', timeout=8).status < 500 else 1)"]

# One process, many threads -- never workers. Every scheduler, the db_writer and Socket.IO's
# in-memory state assume they are alone, and a second process would run each scheduled job
# twice against the same SQLite files. 128 threads because every open console tab and every
# held agent command long-poll pins one; install.ps1's service definition explains the number.
CMD ["waitress-serve", "--host=0.0.0.0", "--port=3001", "--threads=128", "wsgi:application"]
