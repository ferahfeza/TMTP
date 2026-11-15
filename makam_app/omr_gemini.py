"""
Gemini API (Interactions API — google-genai >= 3.2.0) Çok-Modlu Yüksek Doğruluklu Türk Müziği OMR Motoru.
Her sayfa başlığını ve kırpılmış porte satırını (StaffSystemCrop) yüksek DPI PNG olarak gönderir,
yapılandırılmış JSON şeması (Pydantic) ile 53-koma AEU donanım, usûl, arıza, bağ, çarpma ve güfteleri çıkarır,
süre toplamı uyuşmayan satırları otomatik yeniden sorgulayarak düzeltir.
"""

import base64
import io
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np
from google import genai
from PIL import Image
from pydantic import BaseModel, Field

from .omr_slicer import StaffSystemCrop, detect_and_slice_systems, render_input_pages
from .score_model import (
    MeasureSpec,
    NoteEvent,
    ScoreDoc,
    auto_beam_measure_events,
    validate_score_durations,
)


# ---------------------------------------------------------------------------
# Yapılandırılmış Çıktı (Structured Output) Pydantic Şemaları
# ---------------------------------------------------------------------------
class KeySigSignSchema(BaseModel):
    step: str = Field(description="Diyatonik ses harfi: 'C', 'D', 'E', 'F', 'G', 'A' veya 'B'")
    commas: int = Field(
        description=(
            "53-koma AEU arıza değeri: -8 (büyük mücenneb bemolü), -5 (küçük mücenneb bemolü ♭), "
            "-4 (bakiye bemolü), -1 (koma bemolü), +1 (koma diyezi), +4 (bakiye diyezi #), "
            "+5 (küçük mücenneb diyezi), +8 (büyük mücenneb diyezi)"
        )
    )


class HeaderMetadataSchema(BaseModel):
    title: str = Field(description="Eserin ana başlığı (örn. 'Şevkefzâ Saz Semâî')")
    subtitle: str = Field(default="", description="Alt başlık veya usûl açıklaması")
    composer: str = Field(default="", description="Bestekâr adı")
    lyricist: str = Field(default="", description="Güftekâr adı (sözlü eserse)")
    nominal_ts_num: int = Field(default=4, description="Görünür usûl payı (örn. 10/8 için 10, 28/4 için 28)")
    nominal_ts_den: int = Field(default=4, description="Görünür usûl paydası (örn. 10/8 için 8, 28/4 için 4)")
    tempo_text: str = Field(default="", description="Tempo / usûl yazısı (örn. 'Aksak Semâî ♪ = 112')")
    tempo_qpm: float = Field(default=80.0, description="Dakikadaki dörtlük vuruş hızı")
    keysig: List[KeySigSignSchema] = Field(default_factory=list, description="Sol anahtarındaki AEU donanım işaretleri")


class NoteEventSchema(BaseModel):
    kind: str = Field(description="'note' veya 'rest'")
    step: str = Field(default="C", description="Nota adı: 'C','D','E','F','G','A','B' (Sol anahtarında alt çizgi E4)")
    octave: int = Field(default=5, description="Oktav numarası (C4=orta Do, alt çizgi E4, 3. çizgi B4, üst boşluk E5)")
    duration: str = Field(
        description="Süre: 'whole', 'half', 'half.', 'quarter', 'quarter.', 'eighth', 'eighth.', '16th', '32nd'"
    )
    beam: str = Field(default="no", description="'no', 'begin', 'mid' veya 'end'")
    accidental_commas: Optional[int] = Field(
        default=None,
        description=(
            "Yalnızca bu notanın solunda AÇIKÇA basılmış arıza varsa koma değeri "
            "(-8,-5,-4,-1, 0 [natürel ♮], +1,+4 [#],+5,+8). Donanımdan geliyorsa null bırak."
        ),
    )
    cautionary: bool = Field(default=False, description="Arıza parantez içindeyse true")
    tie_start: bool = Field(default=False, description="Sonraki aynı perdeye uzatma bağı başlıyorsa true")
    tie_end: bool = Field(default=False, description="Önceki notadan gelen uzatma bağı bitiyorsa true")
    grace_before: Optional[str] = Field(default=None, description="Nota önünde küçük çarpma notası varsa örn. 'D5'")
    lyric: Optional[str] = Field(default=None, description="Nota altındaki güfte hecesi (tireliyse sonuna '-' ekle)")


class MeasureSchema(BaseModel):
    actual_len: str = Field(description="Bu ölçünün gerçek toplam süresi, örn. '10/8', '6/4', '4/4'")
    section_label: Optional[str] = Field(default=None, description="Satır başı hâne etiketi: '1. Hâne', 'Teslim' vb.")
    segno_start: bool = Field(default=False, description="Ölçü başında Segno (𝄋) işareti varsa true")
    segno_end: bool = Field(default=False, description="Ölçü sonunda Segno işareti varsa true")
    start_repeat: bool = Field(default=False, description="Ölçü başında röpriz açılışı (|:) varsa true")
    end_repeat: bool = Field(default=False, description="Ölçü sonunda röpriz kapanışı (:|) varsa true")
    volta: Optional[int] = Field(default=None, description="1. veya 2. dolap kutusu varsa 1 veya 2")
    barline: Optional[str] = Field(default=None, description="Özel ölçü çizgisi: 'double' veya 'end', normal ise null")
    end_text: Optional[str] = Field(default=None, description="Ölçü altında '— Son —' vb. bitiş yazısı")
    events: List[NoteEventSchema] = Field(description="Ölçü içindeki sıralı nota ve es olayları")


class SystemTranscriptionSchema(BaseModel):
    measures: List[MeasureSchema] = Field(description="Bu porte satırında soldan sağa yer alan tüm ölçüler")


def _rgb_to_png_b64(rgb: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


SYSTEM_OMR_INSTRUCTIONS = """Sen uzman bir Türk Musikisi (53-Koma Arel-Ezgi-Uzdilek sistemi) nota dizgicisi ve OMR çözümleyicisisin.
Verilen tek porteli nota satırı görüntüsündeki tüm ölçüleri soldan sağa eksiksiz oku:
1. Sol anahtarında çizgiler aşağıdan yukarıya: E4, G4, B4, D5, F5; boşluklar: F4, A4, C5, E5. Porte üstü ilk çizgi A5.
2. Her ölçüdeki nota ve es sürelerinin toplamı `actual_len` kesrine (örn. 10/8, 6/4, 4/4) TAM EŞİT olmalıdır.
   (Bileşik Türk usûllerinde örn. Devr-i Kebîr 28/4 satır içinde 6/4 + 4/4 + 4/4 şeklinde bölünmüş olabilir; her ölçünün gerçek nota toplamını `actual_len` olarak yaz.)
3. Yalnızca notanın solunda açıkça basılmış değiştirici işaret varsa `accidental_commas` alanını doldur:
   - Natürel (♮) -> 0
   - Koma bemolü (ters bemol d) -> -1
   - Bakiye bemolü (çengelli bemol) -> -4
   - Küçük mücenneb bemolü (standart ♭) -> -5
   - Koma diyezi -> +1
   - Bakiye diyezi (standart #, 2 yatay çizgili) -> +4
   - Küçük mücenneb diyezi (1 dikey + 3 yatay çizgili) -> +5
4. Kirişli (beamed) sekizlik/onaltılık gruplarında grup başını 'begin', ortasını 'mid', sonunu 'end' işaretle.
5. Nota altında güfte (şarkı sözü) varsa her heceyi ilgili notanın `lyric` alanına yaz.
"""


def recognize_score_gemini(
    input_path: str | Path,
    api_key: str,
    model_name: str = "gemini-4-hydrogen",
    dpi: int = 350,
    max_retries_per_system: int = 2,
    progress_cb: Callable[[int, str], None] | None = None,
) -> ScoreDoc:
    """
    Gemini Interactions API kullanarak yüksek doğruluklu çok-modlu Türk müziği OMR gerçekleştirir.
    """
    if not api_key or not api_key.strip():
        raise ValueError("Gemini API anahtarı girilmemiş. Lütfen Ayarlar bölümünden API anahtarınızı girin veya Çevrimdışı OpenCV modunu seçin.")

    # Eski/kullanımdan kalkmış model adı kalmışsa güncel modele yükselt
    if model_name.startswith(("gemini-1.5", "gemini-2.0", "gemini-2.5")):
        model_name = "gemini-4-hydrogen"

    client = genai.Client(api_key=api_key.strip())

    if progress_cb:
        progress_cb(5, "Nota sayfaları dilimleniyor...")

    pages = render_input_pages(input_path, dpi=dpi)
    header_rgb, crops = detect_and_slice_systems(pages)
    if not crops:
        raise RuntimeError("Görüntüde 5 çizgili porte satırı bulunamadı.")

    # 1. Başlık + İlk Porte Satırından Eser Bilgisi, Donanım ve Usûl Çıkarımı
    if progress_cb:
        progress_cb(12, "Başlık, AEU donanım ve usûl bilgileri okunuyor (Gemini API)...")

    overview_inputs: list[dict] = [
        {
            "type": "text",
            "text": (
                "Bu Türk müziği notasının üst başlık ve ilk porte satırı görüntülerinden: "
                "eser adını, bestekârı, görünür usûl rakamını (örn. 10/8 veya 28/4), tempo yazısını ve "
                "Sol anahtarındaki 53-koma Arel-Ezgi-Uzdilek donanım işaretlerini JSON şemasına uygun çıkar. "
                "Hatırlatma: Türk müziğinde standart # işareti +4 koma (bakiye diyezi), ters bemol -1 koma (koma bemolü), "
                "eğik çizgili bemol -4 koma (bakiye bemolü), standart bemol -5 komadır."
            ),
        }
    ]
    if header_rgb is not None:
        overview_inputs.append({"type": "image", "data": _rgb_to_png_b64(header_rgb), "mime_type": "image/png"})
    overview_inputs.append({"type": "image", "data": _rgb_to_png_b64(crops[0].crop_rgb), "mime_type": "image/png"})

    hdr_inter = client.interactions.create(
        model=model_name,
        input=overview_inputs,
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": HeaderMetadataSchema.model_json_schema(),
        },
        store=False,
    )
    hdr = HeaderMetadataSchema.model_validate_json(hdr_inter.output_text)
    ks_dict = {item.step.upper(): item.commas for item in hdr.keysig if item.commas != 0}

    # 2. Her Porte Satırını Ayrı Ayrı Oku ve Süre Doğrulamasıyla Kontrol Et
    all_measures: list[MeasureSpec] = []
    m_counter = 1
    total_sys = len(crops)

    for idx, crop in enumerate(crops):
        if progress_cb:
            pct = 18 + int(76 * (idx + 1) / max(1, total_sys))
            progress_cb(pct, f"Porte satırı {idx + 1}/{total_sys} okunuyor (Gemini API)...")

        crop_b64 = _rgb_to_png_b64(crop.crop_rgb)
        extra_feedback = ""
        parsed_measures: list[MeasureSpec] = []

        for attempt in range(max_retries_per_system + 1):
            prompt_txt = (
                f"{SYSTEM_OMR_INSTRUCTIONS}\n"
                f"Eserin genel usûlü: {hdr.nominal_ts_num}/{hdr.nominal_ts_den}. "
                f"Donanım: {ks_dict}. Bu görüntü {idx + 1}. porte satırıdır."
            )
            if extra_feedback:
                prompt_txt += f"\nÖNCEKİ DENEMEDE SÜRE HATASI TESPİT EDİLDİ, LÜTFEN DÜZELT:\n{extra_feedback}"

            sys_inter = client.interactions.create(
                model=model_name,
                input=[
                    {"type": "text", "text": prompt_txt},
                    {"type": "image", "data": crop_b64, "mime_type": "image/png"},
                ],
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": SystemTranscriptionSchema.model_json_schema(),
                },
                store=False,
            )
            sys_data = SystemTranscriptionSchema.model_validate_json(sys_inter.output_text)

            candidate_measures: list[MeasureSpec] = []
            temp_num = m_counter
            for ms_i, ms in enumerate(sys_data.measures):
                evs = [
                    NoteEvent(
                        kind=e.kind,
                        step=e.step.upper(),
                        octave=e.octave,
                        duration=e.duration,
                        beam=e.beam,
                        accidental_commas=e.accidental_commas,
                        cautionary=e.cautionary,
                        tie_start=e.tie_start,
                        tie_end=e.tie_end,
                        grace_before=e.grace_before,
                        lyric=e.lyric,
                    )
                    for e in ms.events
                ]
                mspec = MeasureSpec(
                    number=temp_num,
                    actual_len=ms.actual_len,
                    section_label=ms.section_label,
                    segno_start=ms.segno_start,
                    segno_end=ms.segno_end,
                    start_repeat=ms.start_repeat,
                    end_repeat=ms.end_repeat,
                    volta=ms.volta,
                    barline=ms.barline,
                    end_text=ms.end_text,
                    line_break=(ms_i == len(sys_data.measures) - 1 and idx < total_sys - 1),
                    page_break=(
                        ms_i == len(sys_data.measures) - 1
                        and idx < total_sys - 1
                        and crops[idx + 1].page_index > crop.page_index
                    ),
                    events=evs,
                )
                auto_beam_measure_events(mspec)
                candidate_measures.append(mspec)
                temp_num += 1

            temp_doc = ScoreDoc(measures=candidate_measures)
            issues = validate_score_durations(temp_doc)
            if not issues or attempt == max_retries_per_system:
                parsed_measures = candidate_measures
                break
            extra_feedback = "\n".join(iss.message for iss in issues)

        all_measures.extend(parsed_measures)
        m_counter += len(parsed_measures)

    if all_measures:
        all_measures[0].nominal_ts = [hdr.nominal_ts_num, hdr.nominal_ts_den]
        if hdr.tempo_text:
            all_measures[0].tempo_text = hdr.tempo_text
            all_measures[0].tempo_qpm = hdr.tempo_qpm

    doc = ScoreDoc(
        title=hdr.title or Path(input_path).stem,
        subtitle=hdr.subtitle,
        composer=hdr.composer,
        lyricist=hdr.lyricist,
        keysig=ks_dict,
        measures=all_measures,
    )
    if progress_cb:
        progress_cb(96, f"Gemini OMR tamamlandı ({len(all_measures)} ölçü).")
    return doc
