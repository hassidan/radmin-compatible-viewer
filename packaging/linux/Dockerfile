FROM python:3.12-slim-bookworm AS builder-deps
ENV DEBIAN_FRONTEND=noninteractive QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    binutils dpkg-dev libgl1 libegl1 libopengl0 libglib2.0-0 libdbus-1-3 \
    libfontconfig1 libfreetype6 libx11-6 libx11-xcb1 libxcb1 libxcb-cursor0 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-shape0 \
    libxcb-xfixes0 libxcb-randr0 libxcb-sync1 libxcb-xinerama0 libxcb-xkb1 \
    libxkbcommon0 libxkbcommon-x11-0 libsm6 libice6 libxext6 libxrender1 \
    libwayland-client0 libwayland-cursor0 libwayland-egl1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /source
COPY requirements-build.txt .
RUN python -m pip install --no-cache-dir -r requirements-build.txt
FROM builder-deps AS release
COPY . .
CMD ["python", "scripts/build.py", "--platform", "linux", "--format", "all", "--output", "/output"]
