"""The sandbox base image.

Built once and reused across runs. Baking pytest and git in means a run's setup
is only the target repo's own dependencies, which is the difference between a
demo that starts in seconds and one that starts in a minute.
"""

from __future__ import annotations

import asyncio
import io
import logging

log = logging.getLogger(__name__)

#: Runs as a non-root user with its own virtualenv, so `pip install` needs no
#: write access to system site-packages and a compromised process is unprivileged.
#: git is present for the snapshot mechanism (see runner.py), not for the agent.
DOCKERFILE = """\
FROM python:3.13-slim

RUN apt-get update \\
 && apt-get install -y --no-install-recommends git \\
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /bin/bash sandbox

RUN python -m venv /venv \\
 && /venv/bin/pip install --no-cache-dir --upgrade pip \\
 && /venv/bin/pip install --no-cache-dir pytest \\
 && chown -R sandbox:sandbox /venv

ENV PATH=/venv/bin:$PATH \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN mkdir -p /work /snapshot.git \\
 && chown sandbox:sandbox /work /snapshot.git

USER sandbox
WORKDIR /work
"""


def _build_sync(tag: str) -> None:
    import docker

    client = docker.from_env()
    try:
        client.images.get(tag)
        log.debug("sandbox image %s already present", tag)
        return
    except docker.errors.ImageNotFound:
        pass

    log.info("building sandbox image %s (first run only)", tag)
    _image, logs = client.images.build(
        fileobj=io.BytesIO(DOCKERFILE.encode()),
        tag=tag,
        rm=True,
        pull=False,
    )
    for chunk in logs:
        if isinstance(chunk, dict) and (stream := str(chunk.get("stream", "")).strip()):
            log.debug("docker build: %s", stream)


async def ensure_image(tag: str) -> None:
    """Build the base image unless it already exists.

    Runs in a thread: the docker SDK is synchronous and a build takes tens of
    seconds, which would otherwise block the event loop and stall the SSE stream.
    """
    await asyncio.to_thread(_build_sync, tag)
