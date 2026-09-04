FROM debian:bookworm-slim

LABEL maintainer="Code-Leafy" \
      description="Leafy unified Codespace"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TERM=xterm-256color \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash curl ca-certificates sudo openssl uuid-runtime tmux tzdata \
        qrencode jq locales python3 python3-venv python3-pip \
    && sed -i 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Debian bookworm ships PEP 668 externally-managed Python, which blocks
# system-wide pip installs. Install into a virtualenv and put it on PATH so
# `python` / `pip` resolve inside the venv (Codespaces recovery-mode fix).
RUN python3 -m venv /opt/leafy-venv \
    && /opt/leafy-venv/bin/pip install --no-cache-dir \
        fastapi==0.141.1 \
        "uvicorn[standard]==0.52.4" \
        httpx==0.28.1 \
        psutil==7.2.2

ENV PATH="/opt/leafy-venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/leafy-venv

EXPOSE 8080
