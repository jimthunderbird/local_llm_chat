"""Local text-to-speech cache and synthesis engines."""

from __future__ import annotations

from .core import *
from .core import SCRIPT_DIR

# The news modal's read-aloud button sends one article paragraph at a time to
# /api/tts. The local engine returns a small WAV so playback starts as soon as
# the first paragraph is ready while the browser prefetches the next one.
# research/text_to_speech.txt uses Kokoro; it is used when installed, with the
# operating system's voice as a fast fallback (macOS `say`, Linux espeak).
TTS_CACHE_DIR = SCRIPT_DIR / ".chat" / "tts_cache"

TTS_ENGINE = os.environ.get("TTS_ENGINE", "auto").strip().lower()

TTS_MAX_CHARS = 12_000

TTS_CACHE_LOCK = threading.Lock()

TTS_KEY_LOCKS: dict[str, threading.Lock] = {}

KOKORO_PIPELINE = None

KOKORO_PIPELINE_LOCK = threading.Lock()

KOKORO_SYNTH_LOCK = threading.Lock()

def _tts_voice(override: str = "") -> str:
    return _clean_voice_name(override) or os.environ.get("TTS_VOICE", "").strip()

def _tts_cache_path(text: str, voice: str = "") -> Path:
    voice = _tts_voice(voice) or os.environ.get("KOKORO_VOICE", "").strip()
    digest = hashlib.sha256(
        f"{TTS_ENGINE}\0{voice}\0{text}".encode("utf-8")
    ).hexdigest()
    return TTS_CACHE_DIR / f"{digest}.wav"

def _tts_key_lock(key: str) -> threading.Lock:
    with TTS_CACHE_LOCK:
        return TTS_KEY_LOCKS.setdefault(key, threading.Lock())

def _synthesize_kokoro(text: str, out_path: Path, voice: str = "") -> None:
    """Render one paragraph with the local Kokoro pipeline from the research."""
    global KOKORO_PIPELINE
    try:
        import numpy as np  # type: ignore
        from kokoro import KPipeline  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("kokoro is not installed") from exc

    with KOKORO_PIPELINE_LOCK:
        if KOKORO_PIPELINE is None:
            lang_code = os.environ.get("KOKORO_LANG_CODE", "a")
            KOKORO_PIPELINE = KPipeline(lang_code=lang_code)
        pipeline = KOKORO_PIPELINE

    voice = _tts_voice(voice) or os.environ.get("KOKORO_VOICE", "af_heart")
    parts = []
    # The pipeline owns a single model instance; serialize inference when the
    # browser prefetches the next paragraph concurrently.
    with KOKORO_SYNTH_LOCK:
        for _graphemes, _phonemes, audio in pipeline(text, voice=voice):
            samples = np.asarray(audio, dtype=np.float32).reshape(-1)
            if samples.size:
                parts.append(samples)
    if not parts:
        raise RuntimeError("Kokoro produced no audio")
    samples = np.concatenate(parts) if len(parts) > 1 else parts[0]
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24_000)
        wav.writeframes(pcm.tobytes())

def _synthesize_macos(text: str, out_path: Path, voice: str = "") -> None:
    executable = shutil.which("say")
    if not executable:
        raise RuntimeError("macOS 'say' is not available")
    command = [
        executable,
        "-o",
        str(out_path),
        "--file-format=WAVE",
        "--data-format=LEI16@22050",
    ]
    voice = _tts_voice(voice)
    if voice:
        command.extend(["-v", voice])
    command.append(text)
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=120,
    )

def _synthesize_espeak(text: str, out_path: Path, voice: str = "") -> None:
    executable = shutil.which("espeak-ng") or shutil.which("espeak")
    if not executable:
        raise RuntimeError("espeak-ng/espeak is not available")
    command = [executable, "-w", str(out_path)]
    voice = _tts_voice(voice)
    if voice:
        command.extend(["-v", voice])
    command.append(text)
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=120,
    )

def _synthesize_to_file(text: str, out_path: Path, voice: str = "") -> None:
    engine = TTS_ENGINE
    errors: list[str] = []

    if engine in {"auto", "kokoro"}:
        try:
            _synthesize_kokoro(text, out_path, voice)
            return
        except Exception as exc:
            errors.append(f"kokoro: {exc}")
            if engine == "kokoro":
                raise RuntimeError("; ".join(errors)) from exc

    if engine in {"auto", "say"}:
        if sys.platform == "darwin" or engine == "say":
            try:
                _synthesize_macos(text, out_path, voice)
                return
            except Exception as exc:
                errors.append(f"say: {exc}")

    if engine in {"auto", "espeak"}:
        try:
            _synthesize_espeak(text, out_path, voice)
            return
        except Exception as exc:
            errors.append(f"espeak: {exc}")

    detail = "; ".join(errors) or f"unknown TTS_ENGINE {engine!r}"
    raise RuntimeError(f"No local text-to-speech engine is available ({detail})")

def _prewarm_tts() -> None:
    """Load the local TTS model in the background so the first click is fast."""
    if os.environ.get("TTS_PREWARM", "1").strip() == "0":
        return
    try:
        synthesize_speech("Text to speech is ready.")
    except Exception:
        # TTS is optional; a missing engine must not affect chat startup.
        pass

def _keepalive_watchdog() -> None:
    """Refresh Ollama's keep-alive timer without generating a real answer.

    Ollama evicts a model after ``OLLAMA_KEEP_ALIVE`` without traffic. Any
    request refreshes that timer, so a one-token generation every few minutes
    keeps the resident models warm. Each request uses the same ``num_ctx`` as
    the real calls, so it never triggers a context-size reload.
    """
    while True:
        time.sleep(KEEPALIVE_WATCHDOG_INTERVAL_SECONDS)
        for model_name in chat.RESIDENT_MODELS:
            try:
                response = requests.post(
                    f"{chat.OLLAMA_HOST}/api/generate",
                    json={
                        "model": model_name,
                        "prompt": "",
                        "stream": False,
                        "think": False,
                        "keep_alive": chat.OLLAMA_KEEP_ALIVE,
                        "options": {
                            "num_predict": 1,
                            "num_ctx": chat.ANSWER_NUM_CTX,
                        },
                    },
                    timeout=(2, 60),
                )
                response.close()
            except requests.RequestException:
                # The watchdog is best-effort; a stopped Ollama simply retries.
                pass

def synthesize_speech(text: str, voice: str = "") -> bytes:
    """Return a cached WAV for one article paragraph, synthesizing if needed."""
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("Missing text to read aloud")
    if len(text) > TTS_MAX_CHARS:
        raise ValueError(
            f"That paragraph is too long to read aloud (limit "
            f"{TTS_MAX_CHARS:,} characters)."
        )

    TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _tts_cache_path(text, voice)
    with _tts_key_lock(cache_path.name):
        try:
            cached = cache_path.read_bytes()
            if len(cached) > 44:
                return cached
        except OSError:
            pass

        temp_path = cache_path.with_name(
            f".{cache_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp.wav"
        )
        try:
            _synthesize_to_file(text, temp_path, voice)
            if not temp_path.exists() or temp_path.stat().st_size <= 44:
                raise RuntimeError("The text-to-speech engine produced no audio")
            os.replace(temp_path, cache_path)
            return cache_path.read_bytes()
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .config import KEEPALIVE_WATCHDOG_INTERVAL_SECONDS
from .core import _clean_voice_name
