# The archive jobs as a container image.
#
# This exists for deployments where the jobs are scheduled by something other
# than systemd on the host paperless runs on. The image holds no credentials and
# no configuration: both arrive at runtime, through the environment and through
# a config file mounted at $PAPERLESS_ARCHIVE_CONFIG.

FROM ghcr.io/astral-sh/uv:0.11.15-python3.13-trixie-slim AS build

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app

# Dependencies first, from the lockfile, so a source change does not invalidate
# the layer that takes the time to build.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-editable --no-dev

ADD . /app

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --no-dev

FROM python:3.13-slim-trixie AS runtime

LABEL org.opencontainers.image.title="paperless-s3-archiver"
LABEL org.opencontainers.image.description="Write paperless-ngx documents to a WORM object store under Object Lock"
LABEL org.opencontainers.image.source="https://github.com/climate-resource/paperless-s3-archiver"
LABEL org.opencontainers.image.licenses="Apache-2.0"

ENV PATH="/app/.venv/bin:${PATH}"

# Nothing here needs root. The archive credentials are the most sensitive thing
# this process holds, so it holds them as nobody in particular.
RUN useradd --system --create-home --uid 10001 archiver
USER 10001

COPY --from=build /app/.venv /app/.venv

ENTRYPOINT ["paperless-archive"]
CMD ["--help"]
