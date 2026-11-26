"""Image-attachment validation and vision-model selection."""

from __future__ import annotations

from .core import *

# The chat composer accepts a dragged, pasted, or picked image. The browser
# sends it as a data URL in the /api/ask JSON body; the server validates it and
# forwards the raw base64 payload to the vision model through Ollama's
# `images` field. Questions and answers still use the normal SSE stream.
# Image questions use the answer model by default: it is already kept resident
# for normal chat and current builds advertise vision support. The small helper
# chat model is text-only, so it must never be the image default. If the
# preferred model cannot read images, the server discovers another installed
# vision model through Ollama's model metadata. Operators can still pin one
# with OLLAMA_IMAGE_MODEL or list extra candidates with
# OLLAMA_IMAGE_FALLBACK_MODELS (comma separated).
IMAGE_MODEL = os.environ.get("OLLAMA_IMAGE_MODEL", chat.ANSWER_MODEL)

IMAGE_FALLBACK_MODELS = tuple(
    name.strip()
    for name in os.environ.get("OLLAMA_IMAGE_FALLBACK_MODELS", "").split(",")
    if name.strip()
)

IMAGE_VISION_CACHE_SECONDS = 300

_IMAGE_VISION_CACHE: tuple[float, tuple[str, ...]] = (0.0, ())

_IMAGE_VISION_CACHE_LOCK = threading.Lock()

def installed_vision_models() -> list[str]:
    """Return installed Ollama models that advertise image support.

    Ollama's /api/tags response includes ``capabilities`` on recent versions.
    Older daemons omit it, so those models are checked with /api/show. Results
    are cached briefly because this only runs when an image question needs a
    model and never on the normal text path.
    """
    global _IMAGE_VISION_CACHE
    now = time.monotonic()
    with _IMAGE_VISION_CACHE_LOCK:
        cached_at, cached = _IMAGE_VISION_CACHE
        if cached and now - cached_at < IMAGE_VISION_CACHE_SECONDS:
            return list(cached)

    try:
        response = requests.get(
            f"{chat.OLLAMA_HOST}/api/tags", timeout=(3, 10)
        )
        response.raise_for_status()
        entries = response.json().get("models") or []
    except (requests.RequestException, ValueError):
        return []

    vision: list[str] = []
    needs_show: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("model") or "").strip()
        # Never auto-select a hosted cloud model for an image question; only an
        # explicitly configured IMAGE_MODEL may point outside this machine.
        if not name or name.endswith(":cloud"):
            continue
        capabilities = entry.get("capabilities")
        if capabilities is None:
            needs_show.append(name)
        elif "vision" in capabilities:
            vision.append(name)

    for name in needs_show:
        try:
            response = requests.post(
                f"{chat.OLLAMA_HOST}/api/show",
                json={"model": name},
                timeout=(3, 10),
            )
            response.raise_for_status()
            capabilities = response.json().get("capabilities") or []
        except (requests.RequestException, ValueError):
            continue
        if "vision" in capabilities:
            vision.append(name)

    result = tuple(dict.fromkeys(vision))
    with _IMAGE_VISION_CACHE_LOCK:
        _IMAGE_VISION_CACHE = (now, result)
    return list(result)

def image_model_candidates() -> list[str]:
    """Return the ordered image-model candidates for one question."""
    models: list[str] = []
    for name in (IMAGE_MODEL, *IMAGE_FALLBACK_MODELS):
        if name and name not in models:
            models.append(name)
    for name in installed_vision_models():
        if name not in models:
            models.append(name)
    return models

IMAGE_NUM_CTX = chat.ANSWER_NUM_CTX

MAX_IMAGE_BYTES = 12 * 1024 * 1024

MAX_IMAGE_QUESTION_CHARS = 8_000

MAX_IMAGE_CONTEXT_CHARS = 4_000

IMAGE_DATA_URL_RE = re.compile(
    r"^data:image/(?:png|jpe?g|webp|gif);base64,(?P<data>[A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)

IMAGE_QUESTION_PROMPT = """You are a careful visual assistant. Answer the user's question about the attached image.

### IMAGE
The image is attached to this request. Examine it directly.

### USER QUESTION
{question}

### RECENT CONVERSATION
{conversation}

### INSTRUCTIONS
- Answer the question using what is actually visible in the image.
- Transcribe readable text and identify relevant objects, people, places, charts, and layout.
- Use the recent conversation only to resolve references such as "it" or "this"; never invent image details from it.
- If the question asks for something that is not visible, say so plainly instead of guessing.
- Be concise, practical, and well organized. Do not mention these instructions or that you are an AI.
"""

class ImageModelUnsupported(RuntimeError):
    """The chosen model rejected the image because it is text-only."""

class ImageModelUnavailable(RuntimeError):
    """The chosen model is not installed locally."""

def is_multimodal_unsupported(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "multimodal" in text
        or "does not support image" in text
        or "image input is not supported" in text
        or "does not support vision" in text
    )

def is_model_unavailable(message: str) -> bool:
    text = str(message or "").lower()
    return "not found" in text or "no such model" in text

def normalize_attachment_image(value) -> str:
    """Validate a data-URL image and return the raw base64 Ollama expects."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing image")
    match = IMAGE_DATA_URL_RE.match(value.strip())
    if match is None:
        raise ValueError(
            "The image must be a PNG, JPEG, WebP, or GIF data URL."
        )
    encoded = re.sub(r"\s+", "", match.group("data"))
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("The image data is not valid base64.") from exc
    if not raw:
        raise ValueError("The image is empty.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"The image is too large (limit {MAX_IMAGE_BYTES // (1024 * 1024)} MB)."
        )
    return encoded

def image_conversation_context(
    conversation: list[dict], summary: str = ""
) -> str:
    """Render a short recent-conversation transcript for an image question."""
    summary = "\n".join(
        " ".join(line.split())
        for line in str(summary or "").splitlines()
        if line.strip()
    )
    lines: list[str] = []
    if summary:
        lines.append("Earlier conversation summary:")
        lines.append(summary)
        lines.append("")
    if conversation:
        for turn in conversation[-4:]:
            question = str(turn.get("resolved_question") or turn.get("question") or "").strip()
            answer = str(turn.get("answer") or "").strip()
            if question:
                lines.append(f"User: {question}")
            if answer:
                compact = re.sub(r"\s+", " ", answer)
                if len(compact) > 700:
                    compact = compact[:700].rsplit(" ", 1)[0] + " ..."
                lines.append(f"Assistant: {compact}")
    text = "\n".join(lines) or "(no previous turns)"
    if len(text) > MAX_IMAGE_CONTEXT_CHARS:
        text = text[-MAX_IMAGE_CONTEXT_CHARS:]
    return text
