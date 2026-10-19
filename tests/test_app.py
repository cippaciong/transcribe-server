import io
import threading
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace

from fastapi.testclient import TestClient

from transcribe_server.app import Settings, create_app


@dataclass
class Timings:
    load_ms: float = 12
    mel_ms: float = 1
    encode_ms: float = 2
    decode_ms: float = 3


class FakeModel:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    @contextmanager
    def session(self, **kwargs):
        yield self

    def run(self, pcm, **kwargs):
        self.calls.append((pcm, kwargs))
        return SimpleNamespace(text="Ciao mondo", language="it", timings=Timings())


def wav(seconds=0.1):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(b"\0" * int(48000 * seconds) * 4)
    return output.getvalue()


def client(settings=None, model=None):
    model = model or FakeModel()
    return TestClient(create_app(settings or Settings(), lambda _: model)), model


def test_conversion_response_and_model_reuse():
    http, model = client()
    with http:
        assert http.get("/health").status_code == 200
        for _ in range(2):
            result = http.post(
                "/v1/audio/transcriptions",
                files={"file": ("test.wav", wav())},
                data={"language": "auto", "response_format": "verbose_json"},
            )
            assert result.status_code == 200, result.text
            assert result.json()["duration"] == 0.1
            assert result.json()["text"] == "Ciao mondo"
            assert "load_ms" not in result.json()["timings"]
        pcm, args = model.calls[-1]
        assert pcm.dtype.name == "float32"
        assert pcm.shape == (1600,)
        assert args["language"] is None


def test_authentication_and_text_response():
    http, _ = client(Settings(api_key="test-secret"))
    with http:
        assert http.post("/v1/audio/transcriptions", files={"file": wav()}).status_code == 401
        result = http.post(
            "/v1/audio/transcriptions",
            files={"file": wav()},
            headers={"Authorization": "Bearer test-secret"},
            data={"response_format": "text"},
        )
        assert result.status_code == 200
        assert result.text == "Ciao mondo"


def test_invalid_audio_and_request_limits():
    http, _ = client(Settings(max_upload_bytes=20000, max_audio_seconds=1))
    with http:
        assert http.post("/v1/audio/transcriptions", files={"file": b"invalid"}).status_code == 422
        assert (
            http.post("/v1/audio/transcriptions", files={"file": b"x" * 20001}).status_code == 413
        )
        assert (
            http.post(
                "/v1/audio/transcriptions", files={"file": wav()}, data={"response_format": "srt"}
            ).status_code
            == 400
        )
    http, _ = client(Settings(max_audio_seconds=1))
    with http:
        assert http.post("/v1/audio/transcriptions", files={"file": wav(2)}).status_code == 413


def test_concurrent_request_is_rejected_and_health_stays_responsive():
    entered, release = threading.Event(), threading.Event()

    class SlowModel(FakeModel):
        def run(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            return super().run(*args, **kwargs)

    http, _ = client(model=SlowModel())
    with http:
        responses = []
        worker = threading.Thread(
            target=lambda: responses.append(
                http.post("/v1/audio/transcriptions", files={"file": wav()})
            )
        )
        worker.start()
        try:
            assert entered.wait(5)
            assert http.get("/health").status_code == 200
            assert http.post("/v1/audio/transcriptions", files={"file": wav()}).status_code == 429
        finally:
            release.set()
            worker.join(5)
        assert responses[0].status_code == 200
