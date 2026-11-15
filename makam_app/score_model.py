"""
MakamNotaApp Ara Nota Veri Modeli (ScoreDoc JSON Şeması), Süre Doğrulayıcı ve ScoreDoc Göçürme Motoru.
"""

import copy
import json
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path

from .aeu_theory import (
    STEPS,
    transpose_keysig_map,
    transpose_pitch_and_commas,
)

DUR_TO_FRACTION: dict[str, Fraction] = {
    "whole": Fraction(1, 1),
    "half": Fraction(1, 2),
    "quarter": Fraction(1, 4),
    "eighth": Fraction(1, 8),
    "16th": Fraction(1, 16),
    "32nd": Fraction(1, 32),
}


def duration_to_fraction(dur_str: str) -> Fraction:
    s = dur_str.strip()
    dots = 0
    while s.endswith("."):
        dots += 1
        s = s[:-1]
    base = DUR_TO_FRACTION[s]
    total = base
    add = base
    for _ in range(dots):
        add = add / 2
        total += add
    return total


def parse_meter_fraction(meter_str: str) -> Fraction:
    n_s, d_s = meter_str.strip().split("/")
    return Fraction(int(n_s), int(d_s))


@dataclass
class NoteEvent:
    kind: str = "note"                   # "note" veya "rest"
    step: str = "C"                      # "C".."B"
    octave: int = 5                      # 3..6
    duration: str = "quarter"            # "whole", "half", "quarter", "quarter.", "eighth", "eighth.", "16th", "32nd"
    beam: str = "no"                     # "no", "begin", "mid", "end"
    accidental_commas: int | None = None # Açık basılan arıza koma değeri (örn. -4, 0, +4, -5); None = donanım/ölçü içi devam
    cautionary: bool = False             # Parantez içi hatırlatma arızası: (♮), (♭) vb.
    tie_start: bool = False
    tie_end: bool = False
    slur_start: bool = False
    slur_end: bool = False
    grace_before: str | None = None      # Örn. "D5" veya "B5" (acciaccatura çarpma notası)
    grace_commas: int | None = None      # Çarpma notasında özel arıza varsa
    lyric: str | None = None             # Nota altı 1. kıta güfte hecesi (örn. "Bir", "ih-", "ti-", "mal")
    lyrics: list[str] = field(default_factory=list)  # Çok kıtalı güfte satırları (örn. ["Bül-", "Câ-"])
    stem_dir: str | None = None          # "up", "down" veya None


@dataclass
class MeasureSpec:
    number: int = 1
    nominal_ts: list[int] | None = None        # Yalnızca ölçü başında görünür usûl rakamı basılacaksa, örn. [10, 8] veya [28, 4]
    actual_len: str = "10/8"                   # Ölçünün gerçek süresi, örn. "10/8", "6/4", "4/4"
    tempo_text: str | None = None              # Örn. "Aksak Semâî   ♪ = 112"
    tempo_qpm: float | None = None             # Dakikadaki dörtlük sayısı (örn. ♪=112 -> 56.0, ♩=72 -> 72.0)
    section_label: str | None = None           # Sol kenar başlığı: "1. Hâne", "Teslim" vb.
    segno_start: bool = False
    segno_end: bool = False
    start_repeat: bool = False
    end_repeat: bool = False
    volta: int | None = None                   # 1 (1. dolap) veya 2 (2. dolap)
    barline: str | None = None                 # None, "double", "end"
    end_text: str | None = None                # Örn. "— Son —"
    footnote_text: str | None = None           # Sayfa altı açıklama notu
    line_break: bool = False
    page_break: bool = False
    vbox_page_label: str | None = None         # 2. sayfa vb. başında yeni VBox başlığı ("2")
    beat3_quarter_rest: bool = False           # Klasik ney notalarındaki 3. vuruş alt çeyrek es
    events: list[NoteEvent] = field(default_factory=list)


@dataclass
class ScoreDoc:
    title: str = "Türk Müziği Eseri"
    subtitle: str = ""
    composer: str = ""
    lyricist: str = ""
    footer_right: str = ""
    instrument_name: str = "Ney"
    midi_program: int = 73
    spatium: float = 1.55
    keysig: dict[str, int] = field(default_factory=dict)  # Örn. {"B": -1, "D": -4}
    measures: list[MeasureSpec] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(indent=2), encoding="utf-8")

    @classmethod
    def from_dict(cls, data: dict) -> "ScoreDoc":
        measures = []
        for m_d in data.get("measures", []):
            evs = [NoteEvent(**ev) for ev in m_d.get("events", [])]
            m_copy = {k: v for k, v in m_d.items() if k != "events"}
            measures.append(MeasureSpec(**m_copy, events=evs))
        top = {k: v for k, v in data.items() if k != "measures"}
        return cls(**top, measures=measures)

    @classmethod
    def load_json(cls, path: str | Path) -> "ScoreDoc":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw)


@dataclass
class MeasureValidationIssue:
    measure_number: int
    expected: Fraction
    actual: Fraction
    message: str


def validate_score_durations(doc: ScoreDoc) -> list[MeasureValidationIssue]:
    """Tüm ölçülerin nota/es süre toplamlarını actual_len ile karşılaştırır."""
    issues: list[MeasureValidationIssue] = []
    for m in doc.measures:
        try:
            exp = parse_meter_fraction(m.actual_len)
        except Exception as exc:
            issues.append(MeasureValidationIssue(m.number, Fraction(0), Fraction(0), f"Geçersiz ölçü birimi '{m.actual_len}': {exc}"))
            continue

        act = Fraction(0, 1)
        try:
            for ev in m.events:
                act += duration_to_fraction(ev.duration)
        except Exception as exc:
            issues.append(MeasureValidationIssue(m.number, exp, act, f"Süre okuma hatası: {exc}"))
            continue

        if act != exp:
            issues.append(
                MeasureValidationIssue(
                    measure_number=m.number,
                    expected=exp,
                    actual=act,
                    message=f"Ölçü {m.number}: toplam süre {act.numerator}/{act.denominator}, beklenen {exp.numerator}/{exp.denominator} ({m.actual_len})",
                )
            )
    return issues


def auto_beam_measure_events(m: MeasureSpec) -> None:
    """Eğer tüm beam alanları 'no' bırakılmışsa basit grup kirişlemesi uygular; tanımlıysa dokunmaz."""
    if any(ev.beam in ("begin", "mid", "end") for ev in m.events):
        return
    # Sekizlik ve onaltılık ardışık notaları vuruş içinde ikişerli-dörderli bağla
    run: list[NoteEvent] = []
    run_dur = Fraction(0, 1)
    max_group = Fraction(1, 4) if not m.actual_len.endswith("/8") else Fraction(3, 8)

    def flush_run():
        if len(run) >= 2:
            run[0].beam = "begin"
            for mid_ev in run[1:-1]:
                mid_ev.beam = "mid"
            run[-1].beam = "end"
        run.clear()

    for ev in m.events:
        d = duration_to_fraction(ev.duration)
        if ev.kind == "note" and d < Fraction(1, 4):
            if run_dur + d > max_group:
                flush_run()
                run_dur = Fraction(0, 1)
            run.append(ev)
            run_dur += d
        else:
            flush_run()
            run_dur = Fraction(0, 1)
    flush_run()


def transpose_score_doc(
    doc: ScoreDoc,
    diatonic_steps: int,
    comma_shift: int,
    custom_keysig_override: dict[str, int] | None = None,
    subtitle_note: str | None = None,
) -> ScoreDoc:
    """
    ScoreDoc nesnesini 53-koma AEU kurallarına göre (diatonic_steps, comma_shift) kadar göçürür.
    Ölçü içindeki gerçek koma değerlerini takip ederek yeni donanıma göre hangi notaların
    açık arıza (♮, #, bakiye vb.) basması gerektiğini tam otomatik hesaplar.
    """
    new_doc = copy.deepcopy(doc)
    orig_ks = {s: doc.keysig.get(s, 0) for s in STEPS}

    if diatonic_steps == 0 and comma_shift == 0 and custom_keysig_override is None:
        return new_doc

    computed_ks = transpose_keysig_map(orig_ks, diatonic_steps, comma_shift)
    target_ks = {s: custom_keysig_override.get(s, 0) for s in STEPS} if custom_keysig_override is not None else computed_ks
    new_doc.keysig = {s: v for s, v in target_ks.items() if v != 0}

    if subtitle_note:
        new_doc.subtitle = f"{doc.subtitle} ({subtitle_note})".strip() if doc.subtitle else subtitle_note

    for m in new_doc.measures:
        # Kaynak ölçüdeki aktif koma durumu (step, octave) -> delta_commas
        orig_active: dict[tuple[str, int], int] = {}
        # Yeni ölçüdeki aktif koma durumu (new_step, new_octave) -> new_delta_commas
        new_active: dict[tuple[str, int], int] = {}

        for ev in m.events:
            if ev.kind != "note":
                continue

            # Önce varsa çarpma notasını (grace_before) göçür
            if ev.grace_before:
                g_step = ev.grace_before[0].upper()
                g_oct = int(ev.grace_before[1:])
                g_old_delta = ev.grace_commas if ev.grace_commas is not None else orig_active.get((g_step, g_oct), orig_ks.get(g_step, 0))
                ng_step, ng_oct, ng_delta = transpose_pitch_and_commas(g_step, g_oct, g_old_delta, diatonic_steps, comma_shift)
                ev.grace_before = f"{ng_step}{ng_oct}"
                g_expected = new_active.get((ng_step, ng_oct), target_ks.get(ng_step, 0))
                ev.grace_commas = ng_delta if ng_delta != g_expected else None

            s_old = ev.step.upper()
            o_old = ev.octave
            if ev.accidental_commas is not None:
                old_delta = ev.accidental_commas
                orig_active[(s_old, o_old)] = old_delta
            else:
                old_delta = orig_active.get((s_old, o_old), orig_ks.get(s_old, 0))

            s_new, o_new, new_delta = transpose_pitch_and_commas(
                s_old, o_old, old_delta, diatonic_steps, comma_shift
            )
            ev.step = s_new
            ev.octave = o_new

            expected_in_new_bar = new_active.get((s_new, o_new), target_ks.get(s_new, 0))
            if ev.cautionary:
                # Kaynakta parantezli hatırlatma arızası varsa yeni değerini de parantezli bas
                ev.accidental_commas = new_delta
                new_active[(s_new, o_new)] = new_delta
            elif new_delta != expected_in_new_bar:
                ev.accidental_commas = new_delta
                new_active[(s_new, o_new)] = new_delta
            else:
                ev.accidental_commas = None

    return new_doc
