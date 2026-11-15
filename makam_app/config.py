import json
import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = APP_ROOT / "settings.json"

DEFAULT_MUSESCORE_PATHS = [
    r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
    r"C:\Program Files\MuseScore 4\bin\MuseScore.exe",
    r"C:\Program Files (x86)\MuseScore 4\bin\MuseScore4.exe",
]


def detect_musescore_exe() -> str:
    for p in DEFAULT_MUSESCORE_PATHS:
        if os.path.isfile(p):
            return p
    return DEFAULT_MUSESCORE_PATHS[0]


def load_settings() -> dict:
    defaults = {
        "musescore_exe": detect_musescore_exe(),
        "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
        "gemini_model": "gemini-4-hydrogen",
        "apply_53edo_tuning": True,
        "export_midi": True,
        "export_mp3": False,
        "last_output_dir": str(APP_ROOT.parent),
    }
    if SETTINGS_FILE.is_file():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            defaults.update({k: v for k, v in saved.items() if v is not None})
        except Exception:
            pass
    if not os.path.isfile(defaults["musescore_exe"]):
        defaults["musescore_exe"] = detect_musescore_exe()
    return defaults


def save_settings(data: dict) -> None:
    current = load_settings()
    current.update(data)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
