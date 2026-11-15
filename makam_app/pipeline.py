"""
MakamNotaApp Uçtan Uca Nota Yükleme, OMR, 53-Koma Göçürme ve Dışa Aktarma İş Hattı (Pipeline).
Hem CLI hem de PyQt5 Masaüstü Arayüzü bu modülü kullanır.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .aeu_theory import STEPS, transpose_keysig_map
from .config import load_settings
from .mscx_io import (
    extract_keysig_from_mscx,
    mscx_to_score_doc,
    score_doc_to_mscx,
    transpose_mscx_lossless,
)
from .musescore_runner import (
    convert_any_digital_score_to_mscx_text,
    export_from_mscx_string,
)
from .omr_opencv import recognize_score_opencv
from .omr_vector_pdf import has_vector_music_notation, recognize_vector_pdf
from .score_model import (
    MeasureValidationIssue,
    ScoreDoc,
    transpose_score_doc,
    validate_score_durations,
)

DIGITAL_EXTS = {".mscz", ".mscx", ".musicxml", ".xml", ".mxl"}
RASTER_PDF_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


@dataclass
class LoadedScoreState:
    input_path: Path
    source_kind: str                 # "digital_mscx", "json_doc", "omr_gemini", "omr_opencv"
    score_doc: ScoreDoc
    orig_keysig: dict[str, int]
    raw_mscx_xml: str | None = None  # Dijital girdilerde %100 kayıpsız XML ağacı


@dataclass
class ExportResult:
    mscz_path: Path
    pdf_path: Path
    midi_path: Path | None = None
    mp3_path: Path | None = None
    json_path: Path | None = None
    preview_pngs: list[Path] = field(default_factory=list)
    target_keysig: dict[str, int] = field(default_factory=dict)
    validation_issues: list[MeasureValidationIssue] = field(default_factory=list)


def load_score_source(
    input_path: str | Path,
    omr_mode: str = "auto",
    settings: dict | None = None,
    progress_cb: Callable[[int, str], None] | None = None,
) -> LoadedScoreState:
    """
    Verilen kaynak dosyayı (.pdf, .png, .jpg, .mscz, .mscx, .musicxml, .json) okur ve
    birleşik LoadedScoreState nesnesi döndürür.
    """
    cfg = settings or load_settings()
    in_p = Path(input_path).resolve()
    if not in_p.is_file():
        raise FileNotFoundError(f"Girdi dosyası bulunamadı: {in_p}")

    ext = in_p.suffix.lower()

    if ext == ".json":
        if progress_cb:
            progress_cb(30, "Önbellek JSON nota verisi okunuyor...")
        doc = ScoreDoc.load_json(in_p)
        ks = {s: doc.keysig.get(s, 0) for s in STEPS}
        if progress_cb:
            progress_cb(100, "JSON nota yüklendi.")
        return LoadedScoreState(
            input_path=in_p,
            source_kind="json_doc",
            score_doc=doc,
            orig_keysig=ks,
            raw_mscx_xml=None,
        )

    if ext in DIGITAL_EXTS:
        if progress_cb:
            progress_cb(25, f"Dijital nota okunuyor ({in_p.name})...")
        mscx_text = convert_any_digital_score_to_mscx_text(in_p, musescore_exe=cfg.get("musescore_exe"))
        ks = extract_keysig_from_mscx(mscx_text)
        doc = mscx_to_score_doc(mscx_text)
        if progress_cb:
            progress_cb(100, "Dijital nota kayıpsız olarak yüklendi.")
        return LoadedScoreState(
            input_path=in_p,
            source_kind="digital_mscx",
            score_doc=doc,
            orig_keysig=ks,
            raw_mscx_xml=mscx_text,
        )

    if ext in RASTER_PDF_EXTS:
        mode = omr_mode.lower().strip()
        api_key = (cfg.get("gemini_api_key") or "").strip()
        model_name = cfg.get("gemini_model") or "gemini-4-hydrogen"

        # Vektörel PDF notalarında (Finale Petrucci/TURKEY veya MuseScore/SMuFL) doğrudan
        # vektör ve glif okuyucuyu kullan (%100 kesin, çevrimdışı, API anahtarsız)
        if ext == ".pdf" and mode != "gemini" and has_vector_music_notation(in_p):
            doc = recognize_vector_pdf(in_p, progress_cb=progress_cb)
            src_kind = "vector_pdf"
        elif (mode == "gemini") or (mode == "auto" and bool(api_key)):
            from .omr_gemini import recognize_score_gemini

            doc = recognize_score_gemini(
                in_p,
                api_key=api_key,
                model_name=model_name,
                progress_cb=progress_cb,
            )
            src_kind = "omr_gemini"
        else:
            doc = recognize_score_opencv(in_p, progress_cb=progress_cb)
            src_kind = "omr_opencv"

        ks = {s: doc.keysig.get(s, 0) for s in STEPS}
        return LoadedScoreState(
            input_path=in_p,
            source_kind=src_kind,
            score_doc=doc,
            orig_keysig=ks,
            raw_mscx_xml=None,
        )

    raise ValueError(f"Desteklenmeyen dosya türü: {ext}")


def default_output_stem(stem: str, diatonic_steps: int, comma_shift: int) -> str:
    if diatonic_steps == 0 and comma_shift == 0:
        return f"{stem}_Replika"
    if diatonic_steps == 1 and comma_shift == 9:
        return f"{stem}_Bir_Ses_Yukari"
    if diatonic_steps == -1 and comma_shift == -9:
        return f"{stem}_Bir_Ses_Asagi"
    sign_s = f"+{diatonic_steps}" if diatonic_steps >= 0 else str(diatonic_steps)
    sign_c = f"+{comma_shift}" if comma_shift >= 0 else str(comma_shift)
    return f"{stem}_Transpoze_{sign_s}Adim_{sign_c}Koma".replace("+", "arti_").replace("-", "eksi_")


def export_transposed_score(
    state: LoadedScoreState,
    out_dir: str | Path,
    base_name: str | None = None,
    diatonic_steps: int = 0,
    comma_shift: int = 0,
    custom_keysig_override: dict[str, int] | None = None,
    force_score_doc_pipeline: bool = False,
    apply_53edo_tuning: bool = True,
    export_midi: bool = True,
    export_mp3: bool = False,
    save_json: bool = True,
    preview_png_dir: str | Path | None = None,
    subtitle_note: str | None = None,
    musescore_exe: str | None = None,
    progress_cb: Callable[[int, str], None] | None = None,
) -> ExportResult:
    """
    Yüklenmiş nota durumunu (LoadedScoreState) istenen 53-koma aralığına göçürür ve
    .mscz + .pdf (+ isteğe bağlı .mid, .mp3, .json ve önizleme PNG) çıktılarını üretir.
    """
    odir = Path(out_dir).resolve()
    odir.mkdir(parents=True, exist_ok=True)
    stem = base_name.strip() if base_name and base_name.strip() else default_output_stem(
        state.input_path.stem, diatonic_steps, comma_shift
    )

    computed_ks = transpose_keysig_map(state.orig_keysig, diatonic_steps, comma_shift)
    target_ks = (
        {s: custom_keysig_override.get(s, 0) for s in STEPS}
        if custom_keysig_override is not None
        else computed_ks
    )

    if progress_cb:
        progress_cb(30, "53-Koma AEU donanım ve perde dönüşümleri hesaplanıyor...")

    if state.raw_mscx_xml is not None and not force_score_doc_pipeline:
        mscx_out = transpose_mscx_lossless(
            state.raw_mscx_xml,
            diatonic_steps=diatonic_steps,
            comma_shift=comma_shift,
            custom_keysig_override=custom_keysig_override,
            apply_53edo_tuning=apply_53edo_tuning,
            subtitle_addition=subtitle_note,
        )
        transposed_doc = transpose_score_doc(
            state.score_doc,
            diatonic_steps=diatonic_steps,
            comma_shift=comma_shift,
            custom_keysig_override=custom_keysig_override,
            subtitle_note=subtitle_note,
        )
    else:
        transposed_doc = transpose_score_doc(
            state.score_doc,
            diatonic_steps=diatonic_steps,
            comma_shift=comma_shift,
            custom_keysig_override=custom_keysig_override,
            subtitle_note=subtitle_note,
        )
        mscx_out = score_doc_to_mscx(transposed_doc, apply_53edo_tuning=apply_53edo_tuning)

    issues = validate_score_durations(transposed_doc)

    mscz_path = odir / f"{stem}.mscz"
    pdf_path = odir / f"{stem}.pdf"
    midi_path = (odir / f"{stem}.mid") if export_midi else None
    mp3_path = (odir / f"{stem}.mp3") if export_mp3 else None
    json_path = (odir / f"{stem}.json") if save_json else None

    if json_path is not None:
        transposed_doc.save_json(json_path)

    if progress_cb:
        progress_cb(65, "MuseScore 4 ile .mscz ve .pdf dosyaları derleniyor...")

    preview_pngs = export_from_mscx_string(
        mscx_xml=mscx_out,
        out_mscz=mscz_path,
        out_pdf=pdf_path,
        out_midi=midi_path,
        out_mp3=mp3_path,
        preview_png_dir=preview_png_dir,
        dpi=220,
        musescore_exe=musescore_exe,
    )

    if progress_cb:
        progress_cb(100, f"Tamamlandı: {mscz_path.name}, {pdf_path.name}")

    return ExportResult(
        mscz_path=mscz_path,
        pdf_path=pdf_path,
        midi_path=midi_path,
        mp3_path=mp3_path,
        json_path=json_path,
        preview_pngs=preview_pngs,
        target_keysig=target_ks,
        validation_issues=issues,
    )
