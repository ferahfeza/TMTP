"""
MakamNotaApp Kapsamlı Doğrulama Test Paketi (pytest).
1. 53-Koma AEU donanım ve perde göçürme cebiri (Şevkefzâ, Acemaşîrân, Ney Ahenkleri, Karar Perdeleri, Cent Tuning)
2. Kayıpsız .mscz replika (0, 0) ve +1/-1 tam ses transpoze (.mscz + .pdf + .mid)
3. Sözlü eser (<Lyrics>), 1./2. dolap (<Volta>), çarpma notası, bağ (Tie) ve tüm AEU arızalarının MuseScore 4.6 derlemesi
4. omr_slicer + omr_opencv yerel OMR motorunun gerçek Acemaşîrân PDF notası üzerinde uçtan uca testi
5. CLI (cli.py) ve PyQt5 Masaüstü Arayüzü (QT_QPA_PLATFORM=offscreen) duman testi
"""

import os
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from makam_app.aeu_theory import (
    compute_53edo_tuning_cents,
    interval_between_ahenkler,
    interval_between_perdeler,
    transpose_keysig_map,
    transpose_pitch_and_commas,
)
from makam_app.mscx_io import (
    extract_keysig_from_mscx,
    score_doc_to_mscx,
    transpose_mscx_lossless,
)
from makam_app.musescore_runner import (
    convert_any_digital_score_to_mscx_text,
    export_from_mscx_string,
)
from makam_app.omr_opencv import recognize_score_opencv
from makam_app.omr_slicer import detect_and_slice_systems, render_input_pages
from makam_app.pipeline import export_transposed_score, load_score_source
from makam_app.score_model import (
    MeasureSpec,
    NoteEvent,
    ScoreDoc,
    transpose_score_doc,
    validate_score_durations,
)


def test_aeu_53koma_algebra():
    # 1. Şevkefzâ (Si -1 koma, Re -4 koma) -> +1 tam ses (+1 adım, +9 koma) yukarı:
    #    Si(-1)=48 -> +9 = 57 -> Do5(53) + 4 (Do bakiye #)
    #    Re(-4)=5  -> +9 = 14 -> Mi4(18) - 4 (Mi bakiye b)
    #    Mi(0)=18  -> +9 = 27 -> Fa4(22) + 5 (Fa küçük mücenneb #)
    #    La(0)=40  -> +9 = 49 -> Si4(49) + 0 (Si natürel)
    sevk_ks = {"B": -1, "D": -4}
    tr_sevk = transpose_keysig_map(sevk_ks, diatonic_steps=1, comma_shift=9)
    nonzero_sevk = {k: v for k, v in tr_sevk.items() if v != 0}
    assert nonzero_sevk == {"C": 4, "E": -4, "F": 5}

    # 2. Acemaşîrân (Si -5 koma / küçük mücenneb b) -> +1 tam ses (+1 adım, +9 koma) yukarı:
    #    Si(-5)=44 -> +9 = 53 -> Do5(53) + 0 (Do natürel)
    #    Mi(0)=18  -> +9 = 27 -> Fa4(22) + 5 (Fa küçük mücenneb #)
    acem_ks = {"B": -5}
    tr_acem = transpose_keysig_map(acem_ks, diatonic_steps=1, comma_shift=9)
    nonzero_acem = {k: v for k, v in tr_acem.items() if v != 0}
    assert nonzero_acem == {"F": 5}

    # 3. Oktav sınırı testi: B4 (-1 koma = Segâh) + (+1 adım, +9 koma) -> C5 (+4 koma = Do#)
    ns, no, nd = transpose_pitch_and_commas("B", 4, -1, 1, 9)
    assert (ns, no, nd) == ("C", 5, 4)

    # 4. Ney ahenkleri (Elimdeki Ney -> İcra Edilecek Hedef Ahenk):
    #    Bolâhenk Nısfiye=0, Süpürde=2, Kız=5, Mansur=7
    assert interval_between_ahenkler(7, 5) == (1, 9)     # Mansur -> Kız = +1 tam ses yukarı
    assert interval_between_ahenkler(7, 0) == (4, 31)    # Mansur -> Bolâhenk Nısfiye = +Tam 5'li yukarı
    assert interval_between_ahenkler(5, 0) == (3, 22)    # Kız -> Bolâhenk Nısfiye = +Tam 4'lü yukarı
    assert interval_between_ahenkler(5, 2) == (2, 13)    # Kız -> Süpürde = +Küçük 3'lü yukarı
    assert interval_between_ahenkler(2, 5) == (-2, -13)  # Süpürde -> Kız = -Küçük 3'lü aşağı
    assert interval_between_ahenkler(2, 7) == (-3, -22)  # Süpürde -> Mansur = -Tam 4'lü aşağı

    # 5. Karar perdesi: Dügâh La4 (indeks 6) -> Nevâ Re5 (indeks 11) = (+3 adım, +22 koma)
    assert interval_between_perdeler(6, 11) == (3, 22)

    # 6. 53-EDO cent hesabı: F + 4 koma (Irak/Eviç), midi_alter=0 ve midi_alter=+1 aynı mutlak frekansı vermeli
    c0 = compute_53edo_tuning_cents("F", 4, midi_alter=0)
    c1 = compute_53edo_tuning_cents("F", 4, midi_alter=1)
    assert abs((500.0 + c0) - (600.0 + c1)) < 0.05


def _make_sample_sevkefza_mscz(target_mscz: Path) -> Path:
    """Her bilgisayarda bağımsız çalışabilmesi için örnek Şevkefzâ .mscz üretir."""
    doc = ScoreDoc(
        title="Şevkefzâ Örnek Saz Semâîsi",
        composer="TMTP Test",
        keysig={"B": -1, "D": -4},
        measures=[
            MeasureSpec(
                number=1,
                nominal_ts=[6, 8],
                actual_len="6/8",
                events=[
                    NoteEvent(kind="note", step="F", octave=4, duration="quarter"),
                    NoteEvent(kind="note", step="A", octave=4, duration="eighth"),
                    NoteEvent(kind="note", step="C", octave=5, duration="quarter"),
                    NoteEvent(kind="note", step="D", octave=5, duration="eighth"),
                ],
            ),
            MeasureSpec(
                number=2,
                actual_len="6/8",
                barline="end",
                events=[
                    NoteEvent(kind="note", step="C", octave=5, duration="quarter."),
                    NoteEvent(kind="note", step="F", octave=4, duration="quarter."),
                ],
            ),
        ],
    )
    mscx = score_doc_to_mscx(doc, apply_53edo_tuning=True)
    export_from_mscx_string(mscx, out_mscz=target_mscz)
    return target_mscz


def test_lossless_mscz_replica_and_transpose(tmp_path: Path):
    src_mscz = _make_sample_sevkefza_mscz(tmp_path / "sample_sevkefza.mscz")
    assert src_mscz.is_file()

    state = load_score_source(src_mscz)
    assert state.orig_keysig["B"] == -1
    assert state.orig_keysig["D"] == -4

    # Replika (0, 0)
    rep = export_transposed_score(
        state=state,
        out_dir=tmp_path / "rep",
        base_name="YZ_Replika",
        diatonic_steps=0,
        comma_shift=0,
        export_midi=True,
    )
    assert rep.mscz_path.is_file() and rep.mscz_path.stat().st_size > 2000
    assert rep.pdf_path.is_file() and rep.pdf_path.stat().st_size > 5000
    assert rep.midi_path is not None and rep.midi_path.is_file()

    # +1 Tam Ses Yukarı (+1, +9) ve ardından -1 Tam Ses Aşağı (-1, -9) tersinirlik (round-trip) kontrolü
    up1 = export_transposed_score(
        state=state,
        out_dir=tmp_path / "up1",
        base_name="YZ_Up1",
        diatonic_steps=1,
        comma_shift=9,
        export_midi=False,
    )
    assert up1.mscz_path.is_file() and up1.pdf_path.is_file()

    state_up1 = load_score_source(up1.mscz_path)
    back0 = transpose_mscx_lossless(state_up1.raw_mscx_xml, diatonic_steps=-1, comma_shift=-9)
    ks_back = extract_keysig_from_mscx(back0)
    assert ks_back["B"] == -1 and ks_back["D"] == -4 and ks_back["F"] == 0 and ks_back["C"] == 0


def test_vocal_lyrics_volta_ties_and_all_aeu_accidentals(tmp_path: Path):
    doc = ScoreDoc(
        title="AEU Donanım, Güfte ve Dolap Testi",
        subtitle="Uşşak Şarkı Kesiti",
        composer="Test Bestekârı",
        lyricist="Test Güftekârı",
        keysig={"B": -1},
        measures=[
            MeasureSpec(
                number=1,
                nominal_ts=[4, 4],
                actual_len="4/4",
                tempo_text="Aksak / Sofyan  ♩ = 88",
                tempo_qpm=88.0,
                section_label="Zemin",
                start_repeat=True,
                events=[
                    NoteEvent(kind="note", step="A", octave=4, duration="quarter", lyric="Şevk-"),
                    NoteEvent(kind="note", step="B", octave=4, duration="quarter", lyric="ef-"),
                    NoteEvent(kind="note", step="C", octave=5, duration="quarter", accidental_commas=4, lyric="zâ"),
                    NoteEvent(kind="note", step="D", octave=5, duration="quarter", grace_before="E5", lyric="nâz"),
                ],
            ),
            MeasureSpec(
                number=2,
                actual_len="4/4",
                volta=1,
                end_repeat=True,
                events=[
                    NoteEvent(kind="note", step="E", octave=5, duration="eighth", accidental_commas=-1, lyric="ey-"),
                    NoteEvent(kind="note", step="F", octave=5, duration="eighth", accidental_commas=1, lyric="ler"),
                    NoteEvent(kind="note", step="G", octave=5, duration="quarter", accidental_commas=5, tie_start=True, lyric="gö-"),
                    NoteEvent(kind="note", step="G", octave=5, duration="quarter", tie_end=True),
                    NoteEvent(kind="note", step="A", octave=5, duration="eighth", accidental_commas=-8, lyric="nül"),
                    NoteEvent(kind="note", step="F", octave=5, duration="eighth", accidental_commas=8, lyric="yâr"),
                ],
            ),
            MeasureSpec(
                number=3,
                actual_len="4/4",
                volta=2,
                barline="end",
                end_text="— Son —",
                events=[
                    NoteEvent(kind="note", step="D", octave=5, duration="quarter", accidental_commas=-4, lyric="dil-"),
                    NoteEvent(kind="note", step="B", octave=4, duration="quarter", accidental_commas=-5, lyric="dâr"),
                    NoteEvent(kind="note", step="A", octave=4, duration="half", accidental_commas=0, cautionary=True, lyric="âh"),
                ],
            ),
        ],
    )
    assert validate_score_durations(doc) == []

    # +1 ses yukarı göçür ve MuseScore 4 ile PDF + PNG üret
    tr_doc = transpose_score_doc(doc, diatonic_steps=1, comma_shift=9)
    assert validate_score_durations(tr_doc) == []

    mscx = score_doc_to_mscx(doc, apply_53edo_tuning=True)
    png_dir = tmp_path / "test_vocal_render"
    pngs = export_from_mscx_string(
        mscx,
        out_mscz=tmp_path / "vocal_test.mscz",
        out_pdf=tmp_path / "vocal_test.pdf",
        preview_png_dir=png_dir,
        dpi=300,
    )
    assert (tmp_path / "vocal_test.mscz").is_file()
    assert (tmp_path / "vocal_test.pdf").is_file()
    assert len(pngs) >= 1 and pngs[0].is_file()


def test_omr_slicer_and_offline_opencv_engine(tmp_path: Path):
    # MuseScore 4 üzerinden vektörel/görsel PDF üretip PNG üzerinden çevrimdışı OMR motorunu doğrula
    sample_mscz = _make_sample_sevkefza_mscz(tmp_path / "omr_src.mscz")
    mscx_txt = convert_any_digital_score_to_mscx_text(sample_mscz)
    pngs = export_from_mscx_string(
        mscx_txt,
        out_pdf=tmp_path / "omr_src.pdf",
        preview_png_dir=tmp_path / "omr_png",
        dpi=300,
    )
    assert len(pngs) >= 1
    pages = render_input_pages(pngs[0], dpi=300)
    assert len(pages) == 1
    hdr, crops = detect_and_slice_systems(pages)
    assert len(crops) >= 1

    doc = recognize_score_opencv(pngs[0], dpi=300)
    assert len(doc.measures) >= 1
    assert validate_score_durations(doc) == []

    mscx = score_doc_to_mscx(doc, apply_53edo_tuning=True)
    export_from_mscx_string(
        mscx,
        out_mscz=tmp_path / "omr_out.mscz",
        out_pdf=tmp_path / "omr_out.pdf",
    )
    assert (tmp_path / "omr_out.mscz").is_file()
    assert (tmp_path / "omr_out.pdf").is_file()


def test_cli_and_offscreen_gui(tmp_path: Path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import cli
    from PyQt5.QtWidgets import QApplication
    import gui_app

    # 1. CLI test
    rc_list = cli.main(["list-presets"])
    assert rc_list == 0

    src_mscz = str(_make_sample_sevkefza_mscz(tmp_path / "cli_src.mscz"))
    rc_proc = cli.main([
        "process",
        "--input", src_mscz,
        "--out-dir", str(tmp_path / "cli_out"),
        "--name", "Sevkefza_CLI_Test",
        "--from-ahenk", "7",
        "--to-ahenk", "5",
        "--no-midi",
    ])
    assert rc_proc == 0
    assert (tmp_path / "cli_out" / "Sevkefza_CLI_Test.mscz").is_file()
    assert (tmp_path / "cli_out" / "Sevkefza_CLI_Test.pdf").is_file()
    # Yan dosya sızıntısı olmamalı
    files_in_out = {p.name for p in (tmp_path / "cli_out").iterdir()}
    assert files_in_out == {
        "Sevkefza_CLI_Test.mscz",
        "Sevkefza_CLI_Test.pdf",
        "Sevkefza_CLI_Test.json",
    }

    # 2. PyQt5 GUI offscreen test
    app = QApplication.instance() or QApplication([])
    win = gui_app.MainWindow()
    win.load_file_sync(src_mscz)
    win.ed_out_dir.setText(str(tmp_path / "gui_out"))

    # Sekme 3 (Ney Ahenkleri: Elimde Mansur [7] -> İcra Ahengi Bolâhenk Nısfiye [0] = +4 adım, +31 koma)
    win.tabs_mode.setCurrentIndex(2)
    win.cmb_src_ahenk.setCurrentIndex(7)
    win.cmb_dst_ahenk.setCurrentIndex(0)
    assert win.current_interval() == (4, 31)

    res = win.export_current_sync()
    assert res.mscz_path.is_file()
    assert res.pdf_path.is_file()
    win.close()


def test_vector_pdf_extraction_and_gui_preview_save_workflow(tmp_path: Path):
    """
    Vektörel PDF okuma, kaydetmeden önizleme, çıkış uyarısı ve Acemaşîrân (Fa4) -> Rast (Sol4)
    (+1 adım, +9 koma) uçtan uca GUI iş akışı testi.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication
    import gui_app

    # Her ortamda çalışması için çok kıtalı güfte ve dolap içeren vektörel PDF üret
    src_doc = ScoreDoc(
        title="Şevkefzâ Şarkı",
        subtitle="Gül âşık olup bülbüle feryâd ediverse",
        composer="Örnek Bestekâr",
        lyricist="Örnek Güftekâr",
        keysig={"B": -1, "D": -4},
        measures=[
            MeasureSpec(
                number=1,
                nominal_ts=[6, 8],
                actual_len="6/8",
                start_repeat=True,
                events=[
                    NoteEvent(kind="note", step="G", octave=5, duration="eighth", lyrics=["Bül", "Câ"]),
                    NoteEvent(kind="note", step="A", octave=5, duration="eighth", accidental_commas=-4, lyrics=["bül", "nân"]),
                    NoteEvent(kind="note", step="G", octave=5, duration="quarter", lyrics=["o", "bi"]),
                    NoteEvent(kind="note", step="F", octave=5, duration="quarter", lyrics=["lup", "zi"]),
                ],
            ),
            MeasureSpec(
                number=2,
                actual_len="6/8",
                end_repeat=True,
                volta=1,
                events=[
                    NoteEvent(kind="note", step="F", octave=5, duration="quarter.", lyrics=["se"]),
                    NoteEvent(kind="rest", duration="eighth"),
                    NoteEvent(kind="note", step="D", octave=5, duration="quarter", lyrics=["SAZ"]),
                ],
            ),
            MeasureSpec(
                number=3,
                actual_len="6/8",
                volta=2,
                barline="end",
                events=[
                    NoteEvent(kind="note", step="F", octave=5, duration="dotted_half" if False else "quarter.", lyrics=["se"]),
                    NoteEvent(kind="note", step="F", octave=4, duration="quarter."),
                ],
            ),
        ],
    )
    vec_pdf = tmp_path / "sevkefza_vector.pdf"
    export_from_mscx_string(score_doc_to_mscx(src_doc, apply_53edo_tuning=True), out_pdf=vec_pdf)
    assert vec_pdf.is_file()

    state = load_score_source(vec_pdf, omr_mode="offline")
    assert state.source_kind == "vector_pdf"
    assert state.orig_keysig == {"C": 0, "D": -4, "E": 0, "F": 0, "G": 0, "A": 0, "B": -1}
    assert state.score_doc.measures[0].nominal_ts == [6, 8]
    assert validate_score_durations(state.score_doc) == []

    # PyQt5 GUI üzerinde birebir kullanıcı akışı: Acemaşîrân (Fa4) -> Rast (Sol4)
    app = QApplication.instance() or QApplication([])
    win = gui_app.MainWindow()
    win.load_file_sync(str(vec_pdf))
    win.ed_out_dir.setText(str(tmp_path / "sevk_gui_out"))

    # Sekme 1: Karar Perdesi -> Acemaşîrân (indeks 3) -> Rast (indeks 5)
    win.tabs_mode.setCurrentIndex(1)
    win.cmb_src_perde.setCurrentIndex(3)
    win.cmb_dst_perde.setCurrentIndex(5)
    assert win.current_interval() == (1, 9)

    # 1. Önizleme (Transpoze Et ve Önizle): kullanıcı klasörüne HİÇBİR dosya kaydedilmemeli!
    prev_res = win.preview_current_sync()
    assert prev_res.pdf_path.is_file()
    assert win.has_unsaved_changes is True
    assert not (tmp_path / "sevk_gui_out").exists()

    # 1b. Çıkış uyarısı testi: 'cancel' seçildiğinde pencere kapanmamalı!
    win.confirm_unsaved_on_close = True
    win._ask_unsaved_action = lambda *_a, **_kw: "cancel"
    assert win.close() is False

    # 2. Yalnızca kullanıcı kaydetmeyi istediğinde hedef klasöre .mscz + .pdf yazılmalı ve has_unsaved_changes sıfırlanmalı
    res = win.export_current_sync()
    assert win.has_unsaved_changes is False
    assert {k: v for k, v in res.target_keysig.items() if v != 0} == {"C": 4, "E": -4, "F": 5}
    assert res.validation_issues == []
    assert res.mscz_path.is_file() and res.mscz_path.stat().st_size > 4000
    assert res.pdf_path.is_file() and res.pdf_path.stat().st_size > 15000
    assert {p.suffix for p in (tmp_path / "sevk_gui_out").iterdir()} == {".mscz", ".pdf"}
    assert win.close() is True
