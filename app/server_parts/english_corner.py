"""English Corner vocabulary settings and corpus word lists."""

from __future__ import annotations

from .core import *

# English Corner: /settings english_words_to_learn <range> selects the
# frequency band whose words are highlighted in news and article readers. The
# band names and range syntax come from the English Corner word finder, so the
# UI and the CLI accept exactly the same options. The setting is small and
# global to the server, so it is stored next to the other .chat state.
ENGLISH_CORNER_SETTINGS_FILE = SCRIPT_DIR / ".chat" / "english_corner_settings.json"
ENGLISH_CORNER_WORD_FINDER = (
    SCRIPT_DIR / "englishcorner" / "scripts" / "find_article_words_in_corpus.py"
)
ENGLISH_CORNER_DEFAULT_RANGE = "10k..50k"
ENGLISH_CORNER_DISABLED_VALUES = {"", "off", "none", "disabled"}
_ENGLISH_CORNER_SETTINGS_LOCK = threading.Lock()
_ENGLISH_CORNER_SETTINGS: dict[str, str] | None = None
_ENGLISH_CORNER_MODULE = None

def _load_english_corner_settings() -> dict[str, str]:
    """Read the persisted English Corner settings once per process."""
    global _ENGLISH_CORNER_SETTINGS
    with _ENGLISH_CORNER_SETTINGS_LOCK:
        if _ENGLISH_CORNER_SETTINGS is not None:
            return dict(_ENGLISH_CORNER_SETTINGS)
        settings: dict[str, str] = {}
        try:
            raw = json.loads(
                ENGLISH_CORNER_SETTINGS_FILE.read_text(encoding="utf-8")
            )
            if isinstance(raw, dict):
                settings = {
                    str(key): str(value)
                    for key, value in raw.items()
                    if isinstance(value, (str, int, float))
                }
        except (OSError, ValueError, json.JSONDecodeError):
            settings = {}
        _ENGLISH_CORNER_SETTINGS = settings
        return dict(settings)

def _save_english_corner_settings(settings: dict[str, str]) -> None:
    """Persist English Corner settings atomically inside ./.chat."""
    global _ENGLISH_CORNER_SETTINGS
    with _ENGLISH_CORNER_SETTINGS_LOCK:
        _ENGLISH_CORNER_SETTINGS = dict(settings)
        try:
            ENGLISH_CORNER_SETTINGS_FILE.parent.mkdir(
                parents=True, exist_ok=True
            )
            temporary = ENGLISH_CORNER_SETTINGS_FILE.with_name(
                ENGLISH_CORNER_SETTINGS_FILE.name + ".tmp"
            )
            temporary.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(ENGLISH_CORNER_SETTINGS_FILE)
        except OSError:
            # Highlighting still works for this process; only persistence
            # across restarts is lost.
            pass

def _english_corner_word_finder():
    """Import the English Corner word finder without installing a package."""
    global _ENGLISH_CORNER_MODULE
    if _ENGLISH_CORNER_MODULE is None:
        if not ENGLISH_CORNER_WORD_FINDER.is_file():
            raise RuntimeError(
                f"missing English Corner word finder: {ENGLISH_CORNER_WORD_FINDER}"
            )
        spec = importlib.util.spec_from_file_location(
            "english_corner_word_finder", ENGLISH_CORNER_WORD_FINDER
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load the English Corner word finder")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _ENGLISH_CORNER_MODULE = module
    return _ENGLISH_CORNER_MODULE

@lru_cache(maxsize=8)
def english_corner_vocabulary(spec: str) -> tuple[tuple[str, ...], str]:
    """Resolve a corpus name/range to a sorted vocabulary and label.

    Raises ``ValueError`` for an unknown corpus or malformed range, matching
    the CLI's error messages. Results are cached because the same browser
    session repeatedly asks for the currently selected setting.
    """
    normalized = " ".join(str(spec or "").split()).lower()
    if normalized in ENGLISH_CORNER_DISABLED_VALUES:
        return (), ""
    vocabulary, label = _english_corner_word_finder().resolve_corpus(normalized)
    return tuple(sorted(vocabulary)), label
