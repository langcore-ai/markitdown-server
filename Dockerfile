FROM docker.1ms.run/python:3.13-slim
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

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
RUN uv sync --no-dev

COPY main.py ./

EXPOSE 8000

CMD ["sh", "-c", "/opt/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${MARKITDOWN_UVICORN_WORKERS:-2}"]
