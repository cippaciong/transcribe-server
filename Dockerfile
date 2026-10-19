FROM ghcr.io/astral-sh/uv:0.13.1 AS uv

FROM ubuntu:24.04 AS native
ARG TRANSCRIBE_REV=1dc22facfc931e2f8adb43924b0e38b9c46f83c7
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git build-essential cmake ninja-build libvulkan-dev glslc \
    spirv-headers libopenblas-dev && rm -rf /var/lib/apt/lists/*
RUN git init /source && cd /source && \
    git remote add origin https://github.com/handy-computer/transcribe.cpp.git && \
    git fetch --depth 1 origin ${TRANSCRIBE_REV} && git checkout --detach FETCH_HEAD
RUN cmake -S /source -B /build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/transcribe \
    -DCMAKE_INSTALL_LIBDIR=lib -DTRANSCRIBE_BUILD_SHARED=ON \
    -DTRANSCRIBE_VULKAN=ON -DTRANSCRIBE_GGML_BACKEND_DL=OFF \
    -DTRANSCRIBE_BUILD_TESTS=OFF -DTRANSCRIBE_BUILD_EXAMPLES=OFF -DBLA_VENDOR=OpenBLAS \
    -DGGML_NATIVE=OFF -DGGML_AVX=ON -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON && \
    cmake --build /build --parallel 4 && cmake --install /build

FROM ubuntu:24.04 AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates python3 ffmpeg libvulkan1 mesa-vulkan-drivers libopenblas0-pthread \
    libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=native /opt/transcribe /opt/transcribe
COPY --from=native /source/LICENSE /usr/share/licenses/transcribe/LICENSE
COPY --from=native /source/ggml/LICENSE /usr/share/licenses/ggml/LICENSE
ENV TRANSCRIBE_LIBRARY=/opt/transcribe/lib/libtranscribe.so \
    LD_LIBRARY_PATH=/opt/transcribe/lib \
    UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    PYTHONPATH=/app/src PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Git is only needed to install the revision-pinned Python binding.
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    uv sync --frozen --no-dev --python /usr/bin/python3 && \
    apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY src ./src
RUN useradd --uid 10001 --create-home app
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]
CMD ["/app/.venv/bin/uvicorn", "transcribe_server.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
