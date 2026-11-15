"""
TMTP (MakamNotaApp) Bağımsız Tek-Dosya Windows (.exe) Derleme Betiği.
Herhangi bir 64-bit Windows 10 / 11 bilgisayarda Python veya VC++ Redistributable
kurulumu gerektirmeden çalışabilen, gereksiz QML/OpenGL/FFmpeg şişkinliğinden
arındırılmış hızlı açılan tek parça 'dist/TMTP.exe' üretir.

Kullanım:
    python build_exe.py
"""

import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = ROOT_DIR / "assets"
BUILD_DIR = ROOT_DIR / "build"
DIST_DIR = ROOT_DIR / "dist"


def generate_app_icon(ico_path: Path) -> None:
    """53-Koma AEU temalı çok-çözünürlüklü (16..256 px) Windows .ico simgesi üretir."""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    size = 512
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")

    draw.rounded_rectangle((20, 20, size - 20, size - 20), radius=108, fill=(18, 35, 60, 255))
    draw.rounded_rectangle(
        (34, 34, size - 34, size - 34),
        radius=96,
        outline=(218, 165, 32, 245),
        width=14,
    )

    for idx in range(5):
        y = 148 + idx * 42
        draw.line((76, y, size - 76, y), fill=(218, 165, 32, 72), width=5)

    f_note = None
    for fp in (r"C:\Windows\Fonts\seguisym.ttf", r"C:\Windows\Fonts\arialbd.ttf"):
        if os.path.isfile(fp):
            f_note = ImageFont.truetype(fp, 220)
            break
    bold_font_p = (
        r"C:\Windows\Fonts\segoeuib.ttf"
        if os.path.isfile(r"C:\Windows\Fonts\segoeuib.ttf")
        else r"C:\Windows\Fonts\arialbd.ttf"
    )
    f_bold = ImageFont.truetype(bold_font_p, 92)
    f_sub = ImageFont.truetype(bold_font_p, 56)

    if f_note is not None:
        nb = draw.textbbox((0, 0), "♪", font=f_note)
        nw = nb[2] - nb[0]
        draw.text(((size - nw) // 2 - 48, 62), "♪", fill=(255, 215, 90, 255), font=f_note)

    draw.rounded_rectangle((282, 92, 434, 184), radius=24, fill=(218, 165, 32, 255))
    b53 = draw.textbbox((0, 0), "53", font=f_sub)
    draw.text(
        (282 + (152 - (b53[2] - b53[0])) // 2, 102),
        "53",
        fill=(18, 32, 54, 255),
        font=f_sub,
    )

    bt = draw.textbbox((0, 0), "TMTP", font=f_bold)
    tw = bt[2] - bt[0]
    draw.text(((size - tw) // 2, 344), "TMTP", fill=(255, 255, 255, 255), font=f_bold)

    img.save(
        ico_path,
        format="ICO",
        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
    )


def write_version_info(ver_path: Path) -> None:
    content = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 0, 0, 0),
    prodvers=(1, 0, 0, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'TMTP Open Source Project'),
        StringStruct('FileDescription', 'TMTP — Türk Müziği Transpoze ve Nota Replikası Programı'),
        StringStruct('FileVersion', '1.0.0'),
        StringStruct('InternalName', 'TMTP'),
        StringStruct('LegalCopyright', 'MIT License — github.com/ferahfeza/TMTP'),
        StringStruct('OriginalFilename', 'TMTP.exe'),
        StringStruct('ProductName', 'TMTP — 53-Koma Türk Müziği Stüdyosu'),
        StringStruct('ProductVersion', '1.0.0')])
      ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    ver_path.write_text(content, encoding="utf-8")


def find_msvc_runtime_dlls() -> list[Path]:
    """Temiz Windows kurulumlarında eksik olabilen MSVC C/C++ çalışma zamanı DLL'lerini toplar."""
    dll_names = [
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "msvcp140.dll",
        "msvcp140_1.dll",
        "msvcp140_2.dll",
        "concrt140.dll",
    ]
    py_dir = Path(sys.executable).resolve().parent
    qt_bin = py_dir / "Lib" / "site-packages" / "PyQt5" / "Qt5" / "bin"
    sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"

    found: list[Path] = []
    for dname in dll_names:
        for folder in (py_dir, qt_bin, sys32):
            cand = folder / dname
            if cand.is_file():
                found.append(cand)
                break
    return found


def write_spec_file(spec_path: Path, ico_path: Path, ver_file: Path, msvc_dlls: list[Path]) -> None:
    binaries_repr = repr([(str(p), ".") for p in msvc_dlls])
    datas_repr = repr([(str(ico_path), "assets")])
    gui_script_repr = repr(str(ROOT_DIR / "gui_app.py"))
    ico_repr = repr(str(ico_path))
    ver_repr = repr(str(ver_file))

    spec_code = f"""# -*- mode: python ; coding: utf-8 -*-

excludes = [
    'vtk', 'vtkmodules', 'pyvista', 'gmsh', 'meshio', 'scipy', 'matplotlib',
    'tkinter', '_tkinter', 'pytest', 'IPython', 'notebook', 'jupyter',
    'sympy', 'pandas', 'lxml', 'traitlets', 'pygments', 'jedi', 'parso',
    'zmq', 'tornado', 'sqlite3', 'PIL._avif',
    'PyQt5.QtQml', 'PyQt5.QtQuick', 'PyQt5.QtQuickWidgets', 'PyQt5.QtDesigner',
    'PyQt5.QtLocation', 'PyQt5.QtMultimedia', 'PyQt5.QtBluetooth', 'PyQt5.QtNfc',
    'PyQt5.QtPositioning', 'PyQt5.QtSensors', 'PyQt5.QtSerialPort',
    'PyQt5.QtSql', 'PyQt5.QtTest', 'PyQt5.QtWebChannel', 'PyQt5.QtWebSockets',
    'PyQt5.QtXmlPatterns',
]

a = Analysis(
    [{gui_script_repr}],
    pathex=[],
    binaries={binaries_repr},
    datas={datas_repr},
    hiddenimports=['makam_app.omr_gemini', 'google.genai', 'pydantic'],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

drop_bin_substrings = (
    'opencv_videoio_ffmpeg',
    'opengl32sw.dll',
    'd3dcompiler_47.dll',
    'libglesv2.dll',
    'libegl.dll',
    'qt5quick',
    'qt5qml',
    'qt5designer',
    'qt5location',
    'qt5multimedia',
    'qt5xmlpatterns',
    'qt5websockets',
    'qt5dbus',
)

a.binaries = [
    b for b in a.binaries
    if not any(sub in b[0].lower() for sub in drop_bin_substrings)
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TMTP',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version={ver_repr},
    icon=[{ico_repr}],
)
"""
    spec_path.write_text(spec_code, encoding="utf-8")


def main() -> None:
    ico_path = ASSETS_DIR / "tmtp_icon.ico"
    generate_app_icon(ico_path)

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    ver_file = BUILD_DIR / "version_info.txt"
    write_version_info(ver_file)

    msvc_dlls = find_msvc_runtime_dlls()
    print("Bundling MSVC runtime DLLs:", [p.name for p in msvc_dlls])

    spec_file = BUILD_DIR / "TMTP_build.spec"
    write_spec_file(spec_file, ico_path, ver_file, msvc_dlls)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR / "work"),
        str(spec_file),
    ]

    print("Running PyInstaller...")
    subprocess.run(cmd, cwd=str(ROOT_DIR), check=True)

    out_exe = DIST_DIR / "TMTP.exe"
    if not out_exe.is_file():
        raise RuntimeError(f"Derlenen dosya bulunamadı: {out_exe}")

    mb = out_exe.stat().st_size / (1024 * 1024)
    print(f"\nBasariyla olusturuldu: {out_exe} ({mb:.2f} MB)")


if __name__ == "__main__":
    main()
