# transcribe-server

An HTTP server for [transcribe.cpp](https://github.com/handy-computer/transcribe.cpp),
using its Python bindings. It keeps one model and session loaded and accepts
multipart audio uploads at `/v1/audio/transcriptions`. FFmpeg converts uploaded
WAV, OGG, MP3, and other supported audio to 16 kHz mono PCM.

The default model is Parakeet TDT 0.6B v3 Q8_0, supporting 25 European languages,
including Italian. The [model weights](https://huggingface.co/handy-computer/parakeet-tdt-0.6b-v3-gguf)
are licensed CC-BY-4.0 and derive from NVIDIA's Parakeet model.
The Docker image includes Vulkan and Mesa's AMD drivers.
The host needs a working GPU kernel driver and `/dev/dri`. The image targets
Linux x86-64 CPUs with AVX2, such as the Ryzen 5 5600GE.

## Published Docker image

Pushes to `main` build and publish the Linux x86-64 image to GitHub Container
Registry with two tags:

- `ghcr.io/cippaciong/transcribe-server:latest`
- `ghcr.io/cippaciong/transcribe-server:<full-commit-sha>`

The **Build and publish Docker image** workflow can also be run manually on
`main`. It uses the built-in `GITHUB_TOKEN`; no additional secrets are needed.
After the first publication, change the package visibility to **Public** in its
GitHub package settings to allow anonymous pulls.

## Run with Docker Compose

Create the model directory:

```sh
mkdir -p volumes/models
```

Download the model:

```sh
curl -fL --retry 3 \
  -o volumes/models/parakeet-tdt-0.6b-v3-Q8_0.gguf \
  https://huggingface.co/handy-computer/parakeet-tdt-0.6b-v3-gguf/resolve/main/parakeet-tdt-0.6b-v3-Q8_0.gguf
```

Create the local environment file:

```sh
cp .env.example .env
```

Find the render device's group ID:

```sh
stat -c '%g' /dev/dri/renderD128
```

Set `RENDER_GID` in `.env` to that number. This gives the unprivileged container
user access to the render device. Optionally set `API_KEY` to require a bearer
key. Generate a key with:

```sh
openssl rand -hex 32
```

Build and start:

```sh
docker compose up -d --build
```

Watch model loading and request timings:

```sh
docker compose logs -f transcribe-server
```

Check readiness:

```sh
curl --fail http://127.0.0.1:8080/health
```

The example publishes only to localhost. To expose it through Tailscale Serve,
put the Tailscale container on the same Compose network and use
`http://transcribe-server:8080` as its proxy target.

## Transcribe

```sh
curl --fail http://127.0.0.1:8080/v1/audio/transcriptions \
  -F 'file=@recording.wav' \
  -F 'response_format=json'
```

If `API_KEY` is configured, include `-H "Authorization: Bearer <your-key>"`.
Without it, the server does not require a key. Health checks remain unauthenticated.

Supported form fields:

| Field | Behavior |
| --- | --- |
| `file` | Required audio upload |
| `response_format` | `json` (default), `text`, or `verbose_json` |
| `language` | Omit or use `auto` for model-default multilingual behavior; a language hint is passed to the binding when provided |
| `model` | Accepted for client compatibility; the loaded model is selected at startup |

JSON returns `{"text": "..."}`. `verbose_json` also returns language, audio duration
in seconds, and native `mel_ms`, `encode_ms`, and `decode_ms` timings. These native
timings exclude model loading and HTTP/audio conversion. This is a subset of the
OpenAI transcription request format: translation, streaming, SRT/VTT, and timestamp
output are not implemented. Model-specific behavior determines whether a language
hint has an effect and whether a detected language is returned.

The server processes one request at a time. Concurrent transcription requests
receive `429` with `Retry-After: 1`; health checks stay responsive. Use one Uvicorn
worker to avoid loading extra copies of the model.

## Configuration

| Environment variable | Default |
| --- | --- |
| `MODEL_PATH` | `/models/parakeet-tdt-0.6b-v3-Q8_0.gguf` |
| `BACKEND` | `vulkan` (fails startup if unavailable; use `cpu` for CPU inference) |
| `API_KEY` | Empty; authentication disabled |
| `THREADS` | `0` (binding default) |
| `MAX_UPLOAD_MB` | `100` |
| `MAX_AUDIO_SECONDS` | `600` |
| `DECODE_TIMEOUT_SECONDS` | `120` |

Add settings to the Compose `environment` section as needed. For a CPU-only host,
set `BACKEND: cpu` and remove `devices` and `group_add` from Compose.

## Development with uv

The Python binding and native library must use the same source revision.
`pyproject.toml` pins the binding; the Dockerfile pins the native build. Update
both together, then regenerate `uv.lock`. The native provider dependency is
excluded because this project builds the native library itself.

For local development, build a shared transcribe.cpp library from the pinned
revision (with `TRANSCRIBE_BUILD_SHARED=ON` and optionally `TRANSCRIBE_VULKAN=ON`).
Install FFmpeg and the native runtime dependencies, then install Python packages:

```sh
uv sync --frozen
```

Set the model and shared-library paths when starting the server:

```sh
MODEL_PATH=/absolute/path/to/model.gguf \
TRANSCRIBE_LIBRARY=/absolute/path/to/libtranscribe.so \
PYTHONPATH=src uv run uvicorn transcribe_server.app:app --host 127.0.0.1 --port 8080
```

Tests exercise real FFmpeg conversion and HTTP behavior with a fake inference
engine, so they need FFmpeg but no GPU, native library, or model:

```sh
uv run pytest
```

```sh
uv run ruff check .
```

## Benchmark

Use the same recording and model settings for each server. Run once to warm up,
then repeat with no concurrent requests:

```sh
curl --silent --show-error --fail \
  --output result.json \
  --write-out 'Total request: %{time_total} seconds\n' \
  http://127.0.0.1:8080/v1/audio/transcriptions \
  -F 'file=@recording.wav' \
  -F 'response_format=verbose_json'
```

The logs report conversion, native inference stages, and processing time;
`curl` measures the whole HTTP request. Check transcript accuracy as well as speed.
