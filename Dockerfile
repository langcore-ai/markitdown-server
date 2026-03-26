FROM astral/uv:python3.13-trixie-slim
ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn
ARG UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    MARKITDOWN_UVICORN_WORKERS=2
RUN printf '%s\n' \
    'Types: deb' \
    "URIs: ${DEBIAN_MIRROR}/debian" \
    'Suites: trixie trixie-updates trixie-backports' \
    'Components: main contrib non-free non-free-firmware' \
    'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
    '' \
    'Types: deb' \
    "URIs: ${DEBIAN_MIRROR}/debian-security" \
    'Suites: trixie-security' \
    'Components: main contrib non-free non-free-firmware' \
    'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
    > /etc/apt/sources.list.d/debian.sources && \
    rm -f /etc/apt/sources.list

WORKDIR /app

# 保留 apt 下载缓存，配合 BuildKit cache mount 复用 deb 包。
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf '%s\n' 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libimage-exiftool-perl

COPY --link pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --no-dev

COPY --link main.py ./

EXPOSE 8000

CMD ["sh", "-c", "/opt/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${MARKITDOWN_UVICORN_WORKERS:-2}"]
