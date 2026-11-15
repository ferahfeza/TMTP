import json
import os
import shutil
import sys
from pathlib import Path

IS_FROZEN: bool = bool(getattr(sys, "frozen", False))
BUNDLE_DIR: Path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
APP_ROOT: Path = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).resolve().parent.parent


def _resolve_settings_file() -> Path:
    if not IS_FROZEN:
        return APP_ROOT / "settings.json"
    portable_cfg = APP_ROOT / "settings.json"
    if portable_cfg.is_file():
        return portable_cfg
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home()
    return base / "TMTP" / "settings.json"


SETTINGS_FILE: Path = _resolve_settings_file()


def _candidate_musescore_paths() -> list[str]:
    paths = [
        r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
        r"C:\Program Files\MuseScore 4\bin\MuseScore.exe",
        r"C:\Program Files (x86)\MuseScore 4\bin\MuseScore4.exe",
    ]
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        paths.append(os.path.join(local_app, "Programs", "MuseScore 4", "bin", "MuseScore4.exe"))
    prog_w64 = os.environ.get("ProgramW6432")
    if prog_w64:
        p = os.path.join(prog_w64, "MuseScore 4", "bin", "MuseScore4.exe")
        if p not in paths:
            paths.append(p)
    return paths


DEFAULT_MUSESCORE_PATHS = _candidate_musescore_paths()


def detect_musescore_exe() -> str:
    for p in _candidate_musescore_paths():
        if os.path.isfile(p):
            return p

    for name in ("MuseScore4.exe", "MuseScore4", "MuseScore.exe"):
        found = shutil.which(name)
        if found and os.path.isfile(found):
            return found

    if sys.platform == "win32":
        try:
            import winreg

            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for subkey in (
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\MuseScore4.exe",
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\MuseScore.exe",
                ):
                    try:
                        with winreg.OpenKey(root, subkey) as k:
                            val, _ = winreg.QueryValueEx(k, "")
                            if val and os.path.isfile(val):
                                return str(val)
                    except OSError:
                        continue
        except Exception:
            pass

    return DEFAULT_MUSESCORE_PATHS[0]


def _default_output_dir() -> str:
    if IS_FROZEN:
        docs = Path.home() / "Documents"
        if docs.is_dir():
            return str(docs)
        return str(APP_ROOT)
    return str(APP_ROOT.parent)


def load_settings() -> dict:
    defaults = {
        "musescore_exe": detect_musescore_exe(),
        "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
        "gemini_model": "gemini-4-hydrogen",
        "apply_53edo_tuning": True,
        "export_midi": False,
        "export_mp3": False,
        "last_output_dir": _default_output_dir(),
    }
    cfg_path = _resolve_settings_file()
    if cfg_path.is_file():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
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
    cfg_path = _resolve_settings_file()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
