"""
53-Koma Arel-Ezgi-Uzdilek (AEU) Ses Sistemi, Donanım ve Göçürme (Transpoze) Cebiri.
"""

from dataclasses import dataclass

STEPS = ["C", "D", "E", "F", "G", "A", "B"]
STEP_TR_NAMES = {
    "C": "Do",
    "D": "Re",
    "E": "Mi",
    "F": "Fa",
    "G": "Sol",
    "A": "La",
    "B": "Si",
}

# C = 0 referansına göre doğal seslerin 53-koma konumları
AEU_STEP_COMMAS = {
    "C": 0,
    "D": 9,
    "E": 18,
    "F": 22,
    "G": 31,
    "A": 40,
    "B": 49,
}

# 12-TET yarım ses konumları ve MuseScore doğal TPC değerleri
STEP_SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
STEP_TPC_NATURAL = {"F": 13, "C": 14, "G": 15, "D": 16, "A": 17, "E": 18, "B": 19}

# Sol anahtarında CustDef degree indeksleri (C=0 .. B=6)
STEP_TO_CUSTDEF_DEGREE = {"C": 0, "D": 1, "E": 2, "F": 3, "G": 4, "A": 5, "B": 6}
CUSTDEF_DEGREE_TO_STEP = {v: k for k, v in STEP_TO_CUSTDEF_DEGREE.items()}

CENTS_PER_COMMA = 1200.0 / 53.0


@dataclass(frozen=True)
class AEUAccidentalInfo:
    commas: int
    code: str
    tr_name: str
    keysig_sym: str | None          # <CustDef><sym> için SMuFL ismi
    note_subtype: str | None        # <Note><Accidental><subtype> ismi
    note_symbol_Attach: str | None  # MuseScore subtype tablosunda olmayan semboller için <Note><Symbol><name>
    tpc_alter: int                  # MuseScore TPC alter (-1, 0, +1)


# Standart AEU değiştirici işaret tablosu
AEU_ACCIDENTALS: dict[int, AEUAccidentalInfo] = {
    -8: AEUAccidentalInfo(
        commas=-8,
        code="buyuk_mucenneb_b",
        tr_name="Büyük mücenneb bemolü (-8 koma)",
        keysig_sym="accidentalBuyukMucennebFlat",
        note_subtype="accidentalBuyukMucennebFlat",
        note_symbol_Attach=None,
        tpc_alter=0,
    ),
    -5: AEUAccidentalInfo(
        commas=-5,
        code="kucuk_mucenneb_b",
        tr_name="Küçük mücenneb bemolü (-5 koma, ♭)",
        keysig_sym="accidentalFlat",
        note_subtype="accidentalFlat",
        note_symbol_Attach=None,
        tpc_alter=-1,
    ),
    -4: AEUAccidentalInfo(
        commas=-4,
        code="bakiye_b",
        tr_name="Bakiye bemolü (-4 koma)",
        keysig_sym="accidentalBakiyeFlat",
        note_subtype="accidentalBakiyeFlat",
        note_symbol_Attach=None,
        tpc_alter=0,
    ),
    -1: AEUAccidentalInfo(
        commas=-1,
        code="koma_b",
        tr_name="Koma bemolü (-1 koma)",
        keysig_sym="accidentalKomaFlat",
        note_subtype=None,
        note_symbol_Attach="accidentalKomaFlat",
        tpc_alter=0,
    ),
    0: AEUAccidentalInfo(
        commas=0,
        code="natural",
        tr_name="Natürel (0 koma, ♮)",
        keysig_sym=None,
        note_subtype="accidentalNatural",
        note_symbol_Attach=None,
        tpc_alter=0,
    ),
    1: AEUAccidentalInfo(
        commas=1,
        code="koma_s",
        tr_name="Koma diyezi (+1 koma)",
        keysig_sym="accidentalKomaSharp",
        note_subtype=None,
        note_symbol_Attach="accidentalKomaSharp",
        tpc_alter=0,
    ),
    4: AEUAccidentalInfo(
        commas=4,
        code="bakiye_s",
        tr_name="Bakiye diyezi (+4 koma, #)",
        keysig_sym="accidentalBakiyeSharp",
        note_subtype="accidentalSharp",
        note_symbol_Attach=None,
        tpc_alter=1,
    ),
    5: AEUAccidentalInfo(
        commas=5,
        code="kucuk_mucenneb_s",
        tr_name="Küçük mücenneb diyezi (+5 koma)",
        keysig_sym="accidentalKucukMucennebSharp",
        note_subtype="accidentalKucukMucennebSharp",
        note_symbol_Attach=None,
        tpc_alter=0,
    ),
    8: AEUAccidentalInfo(
        commas=8,
        code="buyuk_mucenneb_s",
        tr_name="Büyük mücenneb diyezi (+8 koma)",
        keysig_sym="accidentalBuyukMucennebSharp",
        note_subtype="accidentalBuyukMucennebSharp",
        note_symbol_Attach=None,
        tpc_alter=0,
    ),
}

CODE_TO_AEU: dict[str, AEUAccidentalInfo] = {v.code: v for v in AEU_ACCIDENTALS.values()}

# SMuFL / MuseScore XML okurken koma değerine geri dönüşüm tablosu
XML_ACC_TO_COMMAS: dict[str, int] = {
    "accidentalBuyukMucennebFlat": -8,
    "accidentalDoubleFlat": -9,
    "accidentalFlat": -5,
    "accidentalKucukMucennebFlat": -5,
    "accidentalBakiyeFlat": -4,
    "accidentalKomaFlat": -1,
    "accidentalNarrowReversedFlat": -1,
    "accidentalQuarterToneFlatStein": -1,
    "accidentalNatural": 0,
    "accidentalKomaSharp": 1,
    "accidentalBakiyeSharp": 4,
    "accidentalSharp": 4,
    "accidentalKucukMucennebSharp": 5,
    "accidentalBuyukMucennebSharp": 8,
    "accidentalDoubleSharp": 8,
}


def nearest_aeu_accidental(commas: int) -> AEUAccidentalInfo:
    """Herhangi bir koma sapmasını en yakın standart AEU işaretine eşler."""
    if commas in AEU_ACCIDENTALS:
        return AEU_ACCIDENTALS[commas]
    best_k = min(AEU_ACCIDENTALS.keys(), key=lambda k: abs(k - commas))
    return AEU_ACCIDENTALS[best_k]


def mod53_signed(val: int) -> int:
    """Koma farkını [-26, +26] aralığına taşır."""
    m = val % 53
    if m > 26:
        m -= 53
    return m


# ---------------------------------------------------------------------------
# 1. Seçim Modu: Ses / Koma Aralığı Ön-Ayarları
# ---------------------------------------------------------------------------
INTERVAL_PRESETS = [
    ("Bire Bir Replika (Transpozesiz — 0 Koma)", 0, 0),
    ("+1 Tam Ses Yukarı (+9 Koma / Tanînî)", 1, 9),
    ("-1 Tam Ses Aşağı (-9 Koma / Tanînî)", -1, -9),
    ("+Yarım Ses Yukarı (+1 Adım, +4 Koma / Bakiye)", 1, 4),
    ("-Yarım Ses Aşağı (-1 Adım, -4 Koma / Bakiye)", -1, -4),
    ("+2 Tam Ses Yukarı (+18 Koma / Büyük Üçlü)", 2, 18),
    ("-2 Tam Ses Aşağı (-18 Koma / Büyük Üçlü)", -2, -18),
    ("+Tam Dörtlü Yukarı (+3 Adım, +22 Koma)", 3, 22),
    ("-Tam Dörtlü Aşağı (-3 Adım, -22 Koma)", -3, -22),
    ("+Tam Beşli Yukarı (+4 Adım, +31 Koma)", 4, 31),
    ("-Tam Beşli Aşağı (-4 Adım, -31 Koma)", -4, -31),
    ("+1 Oktav Yukarı (+7 Adım, +53 Koma)", 7, 53),
    ("-1 Oktav Aşağı (-7 Adım, -53 Koma)", -7, -53),
]

# ---------------------------------------------------------------------------
# 2. Seçim Modu: Türk Müziği Karar Perdeleri (Diatonik İndeks: C4 = 0, Koma: C4 = 0)
# ---------------------------------------------------------------------------
# diatonic_index = (octave - 4) * 7 + STEPS.index(step)
# abs_commas     = (octave - 4) * 53 + AEU_STEP_COMMAS[step] + delta_commas
PERDE_LIST = [
    ("Kaba Çargâh (Do4)", 0, 0),
    ("Yegâh (Re4)", 1, 9),
    ("Hüseynîaşîrân (Mi4)", 2, 18),
    ("Acemaşîrân (Fa4)", 3, 22),
    ("Irak (Fa4 bakiye diyezi)", 3, 26),
    ("Rast (Sol4)", 4, 31),
    ("Dügâh (La4)", 5, 40),
    ("Kürdî (Si4 küçük mücenneb bemolü)", 6, 44),
    ("Segâh (Si4 koma bemolü)", 6, 48),
    ("Bûselik (Si4 natürel)", 6, 49),
    ("Çargâh (Do5)", 7, 53),
    ("Nevâ (Re5)", 8, 62),
]

# ---------------------------------------------------------------------------
# 3. Seçim Modu: Ney Ahenkleri (Bolâhenk Nısfiye = 0 referansına göre sazların akort konumu)
# ---------------------------------------------------------------------------
# Her ney için (Etiket, Diyatonik Adım, 53-Koma) değerleri Bolâhenk Nısfiye referansına göredir:
#   Bolâhenk Nısfiye'de Nevâ (Re5) = 440 Hz (Batı La4), Dügâh (La4) = Batı Mi4 (Hüseynî).
#   Mansur Ney'de       Muhayyer (La5) = 440 Hz,        Dügâh (La4) = Batı La3 (-4 adım, -31 koma).
# Elimdeki ney (d_hand, c_hand) ile hedef icra ahenginde (d_target, c_target) çalmak için
# notanın kaydırılması gereken aralık: (d_target - d_hand, c_target - c_hand).
NEY_AHENKLERI = [
    ("Bolâhenk Nısfiye (Dügâh = Mi4 / Hüseynî — En Tiz)", 0, 0),
    ("Bolâhenk–Süpürde Mâbeyni (Dügâh = Mi♭4 / Hisar)", -1, -5),
    ("Süpürde Ney (Dügâh = Re4 / Nevâ)", -1, -9),
    ("Müstahsen Ney (Dügâh = Do#4 / Nim Hicaz)", -2, -14),
    ("Yıldız Ney (Dügâh = Do4 / Çargâh)", -2, -18),
    ("Kız Ney (Dügâh = Si3 / Bûselik)", -3, -22),
    ("Kız–Mansur Mâbeyni (Dügâh = Si♭3 / Dik Kürdî)", -3, -26),
    ("Mansur Ney (Dügâh = La3 / Piyano Ahengi)", -4, -31),
    ("Şah–Mansur Mâbeyni (Dügâh = Sol#3 / Zirgüle)", -5, -36),
    ("Şah Ney (Dügâh = Sol3 / Rast)", -5, -40),
    ("Dâvud Ney (Dügâh = Fa#3 / Irak)", -6, -45),
    ("Dâvud–Bolâhenk Mâbeyni (Dügâh = Fa3 / Acemaşîrân)", -6, -49),
    ("Ana Bolâhenk Ney (Dügâh = Mi3 / Hüseynîaşîrân — -1 Oktav)", -7, -53),
]


def interval_between_perdeler(src_idx: int, dst_idx: int) -> tuple[int, int]:
    """PERDE_LIST içindeki iki perde arasındaki (diatonic_steps, comma_shift) farkını döndürür."""
    _, d1, c1 = PERDE_LIST[src_idx]
    _, d2, c2 = PERDE_LIST[dst_idx]
    return d2 - d1, c2 - c1


def interval_between_ahenkler(elimdeki_ney_idx: int, hedef_ahenk_idx: int) -> tuple[int, int]:
    """
    Elimizdeki ney (elimdeki_ney_idx) ile istenen hedef ahenkte (hedef_ahenk_idx) icra yapmak
    için notanın göçürülmesi gereken (diatonic_steps, comma_shift) aralığını hesaplar.

    Örnekler:
    - Elimde Mansur (-4, -31) -> Bolâhenk Nısfiye (0, 0) ahenginde icra: (+4 adım, +31 koma) yukarı
    - Elimde Kız (-3, -22)    -> Süpürde (-1, -9) ahenginde icra:        (+2 adım, +13 koma) yukarı
    - Elimde Süpürde (-1, -9) -> Kız (-3, -22) ahenginde icra:           (-2 adım, -13 koma) aşağı
    - Elimde Süpürde (-1, -9) -> Mansur (-4, -31) ahenginde icra:        (-3 adım, -22 koma) aşağı
    """
    _, d_hand, c_hand = NEY_AHENKLERI[elimdeki_ney_idx]
    _, d_target, c_target = NEY_AHENKLERI[hedef_ahenk_idx]
    return d_target - d_hand, c_target - c_hand


def transpose_step_octave(step: str, octave: int, diatonic_steps: int) -> tuple[str, int]:
    """(step, octave) ikilisini diatonic_steps kadar kaydırır (B -> C oktav geçişini tam korur)."""
    base_idx = octave * 7 + STEPS.index(step.upper())
    new_idx = base_idx + diatonic_steps
    new_octave, step_i = divmod(new_idx, 7)
    return STEPS[step_i], new_octave


def transpose_pitch_and_commas(
    step: str,
    octave: int,
    delta_commas: int,
    diatonic_steps: int,
    comma_shift: int,
) -> tuple[str, int, int]:
    """
    53-Koma AEU göçürme temel denklemi:
      Mutlak koma (C0=0): old_abs = octave * 53 + AEU_STEP_COMMAS[step] + delta_commas
      Yeni mutlak koma:   new_abs = old_abs + comma_shift
      Yeni doğal koma:    nat_abs = new_octave * 53 + AEU_STEP_COMMAS[new_step]
      Yeni arıza (koma):  new_delta = new_abs - nat_abs
    """
    step = step.upper()
    new_step, new_octave = transpose_step_octave(step, octave, diatonic_steps)
    old_abs = octave * 53 + AEU_STEP_COMMAS[step] + delta_commas
    new_abs = old_abs + comma_shift
    nat_abs = new_octave * 53 + AEU_STEP_COMMAS[new_step]
    new_delta = new_abs - nat_abs
    return new_step, new_octave, new_delta


def transpose_keysig_map(
    orig_keysig: dict[str, int],
    diatonic_steps: int,
    comma_shift: int,
) -> dict[str, int]:
    """
    7 diyatonik sesin tamamı için (C..B -> delta_commas) donanım tablosunu
    (diatonic_steps, comma_shift) kadar göçürür ve yeni (C..B -> new_delta_commas) sözlüğünü döndürür.
    """
    if diatonic_steps == 0 and comma_shift == 0:
        return {s: orig_keysig.get(s, 0) for s in STEPS}

    new_map: dict[str, int] = {}
    for s in STEPS:
        old_d = orig_keysig.get(s, 0)
        ns, _, nd = transpose_pitch_and_commas(s, 4, old_d, diatonic_steps, comma_shift)
        new_map[ns] = nd
    return new_map


# Geleneksel donanım yazım sırası (diyezler ve bemoller için klasik Türk müziği sırası)
KEYSIG_ORDER_PRIORITY = ["F", "C", "G", "D", "A", "E", "B"]
KEYSIG_FLAT_ORDER = ["B", "E", "A", "D", "G", "C", "F"]


def order_keysig_signs(keysig_map: dict[str, int]) -> list[tuple[str, int]]:
    """Sıfırdan farklı donanım işaretlerini geleneksel sol anahtarı sırasına göre dizerek döndürür."""
    non_zero = {s: c for s, c in keysig_map.items() if c != 0}
    if not non_zero:
        return []
    # Tümü bemol ise B-E-A-D-G-C-F, aksi halde F-C-G-D-A-E-B (veya önce diyezler sonra bemoller)
    if all(c < 0 for c in non_zero.values()):
        return [(s, non_zero[s]) for s in KEYSIG_FLAT_ORDER if s in non_zero]
    sharps = [(s, non_zero[s]) for s in KEYSIG_ORDER_PRIORITY if s in non_zero and non_zero[s] > 0]
    flats = [(s, non_zero[s]) for s in KEYSIG_FLAT_ORDER if s in non_zero and non_zero[s] < 0]
    return sharps + flats


def compute_53edo_tuning_cents(step: str, true_delta_commas: int, midi_alter: int) -> float:
    """
    MuseScore <Note><tuning> alanı için 12-TET MIDI perdesine göre gereken cent farkını hesaplar.
    1 koma = 1200 / 53 ≈ 22.6415 cent (C = 0 cent referanslı).
    """
    step = step.upper()
    target_cents = (AEU_STEP_COMMAS[step] + true_delta_commas) * CENTS_PER_COMMA
    midi_cents = (STEP_SEMITONE[step] + midi_alter) * 100.0
    return round(target_cents - midi_cents, 2)


def note_midi_and_tpc(step: str, octave: int, tpc_alter: int = 0) -> tuple[int, int]:
    step = step.upper()
    pitch = (octave + 1) * 12 + STEP_SEMITONE[step] + tpc_alter
    tpc = STEP_TPC_NATURAL[step] + tpc_alter * 7
    return pitch, tpc
