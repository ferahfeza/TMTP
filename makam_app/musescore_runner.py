"""
MuseScore 4 CLI (MuseScore4.exe) Güvenli Senkron Çalıştırma ve Dışa Aktarma Katmanı.
- PyQt5 ile Qt6 (MuseScore 4) çakışmasını önlemek için alt süreçte QT_* ortam değişkenlerini temizler.
- Windows'ta MuseScore crashpad_handler alt sürecinin boruları (pipe) açık tutmasını önlemek için
  çıktıyı geçici dosyaya yönlendirerek anında dönüş sağlar.
- Tüm geçici .mscx dosyaları yalıtılmış geçici klasörde tutulur; hedef klasöre asla yan dosya sızmaz.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import detect_musescore_exe


class MuseScoreError(RuntimeError):
    pass


def _clean_env_for_musescore() -> dict[str, str]:
    """
    PyQt5 ve PyInstaller tek-dosya (.exe) ortam değişkenlerinin
    Qt6 tabanlı MuseScore4.exe ile DLL çakışması yaşamasını önler.
    """
    blocked_prefixes = ("QT_", "PYINSTALLER_", "_MEIPASS")
    blocked_exact = {"PYTHONPATH", "PYTHONHOME", "TCL_LIBRARY", "TK_LIBRARY"}
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(blocked_prefixes) and k.upper() not in blocked_exact
    }
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and "PATH" in env:
        mei_norm = os.path.normcase(os.path.normpath(meipass))
        parts = env["PATH"].split(os.pathsep)
        clean_parts = [
            p for p in parts if p and os.path.normcase(os.path.normpath(p)) != mei_norm
        ]
        env["PATH"] = os.pathsep.join(clean_parts)
    return env


def run_musescore_cli(args: list[str], musescore_exe: str | None = None, timeout: int = 90) -> None:
    exe = musescore_exe or detect_musescore_exe()
    if not os.path.isfile(exe):
        raise MuseScoreError(
            f"MuseScore 4 bulunamadı ({exe}).\n\n"
            "Lütfen ücretsiz MuseScore 4 programını (https://musescore.org) kurun "
            "veya farklı bir klasöre kuruluysa üst menüdeki '⚙ Ayarlar' düğmesinden MuseScore4.exe yolunu seçin."
        )

    cmd = [exe, *args]
    child_env = _clean_env_for_musescore()
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

    with tempfile.TemporaryFile(mode="w+b") as log_f:
        res = subprocess.run(
            cmd,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=creation_flags,
        )
        if res.returncode != 0:
            log_f.seek(0)
            out_txt = log_f.read().decode("utf-8", errors="replace")
            raise MuseScoreError(f"MuseScore CLI hatası (kod {res.returncode}):\n{out_txt}")


def export_from_mscx_string(
    mscx_xml: str,
    out_mscz: str | Path | None = None,
    out_pdf: str | Path | None = None,
    out_midi: str | Path | None = None,
    out_mp3: str | Path | None = None,
    preview_png_dir: str | Path | None = None,
    dpi: int = 300,
    musescore_exe: str | None = None,
) -> list[Path]:
    """
    Bellekteki .mscx XML metnini geçici dizine yazar, istenen tüm formatları (.mscz, .pdf, .mid, .mp3, .png)
    senkron olarak üretir ve varsa önizleme PNG yollarının sıralı listesini döndürür.
    """
    exe = musescore_exe or detect_musescore_exe()
    preview_paths: list[Path] = []

    with tempfile.TemporaryDirectory(prefix="makam_ms4_") as tmpdir:
        tmp_mscx = Path(tmpdir) / "score_work.mscx"
        tmp_mscx.write_text(mscx_xml, encoding="utf-8")

        if out_mscz:
            p = Path(out_mscz)
            p.parent.mkdir(parents=True, exist_ok=True)
            run_musescore_cli(["-o", str(p), str(tmp_mscx)], musescore_exe=exe)

        if out_pdf:
            p = Path(out_pdf)
            p.parent.mkdir(parents=True, exist_ok=True)
            run_musescore_cli(["-o", str(p), str(tmp_mscx)], musescore_exe=exe)

        if out_midi:
            p = Path(out_midi)
            p.parent.mkdir(parents=True, exist_ok=True)
            run_musescore_cli(["-o", str(p), str(tmp_mscx)], musescore_exe=exe)

        if out_mp3:
            p = Path(out_mp3)
            p.parent.mkdir(parents=True, exist_ok=True)
            run_musescore_cli(["-o", str(p), str(tmp_mscx)], musescore_exe=exe)

        if preview_png_dir:
            pdir = Path(preview_png_dir)
            pdir.mkdir(parents=True, exist_ok=True)
            for old_f in pdir.glob("preview-*.png"):
                try:
                    old_f.unlink()
                except OSError:
                    pass
            base_png = pdir / "preview.png"
            run_musescore_cli(["-r", str(dpi), "-o", str(base_png), str(tmp_mscx)], musescore_exe=exe)
            preview_paths = sorted(pdir.glob("preview-*.png"))
            if not preview_paths and base_png.is_file():
                preview_paths = [base_png]

    return preview_paths


def convert_any_digital_score_to_mscx_text(input_path: str | Path, musescore_exe: str | None = None) -> str:
    """
    .mscz, .mscx, .musicxml veya .mxl dosyasını kayıpsız olarak .mscx XML metnine dönüştürür.
    """
    import zipfile

    in_p = Path(input_path)
    ext = in_p.suffix.lower()

    if ext == ".mscx":
        return in_p.read_text(encoding="utf-8")

    if ext == ".mscz":
        with zipfile.ZipFile(in_p, "r") as zf:
            mscx_names = [n for n in zf.namelist() if n.lower().endswith(".mscx")]
            if not mscx_names:
                raise MuseScoreError(f"{in_p.name} içinde .mscx verisi bulunamadı.")
            return zf.read(mscx_names[0]).decode("utf-8")

    if ext in (".musicxml", ".xml", ".mxl"):
        exe = musescore_exe or detect_musescore_exe()
        with tempfile.TemporaryDirectory(prefix="makam_import_") as tmpdir:
            out_mscx = Path(tmpdir) / "imported.mscx"
            src_copy = Path(tmpdir) / in_p.name
            shutil.copy2(in_p, src_copy)
            run_musescore_cli(["-o", str(out_mscx), str(src_copy)], musescore_exe=exe)
            return out_mscx.read_text(encoding="utf-8")

    raise MuseScoreError(f"Desteklenmeyen dijital nota uzantısı: {ext}")


def open_in_musescore_gui(score_path: str | Path, musescore_exe: str | None = None) -> None:
    """Üretilen .mscz dosyasını arka planda MuseScore 4 masaüstü uygulamasında açar."""
    exe = musescore_exe or detect_musescore_exe()
    subprocess.Popen([exe, str(score_path)], env=_clean_env_for_musescore(), close_fds=True)
