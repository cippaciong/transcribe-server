import hmac
import logging
import os
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated

import numpy as np
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class Settings:
    model_path: str = "/models/parakeet-tdt-0.6b-v3-Q8_0.gguf"
    backend: str = "vulkan"
    api_key: str = ""
    max_upload_bytes: int = 100 * 1024 * 1024
    max_audio_seconds: int = 600
    decode_timeout_seconds: int = 120
    threads: int = 0

    @classmethod
    def from_env(cls):
        return cls(
            model_path=os.getenv("MODEL_PATH", cls.model_path),
            backend=os.getenv("BACKEND", cls.backend),
            api_key=os.getenv("API_KEY", ""),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_MB", "100")) * 1024 * 1024,
            max_audio_seconds=int(os.getenv("MAX_AUDIO_SECONDS", "600")),
            decode_timeout_seconds=int(os.getenv("DECODE_TIMEOUT_SECONDS", "120")),
            threads=int(os.getenv("THREADS", "0")),
        )


def load_model(settings):
    # Delay native loading until startup, including ABI verification.
    from transcribe_cpp import Model

    return Model(settings.model_path, backend=settings.backend)


def decode_audio(path: Path, settings: Settings):
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(path),
                "-vn",
                "-t",
                str(settings.max_audio_seconds + 1),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "f32le",
                "pipe:1",
            ],
            capture_output=True,
            timeout=settings.decode_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(422, "Audio conversion timed out") from exc
    if result.returncode:
        raise HTTPException(422, "Unable to decode the uploaded audio")
    pcm = np.frombuffer(result.stdout, dtype="<f4").copy()
    if not pcm.size:
        raise HTTPException(422, "Audio contains no samples")
    if pcm.size > settings.max_audio_seconds * 16000:
        raise HTTPException(413, "Audio exceeds MAX_AUDIO_SECONDS")
    return pcm


def create_app(settings=None, model_factory=load_model):
    settings = settings or Settings.from_env()
    if (
        min(settings.max_upload_bytes, settings.max_audio_seconds, settings.decode_timeout_seconds)
        <= 0
        or settings.threads < 0
    ):
        raise ValueError("Limits must be positive and THREADS must be nonnegative")
    busy = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        with model_factory(settings) as model, model.session(n_threads=settings.threads) as session:
            app.state.session = session
            app.state.ready = True
            logger.info("Model loaded: %s (backend=%s)", settings.model_path, settings.backend)
            try:
                yield
            finally:
                app.state.ready = False

    app = FastAPI(title="transcribe-server", lifespan=lifespan)

    def authenticate(authorization: Annotated[str | None, Header()] = None):
        if settings.api_key and not hmac.compare_digest(
            (authorization or "").encode(), f"Bearer {settings.api_key}".encode()
        ):
            raise HTTPException(
                401, "Invalid or missing API key", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.get("/health")
    def health():
        if not getattr(app.state, "ready", False):
            raise HTTPException(503, "Model is not ready")
        return {"status": "ok"}

    def infer(path, language):
        started = time.perf_counter()
        pcm = decode_audio(path, settings)
        conversion_ms = (time.perf_counter() - started) * 1000
        result = app.state.session.run(pcm, language=language, timestamps="none")
        timings = asdict(result.timings)
        timings.pop("load_ms", None)
        logger.info(
            "Transcribed %.3fs audio: conversion=%.1fms inference=%s total=%.1fms",
            pcm.size / 16000,
            conversion_ms,
            timings,
            (time.perf_counter() - started) * 1000,
        )
        return result, pcm.size / 16000, timings

    @app.post("/v1/audio/transcriptions", dependencies=[Depends(authenticate)])
    async def transcribe(
        file: Annotated[UploadFile, File()],
        response_format: Annotated[str, Form()] = "json",
        language: Annotated[str | None, Form()] = None,
        model: Annotated[str | None, Form()] = None,
    ):
        # 'model' is accepted for clients using the OpenAI request shape. The
        # server always uses the single model selected at startup.
        if response_format not in {"json", "text", "verbose_json"}:
            raise HTTPException(400, "Supported response formats: json, text, verbose_json")
        language = None if language in {None, "", "auto"} else language
        if not busy.acquire(blocking=False):
            raise HTTPException(
                429,
                "Server is busy; retry when the current request finishes",
                headers={"Retry-After": "1"},
            )
        try:
            with tempfile.TemporaryDirectory(prefix="transcribe-") as directory:
                path = Path(directory) / "audio"
                total = 0
                with path.open("wb") as output:
                    while chunk := await file.read(1024 * 1024):
                        total += len(chunk)
                        if total > settings.max_upload_bytes:
                            raise HTTPException(413, "Upload exceeds MAX_UPLOAD_MB")
                        output.write(chunk)
                try:
                    result, duration, timings = await run_in_threadpool(infer, path, language)
                except HTTPException:
                    raise
                except Exception as exc:
                    logger.exception("Transcription failed")
                    raise HTTPException(500, "Transcription failed") from exc
            if response_format == "text":
                return PlainTextResponse(result.text)
            if response_format == "verbose_json":
                return {
                    "text": result.text,
                    "language": result.language,
                    "duration": duration,
                    "timings": timings,
                }
            return {"text": result.text}
        finally:
            await file.close()
            busy.release()

    return app


app = create_app()
