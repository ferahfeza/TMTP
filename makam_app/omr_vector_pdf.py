"""
Vektörel Nota PDF Çözümleyici (Finale Petrucci/Maestro + TURKEY AEU Fontu ve MuseScore/SMuFL Leland/Bravura).
Görüntü tabanlı OMR hatası olmadan, PDF içindeki vektör çizgileri (porte, ölçü çizgisi, sap, kiriş, dolap)
ve müzik fontu gliflerini (nota başları, 53-koma AEU arızaları, esler, usûl rakamları ve güfte hecelerini)
doğrudan koordinatlarıyla okuyarak %100 deterministik ScoreDoc üretir.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable

import fitz  # PyMuPDF

from .aeu_theory import STEPS
from .score_model import (
    MeasureSpec,
    NoteEvent,
    ScoreDoc,
    auto_beam_measure_events,
    duration_to_fraction,
)

# ---------------------------------------------------------------------------
# Müzik Fontu Karakter Eşleme Tabloları
# ---------------------------------------------------------------------------
MUSIC_FONT_KEYWORDS = (
    "petrucci",
    "maestro",
    "engraver",
    "turkey",
    "leland",
    "bravura",
    "opus",
    "helsinki",
    "emmentaler",
    "gonville",
    "scorlatti",
)

# Finale TURKEY (Arel-Ezgi-Uzdilek) fontu -> 53-koma değeri
TURKEY_CHAR_TO_COMMAS: dict[str, int] = {
    "q": -1,  # Koma bemolü
    "w": -4,  # Bakiye bemolü
    "e": -5,  # Küçük mücenneb bemolü (♭)
    "r": -8,  # Büyük mücenneb bemolü
    "a": 1,   # Koma diyezi
    "s": 4,   # Bakiye diyezi (#)
    "d": 5,   # Küçük mücenneb diyezi
    "f": 8,   # Büyük mücenneb diyezi
    "z": 0,   # Natürel (♮)
}

# Petrucci / Maestro standart arızalar -> (commas, is_cautionary)
PETRUCCI_ACCIDENTALS: dict[int, tuple[int, bool]] = {
    110: (0, False),   # 'n' -> Natürel
    78: (0, True),     # 'N' -> Parantezli natürel (♮)
    98: (-5, False),   # 'b' -> Bemol
    66: (-5, True),    # 'B' -> Parantezli bemol
    35: (4, False),    # '#' -> Diyez (AEU bakiye diyezi)
    97: (4, True),     # 'a' -> Parantezli diyez
}

# SMuFL (Leland / Bravura) arızalar -> commas
SMUFL_ACCIDENTALS: dict[int, int] = {
    0xE260: -5,  # accidentalFlat
    0xE261: 0,   # accidentalNatural
    0xE262: 4,   # accidentalSharp
    0xE263: 8,   # accidentalDoubleSharp
    0xE264: -8,  # accidentalDoubleFlat
    0xE280: -1,  # accidentalQuarterToneFlatStein / NarrowReversedFlat
    0xE440: -8,  # accidentalBuyukMucennebFlat
    0xE441: -5,  # accidentalKucukMucennebFlat
    0xE442: -4,  # accidentalBakiyeFlat
    0xE443: -1,  # accidentalKomaFlat
    0xE444: 1,   # accidentalKomaSharp
    0xE445: 4,   # accidentalBakiyeSharp
    0xE446: 5,   # accidentalKucukMucennebSharp
    0xE447: 8,   # accidentalBuyukMucennebSharp
}

# Petrucci / Maestro es (rest) kodları
PETRUCCI_RESTS: dict[int, str] = {
    211: "whole",    # 'Ó'
    183: "whole",
    238: "half",     # 'î'
    206: "quarter",  # 'Î'
    156: "quarter",
    228: "eighth",   # 'ä'
    197: "16th",     # 'Å'
    244: "16th",     # 'ô'
    168: "32nd",     # '¨'
}

# SMuFL es kodları
SMUFL_RESTS: dict[int, str] = {
    0xE4E3: "whole",
    0xE4E4: "half",
    0xE4E5: "quarter",
    0xE4E6: "eighth",
    0xE4E7: "16th",
    0xE4E8: "32nd",
}

# Petrucci / Maestro tekil nota çengelleri (flag) -> taban süre
PETRUCCI_FLAGS: dict[int, str] = {
    74: "eighth",   # 'J' (down)
    106: "eighth",  # 'j' (up)
    75: "16th",     # 'K'
    107: "16th",    # 'k'
    82: "16th",     # 'R'
    114: "16th",    # 'r'
}

SMUFL_FLAGS: dict[int, str] = {
    0xE240: "eighth",
    0xE241: "eighth",
    0xE242: "16th",
    0xE243: "16th",
    0xE244: "32nd",
    0xE245: "32nd",
}


@dataclass
class _GlyphItem:
    font: str
    char: str
    code: int
    size: float
    x: float
    y: float
    bbox: tuple[float, float, float, float]


@dataclass
class _TextToken:
    text: str
    font: str
    size: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def xc(self) -> float:
        return 0.5 * (self.x0 + self.x1)

    @property
    def yc(self) -> float:
        return 0.5 * (self.y0 + self.y1)


@dataclass
class _VectorSystem:
    page_idx: int
    ys: list[float]          # [y0, y1, y2, y3, y4]
    x_left: float
    x_right: float
    ss: float                # staff_space = (y4 - y0) / 4.0


def _is_music_font(font_name: str) -> bool:
    fl = font_name.lower()
    return any(k in fl for k in MUSIC_FONT_KEYWORDS)


def has_vector_music_notation(pdf_path: str | Path) -> bool:
    """PDF dosyasının vektörel porte çizgileri ve gömülü müzik fontu içerip içermediğini denetler."""
    p = Path(pdf_path)
    if p.suffix.lower() != ".pdf" or not p.is_file():
        return False
    try:
        doc = fitz.open(str(p))
        if len(doc) == 0:
            doc.close()
            return False
        pg = doc[0]
        fonts = [f[3].lower() for f in pg.get_fonts()]
        has_mfont = any(_is_music_font(fn) for fn in fonts)
        if not has_mfont:
            # SMuFL Private Use kontrolü
            txt = pg.get_text()
            has_mfont = any(0xE040 <= ord(ch) <= 0xE5FF for ch in txt)
        if not has_mfont:
            doc.close()
            return False
        h_lines = [d for d in pg.get_drawings() if d["rect"].width > 160 and d["rect"].height < 2.2]
        doc.close()
        return len(h_lines) >= 5
    except Exception:
        return False


def _extract_systems_on_page(page: fitz.Page, page_idx: int) -> list[_VectorSystem]:
    """Vektör çizimlerinden 5'li porte gruplarını hassas koordinatlarıyla çıkarır."""
    raw_h: list[tuple[float, float, float]] = []
    for d in page.get_drawings():
        r = d["rect"]
        if r.width > 160.0 and r.height < 2.0:
            yc = 0.5 * (r.y0 + r.y1)
            raw_h.append((yc, float(r.x0), float(r.x1)))

    if len(raw_h) < 5:
        return []

    raw_h.sort(key=lambda t: t[0])
    merged: list[tuple[float, float, float]] = []
    cur_ys = [raw_h[0][0]]
    cur_x0 = raw_h[0][1]
    cur_x1 = raw_h[0][2]
    for yc, x0, x1 in raw_h[1:]:
        if abs(yc - cur_ys[-1]) <= 0.85:
            cur_ys.append(yc)
            cur_x0 = min(cur_x0, x0)
            cur_x1 = max(cur_x1, x1)
        else:
            merged.append((sum(cur_ys) / len(cur_ys), cur_x0, cur_x1))
            cur_ys = [yc]
            cur_x0, cur_x1 = x0, x1
    merged.append((sum(cur_ys) / len(cur_ys), cur_x0, cur_x1))

    systems: list[_VectorSystem] = []
    i = 0
    while i <= len(merged) - 5:
        sub = merged[i : i + 5]
        ys = [t[0] for t in sub]
        diffs = [ys[k + 1] - ys[k] for k in range(4)]
        med = sorted(diffs)[1]
        if 3.0 <= med <= 15.0 and max(abs(d - med) for d in diffs) <= 0.75:
            xl = sorted(t[1] for t in sub)[2]
            xr = sorted(t[2] for t in sub)[2]
            ss = (ys[4] - ys[0]) / 4.0
            systems.append(_VectorSystem(page_idx=page_idx, ys=ys, x_left=xl, x_right=xr, ss=ss))
            i += 5
        else:
            i += 1
    return systems


def _extract_glyphs_and_texts(page: fitz.Page) -> tuple[list[_GlyphItem], list[_TextToken], list[tuple[float, float, str]]]:
    """
    Sayfadaki müzik gliflerini (_GlyphItem), sözcük bazlı metin parçalarını (_TextToken)
    ve satır bazlı üst/alt metinleri ayrıştırır.
    """
    glyphs: list[_GlyphItem] = []
    tokens: list[_TextToken] = []
    lines_summary: list[tuple[float, float, str]] = []  # (x0, y0, full_line_str)

    rd = page.get_text("rawdict")
    for b in rd.get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            line_text_parts: list[str] = []
            lx0, ly0 = 9999.0, 9999.0
            for sp in ln.get("spans", []):
                fn = sp["font"].split("+")[-1]
                sz = float(sp["size"])
                chars = sp.get("chars", [])
                if not chars:
                    continue

                is_mus = _is_music_font(fn) or any(0xE040 <= ord(c["c"]) <= 0xE5FF for c in chars)
                if is_mus:
                    for c in chars:
                        ch_s = c["c"]
                        code = ord(ch_s)
                        if code == 32:
                            continue
                        ox, oy = c["origin"]
                        cb = c["bbox"]
                        glyphs.append(
                            _GlyphItem(
                                font=fn.lower(),
                                char=ch_s,
                                code=code,
                                size=sz,
                                x=float(ox),
                                y=float(oy),
                                bbox=(float(cb[0]), float(cb[1]), float(cb[2]), float(cb[3])),
                            )
                        )
                else:
                    # Metin span'ini boşluklara göre ayrı hece/kelime kutularına böl
                    span_str = "".join(c["c"] for c in chars)
                    if span_str.strip():
                        line_text_parts.append(span_str.strip())
                        lx0 = min(lx0, float(sp["bbox"][0]))
                        ly0 = min(ly0, float(sp["bbox"][1]))

                    cur_chars: list[dict] = []
                    for c in chars:
                        if c["c"].isspace():
                            if cur_chars:
                                tok_s = "".join(x["c"] for x in cur_chars)
                                tokens.append(
                                    _TextToken(
                                        text=tok_s,
                                        font=fn,
                                        size=sz,
                                        x0=float(cur_chars[0]["bbox"][0]),
                                        y0=float(min(x["bbox"][1] for x in cur_chars)),
                                        x1=float(cur_chars[-1]["bbox"][2]),
                                        y1=float(max(x["bbox"][3] for x in cur_chars)),
                                    )
                                )
                                cur_chars = []
                        else:
                            cur_chars.append(c)
                    if cur_chars:
                        tok_s = "".join(x["c"] for x in cur_chars)
                        tokens.append(
                            _TextToken(
                                text=tok_s,
                                font=fn,
                                size=sz,
                                x0=float(cur_chars[0]["bbox"][0]),
                                y0=float(min(x["bbox"][1] for x in cur_chars)),
                                x1=float(cur_chars[-1]["bbox"][2]),
                                y1=float(max(x["bbox"][3] for x in cur_chars)),
                            )
                        )

            if line_text_parts:
                lines_summary.append((lx0, ly0, " ".join(line_text_parts)))

    return glyphs, tokens, lines_summary


def _y_to_step_octave(y_val: float, y4: float, ss: float) -> tuple[str, int]:
    """Alt çizgi y4 = E4 referansına göre y koordinatını (step, octave) çiftine çevirir."""
    half_steps = int(round((y4 - y_val) / (0.5 * ss)))
    diatonic_idx = max(18, min(48, 30 + half_steps))  # E4 = 4*7 + 2 = 30
    octave, step_i = divmod(diatonic_idx, 7)
    return STEPS[step_i], octave


def _classify_accidental_glyph(g: _GlyphItem) -> tuple[int, bool] | None:
    """Glif bir AEU / Batı arıza işaretiyse (commas, is_cautionary) döndürür."""
    if "turkey" in g.font:
        if g.char in TURKEY_CHAR_TO_COMMAS:
            return TURKEY_CHAR_TO_COMMAS[g.char], False
        return None
    if g.code in SMUFL_ACCIDENTALS:
        return SMUFL_ACCIDENTALS[g.code], False
    if g.code in PETRUCCI_ACCIDENTALS and ("petrucci" in g.font or "maestro" in g.font or "engraver" in g.font):
        return PETRUCCI_ACCIDENTALS[g.code]
    return None


def _classify_notehead_glyph(g: _GlyphItem) -> str | None:
    """Glif bir nota başıysa 'filled', 'half' veya 'whole' döndürür."""
    if "turkey" in g.font:
        return None
    if g.code == 0xE0A4 or g.code == 207:
        return "filled"
    if g.code == 0xE0A3 or g.code in (250, 729):
        return "half"
    if g.code == 0xE0A2 or (g.code == 119 and ("petrucci" in g.font or "maestro" in g.font)):
        return "whole"
    return None


def _classify_rest_glyph(g: _GlyphItem) -> str | None:
    if "turkey" in g.font:
        return None
    if g.code in SMUFL_RESTS:
        return SMUFL_RESTS[g.code]
    if g.code in PETRUCCI_RESTS:
        return PETRUCCI_RESTS[g.code]
    return None


def _classify_flag_glyph(g: _GlyphItem) -> str | None:
    if "turkey" in g.font:
        return None
    if g.code in SMUFL_FLAGS:
        return SMUFL_FLAGS[g.code]
    if g.code in PETRUCCI_FLAGS:
        return PETRUCCI_FLAGS[g.code]
    return None


def _is_segno_glyph(g: _GlyphItem) -> bool:
    return ("turkey" in g.font and g.char == "v") or g.code == 0xE047 or g.code == 37


def _is_dot_glyph(g: _GlyphItem) -> bool:
    return ("turkey" not in g.font) and (g.code == 46 or g.code == 0xE1E7)


def _parse_header_metadata(
    lines_summary: list[tuple[float, float, str]],
    tokens: list[_TextToken],
    first_sys_y0: float,
    ss: float,
    page_width: float,
    fallback_title: str,
) -> tuple[str, str, str, str, str]:
    """İlk portenin üstündeki metinlerden (title, subtitle, composer, lyricist, tempo_text) ayıklar."""
    hdr_toks = [t for t in tokens if t.y1 < first_sys_y0 - 0.8 * ss]
    if not hdr_toks:
        return fallback_title, "", "", "", ""

    # Satırlara grupla (y0 yakınlığı, yatay boşluk ve punto tutarlılığına göre)
    hdr_toks.sort(key=lambda t: (round(t.yc, 1), t.x0))
    rows: list[list[_TextToken]] = []
    for t in hdr_toks:
        starts_credit = t.text.upper().startswith(("GÜFTE", "GUFTE", "BESTE", "USÛL", "USUL"))
        if (
            rows
            and not starts_credit
            and abs(t.yc - rows[-1][0].yc) <= 4.0
            and abs(t.x0 - rows[-1][-1].x1) <= 18.0
            and abs(t.size - rows[-1][-1].size) < 1.5
        ):
            rows[-1].append(t)
        else:
            rows.append([t])

    title = ""
    subtitle = ""
    composer_lines: list[str] = []
    lyricist = ""
    tempo_text = ""

    # En büyük puntolu ortalanmış satır ana başlıktır (Güfte/Beste/Usûl künyeleri hariç)
    center_rows = [
        r for r in rows
        if 0.24 * page_width <= 0.5 * (r[0].x0 + r[-1].x1) <= 0.76 * page_width
        and not any(
            kw in " ".join(x.text for x in r).upper()
            for kw in ("GÜFTE", "GUFTE", "BESTE", "USÛL", "USUL")
        )
    ]
    if center_rows:
        best_title_row = max(center_rows, key=lambda r: max(t.size for t in r))
        title = " ".join(t.text for t in best_title_row).strip()
        for r in center_rows:
            if r is best_title_row:
                continue
            txt = " ".join(t.text for t in r).strip()
            if txt and r[0].y0 > best_title_row[0].y0:
                subtitle = f"{subtitle} {txt}".strip() if subtitle else txt

    for r in rows:
        if center_rows and (r is best_title_row or " ".join(t.text for t in r).strip() in (title, subtitle)):
            continue
        txt = " ".join(t.text for t in r).strip()
        if not txt or "www." in txt.lower():
            continue
        xc = 0.5 * (r[0].x0 + r[-1].x1)
        up_txt = txt.upper()
        if "GÜFTE" in up_txt or "GUFTE" in up_txt:
            cleaned = re.sub(r"^(GÜFTE|GUFTE)\s*:\s*", "", txt, flags=re.IGNORECASE).strip()
            lyricist = cleaned
        elif "BESTE" in up_txt or xc > page_width * 0.56:
            composer_lines.append(txt)
        elif xc < page_width * 0.42:
            tempo_text = f"{tempo_text}  {txt}".strip() if tempo_text else txt

    return (
        title or fallback_title,
        subtitle,
        "\n".join(composer_lines),
        lyricist,
        tempo_text,
    )


def recognize_vector_pdf(
    pdf_path: str | Path,
    progress_cb: Callable[[int, str], None] | None = None,
) -> ScoreDoc:
    """Vektörel nota PDF dosyasını tam doğrulukla ScoreDoc nesnesine dönüştürür."""
    p = Path(pdf_path)
    if progress_cb:
        progress_cb(10, f"Vektörel PDF nota yapısı okunuyor ({p.name})...")

    fdoc = fitz.open(str(p))
    title = p.stem.strip()
    subtitle = ""
    composer = ""
    lyricist = ""
    header_tempo = ""
    keysig: dict[str, int] = {}
    nominal_ts: list[int] = [4, 4]
    ts_detected = False

    all_measures: list[MeasureSpec] = []
    measure_num = 1

    for pg_idx, page in enumerate(fdoc):
        systems = _extract_systems_on_page(page, pg_idx)
        if not systems:
            continue

        glyphs, text_tokens, lines_summary = _extract_glyphs_and_texts(page)
        drawings = page.get_drawings()

        if pg_idx == 0:
            title, subtitle, composer, lyricist, header_tempo = _parse_header_metadata(
                lines_summary,
                text_tokens,
                systems[0].ys[0],
                systems[0].ss,
                float(page.rect.width),
                p.stem.strip(),
            )

        for sys_i, sys in enumerate(systems):
            if progress_cb:
                pct = 15 + int(80 * (sys_i + 1) / max(1, len(systems)))
                progress_cb(pct, f"Vektör porte satırı çözümleniyor ({sys_i + 1}/{len(systems)})...")

            y0, y1, y2, y3, y4 = sys.ys
            ss = sys.ss
            band_top = y0 - 4.2 * ss
            if sys_i > 0:
                band_top = max(band_top, 0.5 * (systems[sys_i - 1].ys[4] + y0))
            band_bot = y4 + 3.4 * ss
            if sys_i + 1 < len(systems):
                band_bot = min(band_bot, 0.5 * (y4 + systems[sys_i + 1].ys[0]))

            sys_glyphs = [g for g in glyphs if band_top <= g.y <= band_bot]
            main_sizes = [g.size for g in sys_glyphs if _classify_notehead_glyph(g) is not None]
            ref_size = sorted(main_sizes)[len(main_sizes) // 2] if main_sizes else 20.0

            # 1. Bu satırdaki tüm nota başlarını bul (barline/sap ayrımı için önce nota başları)
            raw_heads: list[tuple[_GlyphItem, str, bool]] = []
            for g in sys_glyphs:
                nh_kind = _classify_notehead_glyph(g)
                if nh_kind is not None:
                    is_grace = g.size < 0.78 * ref_size
                    raw_heads.append((g, nh_kind, is_grace))
            raw_heads.sort(key=lambda t: t[0].x)

            # 2. Çizimlerden dikey çizgileri (barline vs sap), kirişleri (beam) ve dolapları (volta) ayır
            sys_beams: list[tuple[float, float, float, float]] = []
            sys_stems: list[tuple[float, float, float]] = []
            raw_bars: list[tuple[float, float]] = []  # (x, line_width)
            volta_spans: list[tuple[float, float, int]] = []

            for d in drawings:
                r = d["rect"]
                if r.y1 < band_top - 2.0 * ss or r.y0 > band_bot + 1.0 * ss:
                    continue

                # Kiriş (Beam): içi siyah dolgulu yatay/eğik paralelkenar
                if d["fill"] is not None and sum(d["fill"][:3]) < 0.6:
                    if r.width >= 0.72 * ss and r.width >= r.height * 0.75 and r.height <= 4.8 * ss:
                        sys_beams.append((float(r.x0), float(r.y0), float(r.x1), float(r.y1)))
                        continue
                    # MuseScore kalın bitiş çizgisi (dolu dikey dikdörtgen)
                    if r.width < 0.72 * ss and abs(r.y0 - y0) <= 1.4 and abs(r.y1 - y4) <= 1.4:
                        raw_bars.append((0.5 * (r.x0 + r.x1), float(r.width)))
                        continue

                # Dikey çizgiler: ölçü çizgisi (barline) veya nota sapı (stem)
                if r.width <= 1.25 and 1.5 * ss <= r.height <= 8.5 * ss:
                    vx = 0.5 * (r.x0 + r.x1)
                    lw = float(d["width"] or 0.4)
                    spans_exact_staff = abs(r.y0 - y0) <= 1.25 and abs(r.y1 - y4) <= 1.25
                    attached_to_head = any(
                        (-0.35 * ss <= vx - hg.x <= 1.65 * ss)
                        and (abs(r.y0 - hg.y) <= 0.85 * ss or abs(r.y1 - hg.y) <= 0.85 * ss)
                        for hg, _, _ in raw_heads
                    )
                    if spans_exact_staff and (lw >= 1.2 or not attached_to_head):
                        raw_bars.append((vx, lw))
                    else:
                        sys_stems.append((vx, float(r.y0), float(r.y1)))
                    continue

                # Dolap (Volta) üst yatay çizgisi
                if r.width >= 30.0 and y0 - 5.5 * ss <= r.y0 <= y0 - 0.4 * ss and r.height <= 4.2 * ss:
                    for tk in text_tokens:
                        if r.x0 - 5.0 <= tk.x0 <= r.x0 + 28.0 and r.y0 - 4.0 <= tk.yc <= y0:
                            m_v = re.match(r"^([12])\.?$", tk.text.strip())
                            if m_v:
                                volta_spans.append((float(r.x0), float(r.x1), int(m_v.group(1))))

            # Yakın ölçü çizgilerini (< 6.5 pt) tek ölçü sınırı kümesinde birleştir
            raw_bars.sort(key=lambda t: t[0])
            bar_clusters: list[list[tuple[float, float]]] = []
            for bx, blw in raw_bars:
                if bar_clusters and abs(bx - bar_clusters[-1][-1][0]) <= 6.5:
                    bar_clusters[-1].append((bx, blw))
                else:
                    bar_clusters.append([(bx, blw)])

            # Röpriz noktalarını (repeat dots) belirle: porte ortasında (y1..y3) alt alta iki nokta
            dot_glyphs = [g for g in sys_glyphs if _is_dot_glyph(g)]
            repeat_dot_xs: set[int] = set()
            repeat_dot_positions: list[float] = []
            for i_d, d1 in enumerate(dot_glyphs):
                for j_d in range(i_d + 1, len(dot_glyphs)):
                    d2 = dot_glyphs[j_d]
                    if abs(d1.x - d2.x) <= 1.5 and 0.6 * ss <= abs(d1.y - d2.y) <= 1.6 * ss:
                        if y1 - 0.3 * ss <= min(d1.y, d2.y) and max(d1.y, d2.y) <= y3 + 0.3 * ss:
                            repeat_dot_xs.add(i_d)
                            repeat_dot_xs.add(j_d)
                            repeat_dot_positions.append(0.5 * (d1.x + d2.x))

            aug_dots = [g for idx_g, g in enumerate(dot_glyphs) if idx_g not in repeat_dot_xs]

            # 3. Sol Anahtarı, Donanım (KeySig) ve Usûl Rakamı (TimeSig) Bölgesi
            clef_glyphs = [g for g in sys_glyphs if g.code in (38, 0xE050) and g.x < sys.x_left + 12.0 * ss]
            clef_x = clef_glyphs[0].x if clef_glyphs else sys.x_left + 1.2 * ss
            first_nh_x = raw_heads[0][0].x if raw_heads else sys.x_right

            # İlk porte satırında usûl (TimeSig) ve donanım (KeySig) tespiti
            ts_max_x = clef_x + 2.0 * ss
            if pg_idx == 0 and sys_i == 0:
                ts_digits_top: list[tuple[float, str]] = []
                ts_digits_bot: list[tuple[float, str]] = []
                common_ts: list[int] | None = None

                for g in sys_glyphs:
                    if not (clef_x + 1.2 * ss <= g.x <= min(first_nh_x - 0.8 * ss, clef_x + 16.0 * ss)):
                        continue
                    if "turkey" in g.font:
                        continue
                    if 48 <= g.code <= 57:
                        dig = chr(g.code)
                        if g.y < y2 + 0.1 * ss:
                            ts_digits_top.append((g.x, dig))
                        else:
                            ts_digits_bot.append((g.x, dig))
                        ts_max_x = max(ts_max_x, g.x)
                    elif 0xE080 <= g.code <= 0xE089:
                        dig = str(g.code - 0xE080)
                        if g.y < y2 + 0.1 * ss:
                            ts_digits_top.append((g.x, dig))
                        else:
                            ts_digits_bot.append((g.x, dig))
                        ts_max_x = max(ts_max_x, g.x)
                    elif g.code in (99, 0xE08A):
                        common_ts = [4, 4]
                        ts_max_x = max(ts_max_x, g.x)
                    elif g.code in (67, 0xE08B):
                        common_ts = [2, 2]
                        ts_max_x = max(ts_max_x, g.x)

                if ts_digits_top and ts_digits_bot:
                    ts_digits_top.sort()
                    ts_digits_bot.sort()
                    nominal_ts = [
                        int("".join(d for _, d in ts_digits_top)),
                        int("".join(d for _, d in ts_digits_bot)),
                    ]
                    ts_detected = True
                elif common_ts:
                    nominal_ts = common_ts
                    ts_detected = True

                # Anahtar ile Usûl rakamı (veya ilk nota) arasındaki arızalar = Makam Donanımı!
                ks_right_bound = (ts_max_x - 0.5 * ss) if ts_detected else (first_nh_x - 2.3 * ss)
                for g in sys_glyphs:
                    if clef_x + 0.8 * ss <= g.x <= ks_right_bound:
                        acc = _classify_accidental_glyph(g)
                        if acc is not None:
                            st_k, _ = _y_to_step_octave(g.y, y4, ss)
                            keysig[st_k] = acc[0]

            # Her satırda anahtar+donanım bitiş sınırı (ölçü içi notaların başladığı x)
            header_end_x = clef_x + (1.8 + 1.45 * len(keysig)) * ss
            if pg_idx == 0 and sys_i == 0 and ts_detected:
                header_end_x = max(header_end_x, ts_max_x + 1.2 * ss)

            # 4. Satırı Ölçü Dilimlerine (Measure Spans) Ayır
            # Sol kenar başlangıç çizgisini çıkar, iç ve sağ bitiş barline kümelerini kullan
            valid_clusters = [cl for cl in bar_clusters if cl[-1][0] > sys.x_left + 3.0 * ss]
            m_bounds: list[tuple[float, float, list[tuple[float, float]] | None, list[tuple[float, float]] | None]] = []

            cur_x0 = header_end_x
            left_cl: list[tuple[float, float]] | None = None
            # Eğer donanımdan hemen sonra açık bir röpriz başlangıç barline kümesi varsa yakala
            if valid_clusters and valid_clusters[0][0][0] < first_nh_x:
                left_cl = valid_clusters[0]
                cur_x0 = max(cur_x0, left_cl[-1][0] + 0.4 * ss)
                valid_clusters = valid_clusters[1:]

            for cl in valid_clusters:
                cl_x0 = cl[0][0]
                if cl_x0 - cur_x0 >= 3.0 * ss:
                    m_bounds.append((cur_x0, cl_x0, left_cl, cl))
                cur_x0 = cl[-1][0] + 0.3 * ss
                left_cl = cl

            if sys.x_right - cur_x0 >= 4.0 * ss:
                m_bounds.append((cur_x0, sys.x_right + 1.0 * ss, left_cl, None))

            # 5. Güfte (Lyrics) Satırlarını Bu Portenin Notalarına Eşle
            next_y0 = systems[sys_i + 1].ys[0] if sys_i + 1 < len(systems) else (y4 + 6.2 * ss)
            lyric_bot_lim = min(next_y0 - 2.2 * ss, y4 + 5.8 * ss)
            raw_lyr_toks = [
                tk for tk in text_tokens
                if y4 + 0.45 * ss <= tk.y0 <= lyric_bot_lim
                and sys.x_left <= tk.xc <= sys.x_right + 2.0 * ss
                and "necati çelik" not in tk.text.lower()
            ]
            # İmza metnini ("Necati Çelik" sağ altta italik el yazısı) çıkar
            raw_lyr_toks = [
                tk for tk in raw_lyr_toks
                if not ("brushscript" in tk.font.lower() or "corsiva" in tk.font.lower())
            ]

            # Kıta (verse) satırlarına ayır (y0 yakınlığına göre)
            raw_lyr_toks.sort(key=lambda t: (t.y0, t.x0))
            verse_rows: list[list[_TextToken]] = []
            for tk in raw_lyr_toks:
                placed = False
                for vr in verse_rows:
                    if abs(tk.yc - vr[0].yc) <= 4.8:
                        vr.append(tk)
                        placed = True
                        break
                if not placed:
                    verse_rows.append([tk])

            main_nh_list = [hg for hg, _, is_gr in raw_heads if not is_gr and hg.x >= header_end_x - 0.5 * ss]
            note_lyrics_map: dict[int, list[str]] = {}
            for v_idx, vr in enumerate(verse_rows[:3]):
                vr.sort(key=lambda t: t.x0)
                for tk in vr:
                    if not main_nh_list:
                        continue
                    best_i = min(range(len(main_nh_list)), key=lambda idx_n: abs(main_nh_list[idx_n].x - tk.x0 - 2.0))
                    if abs(main_nh_list[best_i].x - tk.xc) <= 3.6 * ss:
                        arr = note_lyrics_map.setdefault(id(main_nh_list[best_i]), [])
                        while len(arr) <= v_idx:
                            arr.append("")
                        arr[v_idx] = f"{arr[v_idx]} {tk.text}".strip() if arr[v_idx] else tk.text

            # 6. Her Ölçüdeki Olayları (Nota, Arıza, Sap/Kiriş, Nokta, Es, Röpriz, Dolap, Segno) Derle
            acc_glyphs_mid = [
                g for g in sys_glyphs
                if g.x >= header_end_x - 1.8 * ss and _classify_accidental_glyph(g) is not None
            ]
            flag_glyphs = [g for g in sys_glyphs if _classify_flag_glyph(g) is not None]
            rest_glyphs = [g for g in sys_glyphs if g.x >= header_end_x and _classify_rest_glyph(g) is not None]
            segno_glyphs = [g for g in sys_glyphs if _is_segno_glyph(g)]

            used_acc_ids: set[int] = set()
            used_dot_ids: set[int] = set()

            for mb_i, (mx0, mx1, l_cl, r_cl) in enumerate(m_bounds):
                start_rep = False
                end_rep = False
                bline_type: str | None = None

                if l_cl is not None:
                    lx_max = l_cl[-1][0]
                    if any( lx_max <= rdx <= lx_max + 3.2 * ss for rdx in repeat_dot_positions ):
                        start_rep = True

                if r_cl is not None:
                    rx_min = r_cl[0][0]
                    max_lw = max(w for _, w in r_cl)
                    if any( rx_min - 3.2 * ss <= rdx <= rx_min for rdx in repeat_dot_positions ):
                        end_rep = True
                    elif max_lw >= 1.5 and pg_idx == len(fdoc) - 1 and sys_i == len(systems) - 1 and mb_i == len(m_bounds) - 1:
                        bline_type = "end"
                    elif len(r_cl) >= 2 and not start_rep:
                        # Sonraki ölçüde sağında röpriz noktası yoksa çift çizgidir
                        rx_max = r_cl[-1][0]
                        if not any(rx_max <= rdx <= rx_max + 3.2 * ss for rdx in repeat_dot_positions):
                            bline_type = "double"

                volta_num: int | None = None
                for vx0, vx1, v_n in volta_spans:
                    if abs(vx0 - mx0) <= 4.0 * ss or (vx0 <= 0.5 * (mx0 + mx1) <= vx1):
                        volta_num = v_n
                        break

                seg_start = any(
                    (mb_i == 0 and sg.x < mx0 + 4.0 * ss) or (mx0 - 2.0 * ss <= sg.x <= mx0 + 3.0 * ss)
                    for sg in segno_glyphs
                )
                seg_end = any(mx1 - 4.5 * ss <= sg.x <= mx1 + 2.5 * ss for sg in segno_glyphs) and not seg_start

                m_heads = [(hg, nk, igr) for hg, nk, igr in raw_heads if mx0 - 0.6 * ss <= hg.x < mx1 - 0.2 * ss]
                m_rests = [rg for rg in rest_glyphs if mx0 <= rg.x < mx1 - 0.2 * ss]

                timed_evs: list[tuple[float, NoteEvent]] = []
                pending_grace: tuple[str, int | None] | None = None

                for hg, nh_kind, is_grace in m_heads:
                    st_name, oct_num = _y_to_step_octave(hg.y, y4, ss)

                    # Nota solundaki açık arıza
                    best_acc: tuple[int, bool] | None = None
                    best_acc_dist = 999.0
                    best_acc_id: int | None = None
                    for ag in acc_glyphs_mid:
                        if id(ag) in used_acc_ids:
                            continue
                        dx = hg.x - ag.x
                        dy = abs(hg.y - ag.y)
                        if 0.2 * ss <= dx <= 3.1 * ss and dy <= 0.85 * ss and dx < best_acc_dist:
                            best_acc = _classify_accidental_glyph(ag)
                            best_acc_dist = dx
                            best_acc_id = id(ag)
                    if best_acc_id is not None:
                        used_acc_ids.add(best_acc_id)

                    if is_grace:
                        pending_grace = (f"{st_name}{oct_num}", best_acc[0] if best_acc else None)
                        continue

                    # Süre ve kiriş (beam) tespiti
                    beam_mode = "no"
                    stem_dir: str | None = None
                    if nh_kind == "whole":
                        base_dur = "whole"
                    elif nh_kind == "half":
                        base_dur = "half"
                    else:
                        # Bu nota başına bağlı sapı bul
                        cand_stems = [
                            (sx, sy0, sy1)
                            for sx, sy0, sy1 in sys_stems
                            if (-0.35 * ss <= sx - hg.x <= 1.75 * ss)
                            and (abs(sy0 - hg.y) <= 0.95 * ss or abs(sy1 - hg.y) <= 0.95 * ss)
                        ]
                        if cand_stems:
                            sx, sy0, sy1 = min(
                                cand_stems,
                                key=lambda t: min(abs(t[1] - hg.y), abs(t[2] - hg.y)) + 0.3 * abs(t[0] - hg.x),
                            )
                            stem_dir = "down" if abs(sy0 - hg.y) < abs(sy1 - hg.y) else "up"
                            # Bu sapı kesen tüm siyah kirişleri say (eğik kirişler dahil aralık kesişimi)
                            s_top, s_bot = min(sy0, sy1), max(sy0, sy1)
                            crossing_beams = [
                                (bx0, by0, bx1, by1)
                                for bx0, by0, bx1, by1 in sys_beams
                                if (bx0 - 1.5 <= sx <= bx1 + 1.5)
                                and (by0 <= s_bot + 3.0 and by1 >= s_top - 3.0)
                            ]
                            n_beams = len(crossing_beams)
                            if n_beams >= 3:
                                base_dur = "32nd"
                            elif n_beams == 2:
                                base_dur = "16th"
                            elif n_beams == 1:
                                base_dur = "eighth"
                            else:
                                # Tekil çengel (flag) glifi var mı?
                                fl_match = [
                                    _classify_flag_glyph(fg)
                                    for fg in flag_glyphs
                                    if abs(fg.x - sx) <= 1.4 * ss or abs(fg.x - hg.x) <= 1.5 * ss
                                ]
                                base_dur = fl_match[0] if fl_match and fl_match[0] else "quarter"

                            if crossing_beams:
                                prim = max(crossing_beams, key=lambda b: b[2] - b[0])
                                if abs(sx - prim[0]) <= 1.8:
                                    beam_mode = "begin"
                                elif abs(sx - prim[2]) <= 1.8:
                                    beam_mode = "end"
                                else:
                                    beam_mode = "mid"
                        else:
                            base_dur = "quarter"

                    # Uzatma noktası (augmentation dot) kontrolü
                    has_dot = False
                    for dg in aug_dots:
                        if id(dg) in used_dot_ids:
                            continue
                        if 0.35 * ss <= (dg.x - hg.x) <= 2.75 * ss and abs(dg.y - hg.y) <= 1.05 * ss:
                            has_dot = True
                            used_dot_ids.add(id(dg))
                            break

                    dur_str = base_dur + ("." if has_dot and base_dur != "32nd" else "")
                    lyr_verses = note_lyrics_map.get(id(hg), [])

                    ev = NoteEvent(
                        kind="note",
                        step=st_name,
                        octave=oct_num,
                        duration=dur_str,
                        beam=beam_mode,
                        accidental_commas=best_acc[0] if best_acc is not None else None,
                        cautionary=best_acc[1] if best_acc is not None else False,
                        grace_before=pending_grace[0] if pending_grace else None,
                        grace_commas=pending_grace[1] if pending_grace else None,
                        lyric=lyr_verses[0] if lyr_verses else None,
                        lyrics=lyr_verses,
                        stem_dir=stem_dir,
                    )
                    pending_grace = None
                    timed_evs.append((hg.x, ev))

                for rg in m_rests:
                    r_dur = _classify_rest_glyph(rg) or "quarter"
                    for dg in aug_dots:
                        if id(dg) not in used_dot_ids and 0.3 * ss <= (dg.x - rg.x) <= 2.4 * ss and abs(dg.y - rg.y) <= 1.2 * ss:
                            r_dur += "."
                            used_dot_ids.add(id(dg))
                            break
                    timed_evs.append((rg.x, NoteEvent(kind="rest", duration=r_dur)))

                timed_evs.sort(key=lambda t: t[0])
                # Aynı yatay vuruş konumundaki (dx <= 3.2 pt) çift ses / divisi (ossia) tekrarlarını sadeleştir
                dedup_evs: list[tuple[float, NoteEvent]] = []
                for tx, tev in timed_evs:
                    if dedup_evs and abs(tx - dedup_evs[-1][0]) <= 3.2 and tev.kind == dedup_evs[-1][1].kind and tev.duration == dedup_evs[-1][1].duration:
                        prev_ev = dedup_evs[-1][1]
                        # Güftesi olan ana melodi notasını koru
                        if tev.kind == "note" and (tev.lyrics or tev.lyric) and not (prev_ev.lyrics or prev_ev.lyric):
                            dedup_evs[-1] = (tx, tev)
                        continue
                    dedup_evs.append((tx, tev))

                evs = [t[1] for t in dedup_evs]
                if not evs:
                    continue

                tot_frac = sum((duration_to_fraction(e.duration) for e in evs), Fraction(0, 1))
                nom_frac = Fraction(nominal_ts[0], nominal_ts[1])
                if tot_frac == nom_frac:
                    act_len = f"{nominal_ts[0]}/{nominal_ts[1]}"
                else:
                    den = tot_frac.denominator
                    num = tot_frac.numerator
                    if den < nominal_ts[1] and nominal_ts[1] % den == 0:
                        mult = nominal_ts[1] // den
                        num *= mult
                        den = nominal_ts[1]
                    elif den < 4:
                        mult = 4 // den
                        num *= mult
                        den = 4
                    act_len = f"{num}/{den}"

                mspec = MeasureSpec(
                    number=measure_num,
                    nominal_ts=nominal_ts if measure_num == 1 else None,
                    actual_len=act_len,
                    tempo_text=header_tempo if (measure_num == 1 and header_tempo) else None,
                    segno_start=seg_start,
                    segno_end=seg_end,
                    start_repeat=start_rep,
                    end_repeat=end_rep,
                    volta=volta_num,
                    barline=bline_type,
                    line_break=(mb_i == len(m_bounds) - 1 and not (pg_idx == len(fdoc) - 1 and sys_i == len(systems) - 1)),
                    page_break=(mb_i == len(m_bounds) - 1 and sys_i == len(systems) - 1 and pg_idx < len(fdoc) - 1),
                    events=evs,
                )
                auto_beam_measure_events(mspec)
                all_measures.append(mspec)
                measure_num += 1

    fdoc.close()

    if all_measures and not all_measures[-1].end_repeat and not all_measures[-1].barline:
        all_measures[-1].barline = "end"

    doc = ScoreDoc(
        title=title,
        subtitle=subtitle,
        composer=composer,
        lyricist=lyricist,
        keysig={k: v for k, v in keysig.items() if v != 0},
        measures=all_measures,
    )
    if progress_cb:
        progress_cb(98, f"Vektörel nota eksiksiz okundu ({len(all_measures)} ölçü).")
    return doc
