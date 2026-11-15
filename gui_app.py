"""
MakamNotaApp — Masaüstü Grafik Kullanıcı Arayüzü (PyQt5).
53-Koma Arel-Ezgi-Uzdilek (AEU) Türk Müziği Nota Replikası, OMR ve Göçürme Uygulaması.
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

import fitz  # PyMuPDF
from PyQt5.QtCore import QEvent, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QImage, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from makam_app.aeu_theory import (
    AEU_ACCIDENTALS,
    INTERVAL_PRESETS,
    NEY_AHENKLERI,
    PERDE_LIST,
    STEP_TR_NAMES,
    STEPS,
    interval_between_ahenkler,
    interval_between_perdeler,
    nearest_aeu_accidental,
    transpose_keysig_map,
)
from makam_app.config import load_settings, save_settings
from makam_app.mscx_io import score_doc_to_mscx
from makam_app.musescore_runner import export_from_mscx_string, open_in_musescore_gui
from makam_app.pipeline import (
    DIGITAL_EXTS,
    ExportResult,
    LoadedScoreState,
    default_output_stem,
    export_transposed_score,
    load_score_source,
)
from makam_app.score_model import ScoreDoc, validate_score_durations

# Donanım tablosu açılır menü seçenekleri
DONANIM_CHOICES: list[tuple[int, str]] = [
    (0, "Yok / Natürel (0 koma)"),
    (-1, "-1 Koma bemolü"),
    (-4, "-4 Bakiye bemolü"),
    (-5, "-5 Küçük mücenneb bemolü (♭)"),
    (-8, "-8 Büyük mücenneb bemolü"),
    (1, "+1 Koma diyezi"),
    (4, "+4 Bakiye diyezi (#)"),
    (5, "+5 Küçük mücenneb diyezi"),
    (8, "+8 Büyük mücenneb diyezi"),
]


class PdfOrImagePreviewWidget(QWidget):
    """PDF veya görüntü sayfalarını pencereye tam sığdırma, yakınlaştırma ve sayfa geçişiyle gösteren bileşen."""

    def __init__(self, title_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmaps: list[QPixmap] = []
        self._page_idx: int = 0
        self._zoom: float = 1.0
        self._fit_mode: str | None = "page"  # "page" (tam sayfa fit), "width" (genişliğe fit) veya None (manuel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        bar = QHBoxLayout()
        self.lbl_title = QLabel(f"<b>{title_text}</b>")
        bar.addWidget(self.lbl_title)
        bar.addStretch(1)

        self.btn_prev = QPushButton("◀")
        self.btn_prev.setFixedWidth(28)
        self.btn_prev.setToolTip("Önceki Sayfa")
        self.btn_prev.clicked.connect(self._prev_page)
        self.lbl_page = QLabel("0 / 0")
        self.btn_next = QPushButton("▶")
        self.btn_next.setFixedWidth(28)
        self.btn_next.setToolTip("Sonraki Sayfa")
        self.btn_next.clicked.connect(self._next_page)

        self.btn_fit_page = QPushButton("Sığdır")
        self.btn_fit_page.setFixedWidth(52)
        self.btn_fit_page.setToolTip("Tüm sayfayı pencereye tam sığdır")
        self.btn_fit_page.clicked.connect(self.fit_to_page)

        self.btn_fit_width = QPushButton("Genişlik")
        self.btn_fit_width.setFixedWidth(60)
        self.btn_fit_width.setToolTip("Sayfayı pencere genişliğine sığdır")
        self.btn_fit_width.clicked.connect(self.fit_to_width)

        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_out.setFixedWidth(26)
        self.btn_zoom_out.setToolTip("Uzaklaştır (Ctrl + Fare Tekerleği)")
        self.btn_zoom_out.clicked.connect(lambda: self._change_zoom(0.84))
        self.btn_zoom_reset = QPushButton("%100")
        self.btn_zoom_reset.setFixedWidth(48)
        self.btn_zoom_reset.setToolTip("Gerçek boyut (%100)")
        self.btn_zoom_reset.clicked.connect(self._reset_zoom)
        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.setFixedWidth(26)
        self.btn_zoom_in.setToolTip("Yakınlaştır (Ctrl + Fare Tekerleği)")
        self.btn_zoom_in.clicked.connect(lambda: self._change_zoom(1.19))

        for w in (
            self.btn_prev,
            self.lbl_page,
            self.btn_next,
            self.btn_fit_page,
            self.btn_fit_width,
            self.btn_zoom_out,
            self.btn_zoom_reset,
            self.btn_zoom_in,
        ):
            bar.addWidget(w)

        layout.addLayout(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.setStyleSheet("QScrollArea { background-color: #2b2d30; border: 1px solid #bbb; }")
        self.scroll.viewport().installEventFilter(self)

        self.img_label = QLabel("Henüz nota yüklenmedi.")
        self.img_label.setAlignment(Qt.AlignCenter)
        self.img_label.setStyleSheet("color: #dcdcdc; padding: 6px;")
        self.scroll.setWidget(self.img_label)
        layout.addWidget(self.scroll, 1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._pixmaps and self._fit_mode is not None:
            self._apply_fit_zoom()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.scroll.viewport():
            if event.type() == QEvent.Wheel and (event.modifiers() & Qt.ControlModifier):
                delta = event.angleDelta().y()
                if delta > 0:
                    self._change_zoom(1.12)
                elif delta < 0:
                    self._change_zoom(0.89)
                return True
        return super().eventFilter(obj, event)

    def clear_view(self, message: str = "Henüz önizleme yok.") -> None:
        self._pixmaps = []
        self._page_idx = 0
        self._fit_mode = "page"
        self.img_label.setPixmap(QPixmap())
        self.img_label.setText(message)
        self.lbl_page.setText("0 / 0")
        self.btn_zoom_reset.setText("%100")

    def load_pdf(self, pdf_path: str | Path, dpi: int = 200) -> None:
        doc = fitz.open(str(pdf_path))
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pix_list: list[QPixmap] = []
        for page in doc:
            pm = page.get_pixmap(matrix=mat, alpha=False)
            qimg = QImage(pm.samples, pm.width, pm.height, pm.stride, QImage.Format_RGB888).copy()
            pix_list.append(QPixmap.fromImage(qimg))
        doc.close()
        self._pixmaps = pix_list
        self._page_idx = 0
        self.fit_to_page()
        QTimer.singleShot(0, self._reapply_fit_if_active)

    def load_image_files(self, img_paths: list[Path]) -> None:
        self._pixmaps = [QPixmap(str(p)) for p in img_paths if Path(p).is_file()]
        self._page_idx = 0
        self.fit_to_page()
        QTimer.singleShot(0, self._reapply_fit_if_active)

    def _reapply_fit_if_active(self) -> None:
        if self._pixmaps and self._fit_mode is not None:
            self._apply_fit_zoom()

    def fit_to_page(self) -> None:
        """Sayfanın tamamını yatay ve dikey kaydırma çubuğu gerektirmeden pencereye tam sığdırır."""
        self._fit_mode = "page"
        self._apply_fit_zoom()

    def fit_to_width(self) -> None:
        """Sayfayı önizleme penceresinin genişliğine tam sığdırır."""
        self._fit_mode = "width"
        self._apply_fit_zoom()

    def _apply_fit_zoom(self) -> None:
        if not self._pixmaps:
            return
        pm = self._pixmaps[self._page_idx]
        if pm.width() <= 0 or pm.height() <= 0:
            return
        avail_w = max(120, self.scroll.width() - 24)
        avail_h = max(120, self.scroll.height() - 24)
        if self._fit_mode == "width":
            avail_w_scroll = max(120, self.scroll.width() - 36)
            self._zoom = max(0.15, min(3.5, avail_w_scroll / pm.width()))
        else:
            self._zoom = max(0.15, min(3.5, min(avail_w / pm.width(), avail_h / pm.height())))
        self._refresh_pixmap()

    def _prev_page(self) -> None:
        if self._page_idx > 0:
            self._page_idx -= 1
            if self._fit_mode is not None:
                self._apply_fit_zoom()
            else:
                self._refresh_pixmap()

    def _next_page(self) -> None:
        if self._page_idx + 1 < len(self._pixmaps):
            self._page_idx += 1
            if self._fit_mode is not None:
                self._apply_fit_zoom()
            else:
                self._refresh_pixmap()

    def _change_zoom(self, factor: float) -> None:
        self._fit_mode = None
        self._zoom = max(0.15, min(4.0, self._zoom * factor))
        self._refresh_pixmap()

    def _reset_zoom(self) -> None:
        self._fit_mode = None
        self._zoom = 1.0
        self._refresh_pixmap()

    def _refresh_pixmap(self) -> None:
        if not self._pixmaps:
            self.clear_view()
            return
        pm = self._pixmaps[self._page_idx]
        target_w = max(80, int(round(pm.width() * self._zoom)))
        target_h = max(80, int(round(pm.height() * self._zoom)))
        scaled = pm.scaled(target_w, target_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.img_label.setText("")
        self.img_label.setPixmap(scaled)
        self.lbl_page.setText(f"{self._page_idx + 1} / {len(self._pixmaps)}")
        self.btn_zoom_reset.setText(f"%{int(round(self._zoom * 100))}")


class SettingsDialog(QDialog):
    """Gemini API Anahtarı, Model ve MuseScore 4 Yolu Ayar Penceresi."""

    def __init__(self, current_settings: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("API ve MuseScore 4 Ayarları")
        self.setMinimumWidth(520)
        self._settings = dict(current_settings)

        form = QFormLayout(self)

        ms_row = QHBoxLayout()
        self.ed_musescore = QLineEdit(self._settings.get("musescore_exe", ""))
        btn_ms = QPushButton("Gözat...")
        btn_ms.clicked.connect(self._pick_musescore)
        ms_row.addWidget(self.ed_musescore, 1)
        ms_row.addWidget(btn_ms)
        form.addRow("MuseScore 4 Yolu:", ms_row)

        self.ed_api_key = QLineEdit(self._settings.get("gemini_api_key", ""))
        self.ed_api_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.ed_api_key.setPlaceholderText("İsteğe bağlı — yüksek doğruluklu PDF/Resim OMR için Gemini API anahtarı")
        form.addRow("Gemini API Anahtarı:", self.ed_api_key)

        self.cmb_model = QComboBox()
        self.cmb_model.setEditable(True)
        models = ["gemini-4-hydrogen", "gemini-4-argon", "gemini-4-carbon", "gemini-3.5-flash-lite"]
        self.cmb_model.addItems(models)
        cur_m = self._settings.get("gemini_model", "gemini-4-hydrogen")
        if cur_m.startswith(("gemini-1.5", "gemini-2.0", "gemini-2.5")):
            cur_m = "gemini-4-hydrogen"
        self.cmb_model.setCurrentText(cur_m)
        form.addRow("Gemini Modeli:", self.cmb_model)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _pick_musescore(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "MuseScore4.exe Seçin", r"C:\Program Files\MuseScore 4\bin", "Yürütülebilir (*.exe)"
        )
        if p:
            self.ed_musescore.setText(p)

    def get_updated_settings(self) -> dict:
        return {
            "musescore_exe": self.ed_musescore.text().strip(),
            "gemini_api_key": self.ed_api_key.text().strip(),
            "gemini_model": self.cmb_model.currentText().strip() or "gemini-4-hydrogen",
        }


class LoadWorker(QThread):
    progress = pyqtSignal(int, str)
    finished_ok = pyqtSignal(object, list)
    failed = pyqtSignal(str)

    def __init__(self, input_path: Path, omr_mode: str, settings: dict, tmp_preview_dir: Path) -> None:
        super().__init__()
        self.input_path = input_path
        self.omr_mode = omr_mode
        self.settings = settings
        self.tmp_preview_dir = tmp_preview_dir

    def run(self) -> None:
        try:
            state = load_score_source(
                self.input_path,
                omr_mode=self.omr_mode,
                settings=self.settings,
                progress_cb=lambda p, m: self.progress.emit(p, m),
            )
            src_pngs: list[Path] = []
            ext = self.input_path.suffix.lower()
            if ext in DIGITAL_EXTS and state.raw_mscx_xml:
                src_pngs = export_from_mscx_string(
                    state.raw_mscx_xml,
                    preview_png_dir=self.tmp_preview_dir,
                    dpi=165,
                    musescore_exe=self.settings.get("musescore_exe"),
                )
            elif ext == ".json":
                mscx_tmp = score_doc_to_mscx(state.score_doc, apply_53edo_tuning=False)
                src_pngs = export_from_mscx_string(
                    mscx_tmp,
                    preview_png_dir=self.tmp_preview_dir,
                    dpi=165,
                    musescore_exe=self.settings.get("musescore_exe"),
                )
            self.finished_ok.emit(state, src_pngs)
        except Exception as exc:
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class ExportWorker(QThread):
    progress = pyqtSignal(int, str)
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, kwargs: dict) -> None:
        super().__init__()
        self.kwargs = kwargs

    def run(self) -> None:
        try:
            kw = dict(self.kwargs)
            kw["progress_cb"] = lambda p, m: self.progress.emit(p, m)
            res = export_transposed_score(**kw)
            self.finished_ok.emit(res)
        except Exception as exc:
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MakamNotaApp — 53-Koma Türk Müziği Nota Replikası ve Göçürme Stüdyosu")
        self.resize(1460, 900)

        self.settings = load_settings()
        self.loaded_state: LoadedScoreState | None = None
        self.last_export: ExportResult | None = None
        self.last_saved_export: ExportResult | None = None
        self.has_unsaved_changes: bool = False
        self.confirm_unsaved_on_close: bool = os.environ.get("QT_QPA_PLATFORM") != "offscreen"
        self.force_score_doc_pipeline: bool = False
        self.keysig_user_overridden: bool = False
        self._updating_ui: bool = False
        self._tmp_dir_obj = tempfile.TemporaryDirectory(prefix="makam_gui_prev_")
        self._tmp_dir = Path(self._tmp_dir_obj.name)
        self._worker: QThread | None = None

        self._build_ui()
        self._sync_interval_from_active_tab()

    def _ask_unsaved_action(self, prompt_text: str, discard_label: str = "Kaydetmeden Çık") -> str:
        """Kullanıcıya Kaydet / Kaydetmeden Devam Et / İptal sorusunu yöneltir ('save', 'discard', 'cancel')."""
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("Kaydedilmemiş Nota")
        msg.setText(prompt_text)
        btn_save = msg.addButton("Kaydet", QMessageBox.AcceptRole)
        btn_discard = msg.addButton(discard_label, QMessageBox.DestructiveRole)
        btn_cancel = msg.addButton("İptal", QMessageBox.RejectRole)
        msg.setDefaultButton(btn_save)
        msg.exec_()
        clicked = msg.clickedButton()
        if clicked == btn_save:
            return "save"
        if clicked == btn_discard:
            return "discard"
        return "cancel"

    def _prompt_save_synchronously(self) -> bool:
        """Çıkış öncesinde dosya seçtirip senkron olarak .mscz + .pdf kaydeder. İptal edilirse False döner."""
        if not self.loaded_state:
            return True
        steps, commas = self.current_interval()
        suggested_stem = self.ed_out_name.text().strip() or default_output_stem(
            self.loaded_state.input_path.stem, steps, commas
        )
        default_dir = Path(self.ed_out_dir.text().strip() or str(self.loaded_state.input_path.parent))
        default_file = default_dir / f"{suggested_stem}.mscz"

        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "Dönüştürülen Notayı Kaydet (.mscz + .pdf birlikte kaydedilir)",
            str(default_file),
            "MuseScore ve PDF Çıktısı (*.mscz *.pdf)",
        )
        if not chosen:
            return False

        chosen_path = Path(chosen)
        target_dir = chosen_path.parent
        target_stem = chosen_path.stem
        self.ed_out_dir.setText(str(target_dir))
        self.ed_out_name.setText(target_stem)

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            kw = self._build_export_kwargs(out_dir=target_dir, base_name=target_stem)
            res = export_transposed_score(**kw)
            self._on_export_succeeded(res)
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Kaydetme Hatası", f"Nota kaydedilemedi:\n{exc}")
            return False
        finally:
            QApplication.restoreOverrideCursor()

    def closeEvent(self, event) -> None:
        if self.confirm_unsaved_on_close and self.loaded_state is not None and self.has_unsaved_changes:
            choice = self._ask_unsaved_action(
                "Dönüştürülen nota henüz kaydedilmedi.\nÇıkmadan önce kaydetmek ister misiniz?"
            )
            if choice == "cancel":
                event.ignore()
                return
            if choice == "save":
                if not self._prompt_save_synchronously():
                    event.ignore()
                    return

        try:
            self._tmp_dir_obj.cleanup()
        except Exception:
            pass
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Arayüz Kurulumu
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_vbox = QVBoxLayout(central)
        root_vbox.setContentsMargins(8, 8, 8, 6)
        root_vbox.setSpacing(6)

        # 1. Üst Araç Çubuğu
        top_frame = QFrame()
        top_frame.setFrameShape(QFrame.StyledPanel)
        top_hbox = QHBoxLayout(top_frame)
        top_hbox.setContentsMargins(8, 6, 8, 6)

        self.btn_load = QPushButton("📂 Nota Yükle (.pdf, .png, .mscz, .musicxml, .json)")
        self.btn_load.setMinimumHeight(34)
        self.btn_load.setStyleSheet("font-weight: bold; padding: 4px 12px;")
        self.btn_load.clicked.connect(self._on_browse_input)
        top_hbox.addWidget(self.btn_load)

        self.lbl_loaded_file = QLabel("Yüklü Dosya: —")
        self.lbl_loaded_file.setStyleSheet("color: #333; margin-left: 8px;")
        top_hbox.addWidget(self.lbl_loaded_file, 1)

        top_hbox.addWidget(QLabel("OMR Motoru:"))
        self.cmb_omr_mode = QComboBox()
        self.cmb_omr_mode.addItem("Otomatik (Vektör PDF / Gemini API / OpenCV)", "auto")
        self.cmb_omr_mode.addItem("Gemini API (Yüksek Doğruluklu OMR)", "gemini")
        self.cmb_omr_mode.addItem("Çevrimdışı Yerel (Vektör PDF + OpenCV)", "opencv")
        top_hbox.addWidget(self.cmb_omr_mode)

        self.btn_save_json = QPushButton("💾 Ara JSON Kaydet")
        self.btn_save_json.clicked.connect(self._on_save_intermediate_json)
        self.btn_save_json.setEnabled(False)
        top_hbox.addWidget(self.btn_save_json)

        self.btn_settings = QPushButton("⚙ Ayarlar")
        self.btn_settings.clicked.connect(self._on_open_settings)
        top_hbox.addWidget(self.btn_settings)

        root_vbox.addWidget(top_frame)

        # 2. Orta Ana Bölme: Sol Kontrol Paneli | Sağ Önizleme + Alt Denetçi
        main_hsplit = QSplitter(Qt.Horizontal)

        left_panel = QWidget()
        left_panel.setMinimumWidth(410)
        left_panel.setMaximumWidth(490)
        left_vbox = QVBoxLayout(left_panel)
        left_vbox.setContentsMargins(2, 2, 4, 2)
        left_vbox.setSpacing(8)

        # 2a. 53-Koma Göçürme (Transpoze) Sekmeleri
        grp_tr = QGroupBox("1. 53-Koma Arel-Ezgi-Uzdilek Göçürme Seçimi")
        tr_vbox = QVBoxLayout(grp_tr)

        self.tabs_mode = QTabWidget()

        # Sekme 1: Ses / Koma Aralığı
        tab_int = QWidget()
        f_int = QFormLayout(tab_int)
        self.cmb_preset = QComboBox()
        for label, _, _ in INTERVAL_PRESETS:
            self.cmb_preset.addItem(label)
        self.cmb_preset.currentIndexChanged.connect(self._on_preset_changed)
        f_int.addRow("Hazır Aralık:", self.cmb_preset)

        self.spn_steps = QSpinBox()
        self.spn_steps.setRange(-14, 14)
        self.spn_steps.setValue(0)
        self.spn_steps.valueChanged.connect(self._on_manual_spin_changed)
        f_int.addRow("Diyatonik Adım (±):", self.spn_steps)

        self.spn_commas = QSpinBox()
        self.spn_commas.setRange(-106, 106)
        self.spn_commas.setValue(0)
        self.spn_commas.valueChanged.connect(self._on_manual_spin_changed)
        f_int.addRow("53-Koma Kaydırma (±):", self.spn_commas)
        self.tabs_mode.addTab(tab_int, "Ses / Koma")

        # Sekme 2: Karar Perdesi -> Hedef Perde
        tab_perde = QWidget()
        f_perde = QFormLayout(tab_perde)
        self.cmb_src_perde = QComboBox()
        self.cmb_dst_perde = QComboBox()
        for label, _, _ in PERDE_LIST:
            self.cmb_src_perde.addItem(label)
            self.cmb_dst_perde.addItem(label)
        self.cmb_src_perde.setCurrentIndex(6)  # Dügâh (La4)
        self.cmb_dst_perde.setCurrentIndex(6)
        self.cmb_src_perde.currentIndexChanged.connect(self._sync_interval_from_active_tab)
        self.cmb_dst_perde.currentIndexChanged.connect(self._sync_interval_from_active_tab)
        f_perde.addRow("Kaynak Karar:", self.cmb_src_perde)
        f_perde.addRow("Hedef Karar:", self.cmb_dst_perde)
        self.tabs_mode.addTab(tab_perde, "Karar Perdesi")

        # Sekme 3: Ney Ahenkleri
        tab_ahenk = QWidget()
        f_ahenk = QFormLayout(tab_ahenk)
        self.cmb_src_ahenk = QComboBox()
        self.cmb_dst_ahenk = QComboBox()
        for label, _, _ in NEY_AHENKLERI:
            self.cmb_src_ahenk.addItem(label)
            self.cmb_dst_ahenk.addItem(label)
        self.cmb_src_ahenk.setCurrentIndex(7)  # Mansur Ney
        self.cmb_dst_ahenk.setCurrentIndex(5)  # Kız Ney (+1 tam ses yukarı)

        self.cmb_ahenk_octave = QComboBox()
        self.cmb_ahenk_octave.addItem("-1 Oktav Pest (-7 Adım, -53 Koma)", -1)
        self.cmb_ahenk_octave.addItem("0 (Standart Göçürme Bölgesi)", 0)
        self.cmb_ahenk_octave.addItem("+1 Oktav Tiz (+7 Adım, +53 Koma)", 1)
        self.cmb_ahenk_octave.setCurrentIndex(1)

        self.cmb_src_ahenk.currentIndexChanged.connect(self._sync_interval_from_active_tab)
        self.cmb_dst_ahenk.currentIndexChanged.connect(self._sync_interval_from_active_tab)
        self.cmb_ahenk_octave.currentIndexChanged.connect(self._sync_interval_from_active_tab)
        f_ahenk.addRow("Elimdeki Ney:", self.cmb_src_ahenk)
        f_ahenk.addRow("İcra Edilecek Ahenk:", self.cmb_dst_ahenk)
        f_ahenk.addRow("Oktav Bölgesi:", self.cmb_ahenk_octave)
        self.tabs_mode.addTab(tab_ahenk, "Ney Ahenkleri")

        self.tabs_mode.currentChanged.connect(self._sync_interval_from_active_tab)
        tr_vbox.addWidget(self.tabs_mode)

        self.lbl_interval_summary = QLabel()
        self.lbl_interval_summary.setAlignment(Qt.AlignCenter)
        self.lbl_interval_summary.setStyleSheet(
            "background-color: #eef4fa; border: 1px solid #b8cfe5; border-radius: 4px; "
            "padding: 6px; font-weight: bold; color: #164370;"
        )
        tr_vbox.addWidget(self.lbl_interval_summary)
        left_vbox.addWidget(grp_tr)

        # 2b. Düzenlenebilir Donanım (KeySig) Tablosu
        grp_ks = QGroupBox("2. Makam Donanımı (Otomatik Hesaplanan ve Düzenlenebilir)")
        ks_vbox = QVBoxLayout(grp_ks)

        self.tbl_keysig = QTableWidget(7, 3)
        self.tbl_keysig.setHorizontalHeaderLabels(["Perde", "Kaynak Donanım", "Yeni Donanım"])
        self.tbl_keysig.verticalHeader().setVisible(False)
        self.tbl_keysig.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_keysig.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl_keysig.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tbl_keysig.setMinimumHeight(230)

        self._ks_combos: dict[str, QComboBox] = {}
        for row_i, step in enumerate(STEPS):
            it_step = QTableWidgetItem(f"{STEP_TR_NAMES[step]} ({step})")
            it_step.setFlags(Qt.ItemIsEnabled)
            self.tbl_keysig.setItem(row_i, 0, it_step)

            it_orig = QTableWidgetItem("Yok (0)")
            it_orig.setFlags(Qt.ItemIsEnabled)
            self.tbl_keysig.setItem(row_i, 1, it_orig)

            cmb = QComboBox()
            for c_val, c_txt in DONANIM_CHOICES:
                cmb.addItem(c_txt, c_val)
            cmb.currentIndexChanged.connect(self._on_user_edited_keysig_combo)
            self.tbl_keysig.setCellWidget(row_i, 2, cmb)
            self._ks_combos[step] = cmb

        ks_vbox.addWidget(self.tbl_keysig)

        self.btn_reset_ks = QPushButton("↺ Donanımı Otomatik Hesaba Sıfırla")
        self.btn_reset_ks.clicked.connect(self._reset_keysig_to_auto)
        ks_vbox.addWidget(self.btn_reset_ks)
        left_vbox.addWidget(grp_ks, 1)

        # 2c. Önizleme ve İsteğe Bağlı Kaydetme Kontrolleri
        grp_out = QGroupBox("3. Dönüştürme (Önizleme) ve İsteğe Bağlı Kaydetme")
        out_vbox = QVBoxLayout(grp_out)

        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("Varsayılan Klasör:"))
        self.ed_out_dir = QLineEdit(self.settings.get("last_output_dir", str(Path.home())))
        btn_dir = QPushButton("Seç...")
        btn_dir.clicked.connect(self._on_pick_out_dir)
        dir_row.addWidget(self.ed_out_dir, 1)
        dir_row.addWidget(btn_dir)
        out_vbox.addLayout(dir_row)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Önerilen Ad:"))
        self.ed_out_name = QLineEdit()
        self.ed_out_name.setPlaceholderText("Otomatik oluşturulur...")
        name_row.addWidget(self.ed_out_name, 1)
        out_vbox.addLayout(name_row)

        chk_row1 = QHBoxLayout()
        self.chk_tuning = QCheckBox("53-Koma Gerçek Akort (Cent)")
        self.chk_tuning.setChecked(bool(self.settings.get("apply_53edo_tuning", True)))
        self.chk_midi = QCheckBox("MIDI (.mid)")
        self.chk_midi.setChecked(bool(self.settings.get("export_midi", False)))
        self.chk_mp3 = QCheckBox("MP3 (.mp3)")
        self.chk_mp3.setChecked(bool(self.settings.get("export_mp3", False)))
        chk_row1.addWidget(self.chk_tuning)
        chk_row1.addWidget(self.chk_midi)
        chk_row1.addWidget(self.chk_mp3)
        out_vbox.addLayout(chk_row1)

        self.btn_preview = QPushButton("🔄 Transpoze Et ve Önizle (Kaydetmeden Göster)")
        self.btn_preview.setMinimumHeight(40)
        self.btn_preview.setEnabled(False)
        self.btn_preview.setStyleSheet(
            "QPushButton { background-color: #16537e; color: white; font-size: 11.5pt; font-weight: bold; border-radius: 5px; } "
            "QPushButton:disabled { background-color: #9da4ab; color: #ececec; }"
        )
        self.btn_preview.clicked.connect(self._on_preview_clicked)
        out_vbox.addWidget(self.btn_preview)

        self.btn_save = QPushButton("💾 Kaydet (.mscz + .pdf)...")
        self.btn_save.setMinimumHeight(38)
        self.btn_save.setEnabled(False)
        self.btn_save.setStyleSheet(
            "QPushButton { background-color: #1a6640; color: white; font-size: 11.5pt; font-weight: bold; border-radius: 5px; } "
            "QPushButton:disabled { background-color: #9da8a2; color: #ececec; }"
        )
        self.btn_save.clicked.connect(self._on_save_clicked)
        # Geriye dönük uyumluluk takma adı
        self.btn_generate = self.btn_save
        out_vbox.addWidget(self.btn_save)

        act_row = QHBoxLayout()
        self.btn_open_ms = QPushButton("🎹 MuseScore 4'te Aç")
        self.btn_open_ms.setEnabled(False)
        self.btn_open_ms.clicked.connect(self._on_open_in_musescore)

        self.btn_open_folder = QPushButton("📁 Kayıt Klasörünü Aç")
        self.btn_open_folder.clicked.connect(self._on_open_out_folder)
        act_row.addWidget(self.btn_open_ms)
        act_row.addWidget(self.btn_open_folder)
        out_vbox.addLayout(act_row)

        left_vbox.addWidget(grp_out)
        main_hsplit.addWidget(left_panel)

        # Sağ Bölüm: Üstte Yan Yana Önizleme, Altta Usûl Doğrulama & JSON Düzenleyici
        right_vsplit = QSplitter(Qt.Vertical)

        preview_hsplit = QSplitter(Qt.Horizontal)
        self.prev_source = PdfOrImagePreviewWidget("Kaynak Nota")
        self.prev_output = PdfOrImagePreviewWidget("Dönüştürülen Nota Önizlemesi")
        preview_hsplit.addWidget(self.prev_source)
        preview_hsplit.addWidget(self.prev_output)
        preview_hsplit.setSizes([500, 500])
        right_vsplit.addWidget(preview_hsplit)

        bottom_tabs = QTabWidget()
        # Sekme 1: Usûl / Ölçü Süre Kontrolü
        val_widget = QWidget()
        val_vbox = QVBoxLayout(val_widget)
        val_vbox.setContentsMargins(4, 4, 4, 4)
        self.txt_validation = QPlainTextEdit()
        self.txt_validation.setReadOnly(True)
        self.txt_validation.setPlaceholderText("Nota yüklendiğinde ölçü süre doğrulaması burada görüntülenir.")
        val_vbox.addWidget(self.txt_validation)
        bottom_tabs.addTab(val_widget, "✔ Usûl ve Ölçü Süre Kontrolü")

        # Sekme 2: Ara Nota JSON Düzenleyici
        json_widget = QWidget()
        json_vbox = QVBoxLayout(json_widget)
        json_vbox.setContentsMargins(4, 4, 4, 4)
        self.txt_json = QPlainTextEdit()
        self.txt_json.setFont(QFont("Consolas", 9))
        btn_apply_json = QPushButton("JSON Değişikliklerini Uygula")
        btn_apply_json.clicked.connect(self._on_apply_edited_json)
        json_vbox.addWidget(self.txt_json, 1)
        json_vbox.addWidget(btn_apply_json, 0, Qt.AlignRight)
        bottom_tabs.addTab(json_widget, "📝 Ara Nota JSON Düzenleyici")

        right_vsplit.addWidget(bottom_tabs)
        right_vsplit.setSizes([820, 115])
        main_hsplit.addWidget(right_vsplit)
        main_hsplit.setSizes([440, 1000])

        root_vbox.addWidget(main_hsplit, 1)

        # 3. Alt Durum Çubuğu
        status_hbox = QHBoxLayout()
        self.lbl_status = QLabel("Hazır.")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximumWidth(280)
        status_hbox.addWidget(self.lbl_status, 1)
        status_hbox.addWidget(self.progress_bar)
        root_vbox.addLayout(status_hbox)

    # ------------------------------------------------------------------
    # Göçürme Aralığı ve Donanım Senkronizasyonu
    # ------------------------------------------------------------------
    def current_interval(self) -> tuple[int, int]:
        tab_idx = self.tabs_mode.currentIndex()
        if tab_idx == 0:
            return self.spn_steps.value(), self.spn_commas.value()
        if tab_idx == 1:
            return interval_between_perdeler(
                self.cmb_src_perde.currentIndex(),
                self.cmb_dst_perde.currentIndex(),
            )
        st, cm = interval_between_ahenkler(
            self.cmb_src_ahenk.currentIndex(),
            self.cmb_dst_ahenk.currentIndex(),
        )
        oct_shift = int(self.cmb_ahenk_octave.currentData() or 0)
        return st + 7 * oct_shift, cm + 53 * oct_shift

    def current_target_keysig(self) -> dict[str, int]:
        return {step: int(cmb.currentData()) for step, cmb in self._ks_combos.items()}

    def _on_preset_changed(self, idx: int) -> None:
        if self._updating_ui or not (0 <= idx < len(INTERVAL_PRESETS)):
            return
        _, st, cm = INTERVAL_PRESETS[idx]
        self._updating_ui = True
        self.spn_steps.setValue(st)
        self.spn_commas.setValue(cm)
        self._updating_ui = False
        self.keysig_user_overridden = False
        if self.loaded_state is not None:
            self.has_unsaved_changes = True
        self._refresh_interval_and_keysig_ui()

    def _on_manual_spin_changed(self) -> None:
        if self._updating_ui:
            return
        self.keysig_user_overridden = False
        if self.loaded_state is not None:
            self.has_unsaved_changes = True
        self._refresh_interval_and_keysig_ui()

    def _sync_interval_from_active_tab(self) -> None:
        if self._updating_ui:
            return
        self.keysig_user_overridden = False
        if self.loaded_state is not None:
            self.has_unsaved_changes = True
        self._refresh_interval_and_keysig_ui()

    def _on_user_edited_keysig_combo(self) -> None:
        if self._updating_ui:
            return
        self.keysig_user_overridden = True
        if self.loaded_state is not None:
            self.has_unsaved_changes = True

    def _reset_keysig_to_auto(self) -> None:
        self.keysig_user_overridden = False
        self._refresh_interval_and_keysig_ui()

    def _refresh_interval_and_keysig_ui(self) -> None:
        steps, commas = self.current_interval()
        if steps == 0 and commas == 0:
            self.lbl_interval_summary.setText("Seçili İşlem: Bire Bir Replika (0 Diyatonik Adım, 0 Koma)")
        else:
            self.lbl_interval_summary.setText(
                f"Seçili Göçürme Aralığı: {steps:+d} Diyatonik Adım,  {commas:+d} Koma (53-AEU)"
            )

        orig_ks = self.loaded_state.orig_keysig if self.loaded_state else {s: 0 for s in STEPS}
        auto_ks = transpose_keysig_map(orig_ks, steps, commas)

        self._updating_ui = True
        for row_i, step in enumerate(STEPS):
            oc = orig_ks.get(step, 0)
            orig_txt = "Yok (0)" if oc == 0 else nearest_aeu_accidental(oc).tr_name
            self.tbl_keysig.item(row_i, 1).setText(orig_txt)

            if not self.keysig_user_overridden:
                tc = nearest_aeu_accidental(auto_ks.get(step, 0)).commas if auto_ks.get(step, 0) != 0 else 0
                cmb = self._ks_combos[step]
                idx = cmb.findData(tc)
                cmb.setCurrentIndex(idx if idx >= 0 else 0)

        if self.loaded_state:
            self.ed_out_name.setText(
                default_output_stem(self.loaded_state.input_path.stem, steps, commas)
            )
        self._updating_ui = False

    # ------------------------------------------------------------------
    # Dosya Yükleme ve Çıktı Üretme (Hem Senkron Test Hem QThread Destekli)
    # ------------------------------------------------------------------
    def _on_browse_input(self) -> None:
        if self.confirm_unsaved_on_close and self.loaded_state is not None and self.has_unsaved_changes:
            choice = self._ask_unsaved_action(
                "Mevcut dönüştürülen nota henüz kaydedilmedi.\nYeni bir nota açmadan önce kaydetmek ister misiniz?",
                discard_label="Kaydetmeden Devam Et",
            )
            if choice == "cancel":
                return
            if choice == "save" and not self._prompt_save_synchronously():
                return

        filt = (
            "Desteklenen Notalar (*.pdf *.png *.jpg *.jpeg *.tif *.bmp *.mscz *.mscx *.musicxml *.xml *.mxl *.json);;"
            "PDF ve Görüntü Notaları (*.pdf *.png *.jpg *.jpeg *.tif *.bmp);;"
            "MuseScore ve MusicXML (*.mscz *.mscx *.musicxml *.xml *.mxl);;"
            "Önbellek JSON (*.json)"
        )
        start_dir = self.ed_out_dir.text().strip() or str(Path.home())
        p, _ = QFileDialog.getOpenFileName(self, "Nota Dosyası Seçin", start_dir, filt)
        if p:
            self.start_load_async(Path(p))

    def start_load_async(self, path: Path) -> None:
        self._set_busy(True, f"Yükleniyor: {path.name}...")
        omr_mode = str(self.cmb_omr_mode.currentData())
        src_prev_dir = self._tmp_dir / "src_prev"
        worker = LoadWorker(path, omr_mode, self.settings, src_prev_dir)
        worker.progress.connect(self._on_worker_progress)
        worker.finished_ok.connect(self._on_load_succeeded)
        worker.failed.connect(self._on_worker_failed)
        self._worker = worker
        worker.start()

    def load_file_sync(self, path: str | Path, omr_mode: str = "opencv") -> LoadedScoreState:
        """Otomatik testler ve betikler için senkron dosya yükleme giriş noktası."""
        p = Path(path).resolve()
        state = load_score_source(p, omr_mode=omr_mode, settings=self.settings)
        src_pngs: list[Path] = []
        if p.suffix.lower() in DIGITAL_EXTS and state.raw_mscx_xml:
            src_pngs = export_from_mscx_string(
                state.raw_mscx_xml,
                preview_png_dir=self._tmp_dir / "src_prev",
                dpi=140,
                musescore_exe=self.settings.get("musescore_exe"),
            )
        self._on_load_succeeded(state, src_pngs)
        return state

    def _on_load_succeeded(self, state: LoadedScoreState, src_pngs: list[Path]) -> None:
        self._set_busy(False, f"Yüklendi: {state.input_path.name} ({len(state.score_doc.measures)} ölçü)")
        self.loaded_state = state
        self.last_export = None
        self.last_saved_export = None
        self.has_unsaved_changes = False
        self.force_score_doc_pipeline = False
        self.keysig_user_overridden = False

        self.lbl_loaded_file.setText(f"Yüklü: {state.input_path.name}  [{state.source_kind}]")
        self.ed_out_dir.setText(str(state.input_path.parent))
        self.btn_preview.setEnabled(True)
        self.btn_save.setEnabled(True)
        self.btn_open_ms.setEnabled(False)
        self.btn_save_json.setEnabled(True)

        ext = state.input_path.suffix.lower()
        if ext == ".pdf":
            self.prev_source.load_pdf(state.input_path)
        elif src_pngs:
            self.prev_source.load_image_files(src_pngs)
        else:
            self.prev_source.load_image_files([state.input_path])

        self.prev_output.lbl_title.setText("<b>Dönüştürülen Nota Önizlemesi</b>")
        self.prev_output.clear_view(
            "Notayı diske kaydetmeden görmek için '🔄 Transpoze Et ve Önizle' düğmesine tıklayın."
        )
        self.txt_json.setPlainText(state.score_doc.to_json(indent=2))
        self._update_validation_panel(state.score_doc)
        self._refresh_interval_and_keysig_ui()

    def _update_validation_panel(self, doc: ScoreDoc) -> None:
        issues = validate_score_durations(doc)
        if not issues:
            self.txt_validation.setPlainText(
                f"✅ Tüm ölçüler ({len(doc.measures)} adet) tanımlı usûl/ölçü süreleriyle tam uyumlu."
            )
        else:
            lines = [f"⚠ {len(issues)} ölçüde süre uyuşmazlığı bulundu (JSON sekmesinden düzenleyebilirsiniz):"]
            for iss in issues:
                lines.append(f"  • {iss.message}")
            self.txt_validation.setPlainText("\n".join(lines))

    def _on_apply_edited_json(self) -> None:
        if not self.loaded_state:
            return
        try:
            import json

            raw = json.loads(self.txt_json.toPlainText())
            new_doc = ScoreDoc.from_dict(raw)
            self.loaded_state.score_doc = new_doc
            self.loaded_state.orig_keysig = {s: new_doc.keysig.get(s, 0) for s in STEPS}
            self.force_score_doc_pipeline = True
            self.keysig_user_overridden = False
            self.has_unsaved_changes = True
            self._update_validation_panel(new_doc)
            self._refresh_interval_and_keysig_ui()
            self.lbl_status.setText("JSON değişiklikleri uygulandı. Görmek için 'Transpoze Et ve Önizle'ye tıklayın.")
        except Exception as exc:
            QMessageBox.critical(self, "JSON Hatası", f"JSON ayrıştırılamadı:\n{exc}")

    def _build_preview_kwargs(self) -> dict:
        """Diske/kullanıcı klasörüne hiçbir dosya yazmadan geçici önizleme üretmek için parametreler."""
        assert self.loaded_state is not None
        steps, commas = self.current_interval()
        custom_ks = self.current_target_keysig() if self.keysig_user_overridden else None
        prev_dir = self._tmp_dir / "live_preview"
        return {
            "state": self.loaded_state,
            "out_dir": prev_dir,
            "base_name": "onizleme_gecici",
            "diatonic_steps": steps,
            "comma_shift": commas,
            "custom_keysig_override": custom_ks,
            "force_score_doc_pipeline": self.force_score_doc_pipeline,
            "apply_53edo_tuning": self.chk_tuning.isChecked(),
            "export_midi": False,
            "export_mp3": False,
            "save_json": False,
            "musescore_exe": self.settings.get("musescore_exe"),
        }

    def _build_export_kwargs(self, out_dir: Path | None = None, base_name: str | None = None) -> dict:
        assert self.loaded_state is not None
        steps, commas = self.current_interval()
        custom_ks = self.current_target_keysig() if self.keysig_user_overridden else None
        resolved_dir = out_dir or Path(self.ed_out_dir.text().strip() or str(self.loaded_state.input_path.parent))
        resolved_name = base_name or self.ed_out_name.text().strip() or None

        save_settings({
            "apply_53edo_tuning": self.chk_tuning.isChecked(),
            "export_midi": self.chk_midi.isChecked(),
            "export_mp3": self.chk_mp3.isChecked(),
            "last_output_dir": str(resolved_dir),
        })

        return {
            "state": self.loaded_state,
            "out_dir": resolved_dir,
            "base_name": resolved_name,
            "diatonic_steps": steps,
            "comma_shift": commas,
            "custom_keysig_override": custom_ks,
            "force_score_doc_pipeline": self.force_score_doc_pipeline,
            "apply_53edo_tuning": self.chk_tuning.isChecked(),
            "export_midi": self.chk_midi.isChecked(),
            "export_mp3": self.chk_mp3.isChecked(),
            "save_json": False,
            "musescore_exe": self.settings.get("musescore_exe"),
        }

    def _on_preview_clicked(self) -> None:
        if not self.loaded_state:
            return
        kw = self._build_preview_kwargs()
        self._set_busy(True, "Transpoze önizlemesi hazırlanıyor (diske kaydedilmez)...")
        worker = ExportWorker(kw)
        worker.progress.connect(self._on_worker_progress)
        worker.finished_ok.connect(self._on_preview_succeeded)
        worker.failed.connect(self._on_worker_failed)
        self._worker = worker
        worker.start()

    def preview_current_sync(self) -> ExportResult:
        """Otomatik testler için diske kaydetmeden senkron önizleme metodu."""
        kw = self._build_preview_kwargs()
        res = export_transposed_score(**kw)
        self._on_preview_succeeded(res)
        return res

    def _on_preview_succeeded(self, res: ExportResult) -> None:
        self.last_export = res
        self.has_unsaved_changes = True
        self._set_busy(
            False,
            "👁 Önizleme hazır (henüz diske kaydedilmedi). Kaydetmek için '💾 Kaydet (.mscz + .pdf)...' düğmesini kullanın.",
        )
        self.btn_open_ms.setEnabled(True)
        self.prev_output.lbl_title.setText("<b>Dönüştürülen Nota Önizlemesi (Henüz Kaydedilmedi)</b>")
        if res.pdf_path.is_file():
            self.prev_output.load_pdf(res.pdf_path)

    def _on_save_clicked(self) -> None:
        if not self.loaded_state:
            return
        steps, commas = self.current_interval()
        suggested_stem = self.ed_out_name.text().strip() or default_output_stem(
            self.loaded_state.input_path.stem, steps, commas
        )
        default_dir = Path(self.ed_out_dir.text().strip() or str(self.loaded_state.input_path.parent))
        default_file = default_dir / f"{suggested_stem}.mscz"

        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "Dönüştürülen Notayı Kaydet (.mscz + .pdf birlikte kaydedilir)",
            str(default_file),
            "MuseScore ve PDF Çıktısı (*.mscz *.pdf)",
        )
        if not chosen:
            return

        chosen_path = Path(chosen)
        target_dir = chosen_path.parent
        target_stem = chosen_path.stem
        self.ed_out_dir.setText(str(target_dir))
        self.ed_out_name.setText(target_stem)

        kw = self._build_export_kwargs(out_dir=target_dir, base_name=target_stem)
        self._set_busy(True, f"Kaydediliyor: {target_stem}.mscz ve {target_stem}.pdf...")
        worker = ExportWorker(kw)
        worker.progress.connect(self._on_worker_progress)
        worker.finished_ok.connect(self._on_export_succeeded)
        worker.failed.connect(self._on_worker_failed)
        self._worker = worker
        worker.start()

    def export_current_sync(self) -> ExportResult:
        """Otomatik testler için senkron kaydetme/dışa aktarma metodu."""
        kw = self._build_export_kwargs()
        res = export_transposed_score(**kw)
        self._on_export_succeeded(res)
        return res

    def _on_export_succeeded(self, res: ExportResult) -> None:
        self.last_export = res
        self.last_saved_export = res
        self.has_unsaved_changes = False
        self._set_busy(False, f"✅ Kaydedildi: {res.mscz_path}  |  {res.pdf_path.name}")
        self.btn_open_ms.setEnabled(True)
        self.prev_output.lbl_title.setText(f"<b>Kaydedilen Çıktı: {res.pdf_path.name}</b>")
        if res.pdf_path.is_file():
            self.prev_output.load_pdf(res.pdf_path)

    def _on_save_intermediate_json(self) -> None:
        if not self.loaded_state:
            return
        default_p = self.loaded_state.input_path.with_suffix(".json")
        p, _ = QFileDialog.getSaveFileName(self, "Ara JSON Kaydet", str(default_p), "JSON (*.json)")
        if p:
            self.loaded_state.score_doc.save_json(p)
            self.lbl_status.setText(f"Ara JSON kaydedildi: {p}")

    def _on_open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec_() == QDialog.Accepted:
            upd = dlg.get_updated_settings()
            save_settings(upd)
            self.settings = load_settings()
            self.lbl_status.setText("Ayarlar kaydedildi.")

    def _on_pick_out_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Varsayılan Kayıt Klasörü Seçin", self.ed_out_dir.text().strip())
        if d:
            self.ed_out_dir.setText(d)

    def _on_open_in_musescore(self) -> None:
        if self.last_export and self.last_export.mscz_path.is_file():
            open_in_musescore_gui(self.last_export.mscz_path, self.settings.get("musescore_exe"))

    def _on_open_out_folder(self) -> None:
        if self.last_saved_export and self.last_saved_export.mscz_path.parent.is_dir():
            os.startfile(str(self.last_saved_export.mscz_path.parent))
            return
        d = self.ed_out_dir.text().strip()
        if d and os.path.isdir(d):
            os.startfile(d)

    def _on_worker_progress(self, pct: int, msg: str) -> None:
        self.progress_bar.setValue(pct)
        self.lbl_status.setText(msg)

    def _on_worker_failed(self, err_msg: str) -> None:
        self._set_busy(False, "İşlem sırasında hata oluştu.")
        QMessageBox.critical(self, "Hata", err_msg)

    def _set_busy(self, busy: bool, msg: str) -> None:
        self.btn_load.setEnabled(not busy)
        if self.loaded_state:
            self.btn_preview.setEnabled(not busy)
            self.btn_save.setEnabled(not busy)
        self.lbl_status.setText(msg)
        self.progress_bar.setValue(10 if busy else 100)


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
