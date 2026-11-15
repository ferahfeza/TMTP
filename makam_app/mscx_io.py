"""
MuseScore 4.6 (.mscx / .mscz) Çift Yönlü Dönüştürücü ve Kayıpsız XML Göçürme (Transpoze) Motoru.
"""

import xml.etree.ElementTree as ET
from fractions import Fraction
from xml.dom import minidom

from .aeu_theory import (
    CUSTDEF_DEGREE_TO_STEP,
    STEPS,
    STEP_SEMITONE,
    STEP_TO_CUSTDEF_DEGREE,
    STEP_TPC_NATURAL,
    XML_ACC_TO_COMMAS,
    compute_53edo_tuning_cents,
    nearest_aeu_accidental,
    note_midi_and_tpc,
    order_keysig_signs,
    transpose_keysig_map,
    transpose_pitch_and_commas,
)
from .score_model import (
    MeasureSpec,
    NoteEvent,
    ScoreDoc,
    auto_beam_measure_events,
    duration_to_fraction,
)

# Standart Batı concertKey (-7..+7) -> AEU 5-koma diyez/bemol donanım karşılıkları
CONCERT_KEY_FLATS = ["B", "E", "A", "D", "G", "C", "F"]
CONCERT_KEY_SHARPS = ["F", "C", "G", "D", "A", "E", "B"]

# TPC -> (step, alter) geri çözüm tablosu
TPC_TO_STEP_ALTER: dict[int, tuple[str, int]] = {}
for _st, _ntpc in STEP_TPC_NATURAL.items():
    for _alt in (-2, -1, 0, 1, 2):
        TPC_TO_STEP_ALTER[_ntpc + _alt * 7] = (_st, _alt)


def extract_keysig_from_mscx(mscx_xml: str) -> dict[str, int]:
    """Bir .mscx metnindeki ilk KeySig elemanından {step: delta_commas} donanım sözlüğünü çıkarır."""
    root = ET.fromstring(mscx_xml)
    ks = root.find(".//Staff[@id='1']//Measure/voice/KeySig")
    if ks is None:
        ks = root.find(".//KeySig")
    result: dict[str, int] = {s: 0 for s in STEPS}
    if ks is None:
        return result

    custom_el = ks.find("custom")
    if custom_el is not None and (custom_el.text or "").strip() == "1":
        for cd in ks.findall("CustDef"):
            sym_el = cd.find("sym")
            def_el = cd.find("def")
            if sym_el is not None and def_el is not None:
                sym_name = (sym_el.text or "").strip()
                deg = int(def_el.get("degree", "0"))
                step = CUSTDEF_DEGREE_TO_STEP.get(deg)
                if step and sym_name in XML_ACC_TO_COMMAS:
                    result[step] = XML_ACC_TO_COMMAS[sym_name]
        return result

    ck_el = ks.find("concertKey")
    if ck_el is not None and ck_el.text:
        ck = int(ck_el.text.strip())
        if ck < 0:
            for s in CONCERT_KEY_FLATS[: abs(ck)]:
                result[s] = -5
        elif ck > 0:
            for s in CONCERT_KEY_SHARPS[:ck]:
                result[s] = 5
    return result


def build_keysig_element(keysig_map: dict[str, int]) -> ET.Element:
    """Verilen {step: delta_commas} sözlüğünden MuseScore 4 <KeySig> XML elemanı üretir."""
    ks = ET.Element("KeySig")
    ET.SubElement(ks, "concertKey").text = "0"
    ordered = order_keysig_signs(keysig_map)
    if ordered:
        ET.SubElement(ks, "custom").text = "1"
        for step, commas in ordered:
            info = nearest_aeu_accidental(commas)
            if info.keysig_sym:
                cd = ET.SubElement(ks, "CustDef")
                ET.SubElement(cd, "sym").text = info.keysig_sym
                ET.SubElement(
                    cd,
                    "def",
                    degree=str(STEP_TO_CUSTDEF_DEGREE[step]),
                    xAlt="0",
                    octAlt="0",
                )
    return ks


def _decode_xml_note_state(
    note_el: ET.Element,
    orig_ks: dict[str, int],
    orig_active: dict[tuple[str, int], int],
) -> tuple[str, int, int, bool]:
    """
    Mevcut XML <Note> elemanından (step, octave, true_delta_commas, is_cautionary) bilgisini çözer
    ve orig_active sözlüğünü günceller.
    """
    pitch_val = int(note_el.find("pitch").text)
    tpc_val = int(note_el.find("tpc").text)
    step, tpc_alter = TPC_TO_STEP_ALTER.get(tpc_val, ("C", 0))
    semitone = STEP_SEMITONE[step]
    octave = (pitch_val - tpc_alter - semitone) // 12 - 1

    ac_el = note_el.find("Accidental")
    sym_el = note_el.find("Symbol")
    is_cautionary = False

    explicit_commas: int | None = None
    if sym_el is not None:
        sname = (sym_el.findtext("name") or "").strip()
        if sname in XML_ACC_TO_COMMAS:
            explicit_commas = XML_ACC_TO_COMMAS[sname]
    if ac_el is not None:
        br = ac_el.findtext("bracket")
        if br and br.strip() in ("1", "2"):
            is_cautionary = True
        sub = (ac_el.findtext("subtype") or "").strip()
        if sub in XML_ACC_TO_COMMAS:
            explicit_commas = XML_ACC_TO_COMMAS[sub]

    if explicit_commas is not None:
        delta = explicit_commas
        orig_active[(step, octave)] = delta
    elif tpc_alter != 0:
        # FLAT_CONT (-1 -> -5 koma) veya SHARP_CONT (+1 -> +4/+5 koma)
        if (step, octave) in orig_active:
            delta = orig_active[(step, octave)]
        else:
            delta = -5 if tpc_alter < 0 else (orig_ks.get(step, 0) or 4)
            orig_active[(step, octave)] = delta
    else:
        delta = orig_active.get((step, octave), orig_ks.get(step, 0))

    return step, octave, delta, is_cautionary


def _apply_transposed_note_to_xml(
    note_el: ET.Element,
    new_step: str,
    new_octave: int,
    new_delta: int,
    is_cautionary: bool,
    target_ks: dict[str, int],
    new_active_commas: dict[tuple[str, int], int],
    new_active_tpc_alter: dict[tuple[str, int], int],
    apply_53edo_tuning: bool,
) -> None:
    """Yeni (step, octave, delta_commas) bilgisini MuseScore 4.6 kurallarıyla <Note> içine yazar."""
    # Eski Accidental, AEU Symbol ve tuning elemanlarını temizle
    old_ac = note_el.find("Accidental")
    if old_ac is not None:
        note_el.remove(old_ac)
    for old_sym in list(note_el.findall("Symbol")):
        sname = (old_sym.findtext("name") or "").strip()
        if sname in XML_ACC_TO_COMMAS:
            note_el.remove(old_sym)
    old_tun = note_el.find("tuning")
    if old_tun is not None:
        note_el.remove(old_tun)

    expected_commas = new_active_commas.get((new_step, new_octave), target_ks.get(new_step, 0))
    need_print = is_cautionary or (new_delta != expected_commas)

    acc_info = nearest_aeu_accidental(new_delta)
    if need_print:
        new_active_commas[(new_step, new_octave)] = new_delta
        tpc_alter = acc_info.tpc_alter
        new_active_tpc_alter[(new_step, new_octave)] = tpc_alter
    else:
        tpc_alter = new_active_tpc_alter.get((new_step, new_octave), 0)

    new_pitch, new_tpc = note_midi_and_tpc(new_step, new_octave, tpc_alter)
    note_el.find("pitch").text = str(new_pitch)
    note_el.find("tpc").text = str(new_tpc)

    pitch_idx = list(note_el).index(note_el.find("pitch"))
    if need_print:
        if acc_info.note_symbol_Attach:
            sym_el = ET.Element("Symbol")
            ET.SubElement(sym_el, "name").text = acc_info.note_symbol_Attach
            ET.SubElement(sym_el, "offset", x="-1.25", y="0.0")
            note_el.insert(pitch_idx, sym_el)
        elif acc_info.note_subtype:
            ac_el = ET.Element("Accidental")
            if is_cautionary:
                ET.SubElement(ac_el, "bracket").text = "1"
            ET.SubElement(ac_el, "role").text = "1"
            ET.SubElement(ac_el, "subtype").text = acc_info.note_subtype
            note_el.insert(pitch_idx, ac_el)

    if apply_53edo_tuning:
        tun_cents = compute_53edo_tuning_cents(new_step, new_delta, tpc_alter)
        if abs(tun_cents) >= 0.05:
            tpc_idx = list(note_el).index(note_el.find("tpc"))
            tun_el = ET.Element("tuning")
            tun_el.text = f"{tun_cents:.2f}"
            note_el.insert(tpc_idx + 1, tun_el)


def transpose_mscx_lossless(
    mscx_xml: str,
    diatonic_steps: int = 0,
    comma_shift: int = 0,
    custom_keysig_override: dict[str, int] | None = None,
    apply_53edo_tuning: bool = True,
    subtitle_addition: str | None = None,
) -> str:
    """
    Mevcut bir .mscx XML ağacını güfte, bağ, dolap, Segno, çoklu ses ve sayfa düzenini
    %100 koruyarak 53-koma AEU sisteminde replika (0, 0) veya transpoze eder.
    """
    orig_ks = extract_keysig_from_mscx(mscx_xml)
    computed_ks = transpose_keysig_map(orig_ks, diatonic_steps, comma_shift)
    target_ks = (
        {s: custom_keysig_override.get(s, 0) for s in STEPS}
        if custom_keysig_override is not None
        else computed_ks
    )

    root = ET.fromstring(mscx_xml)
    score = root.find("Score")
    if score is None:
        raise ValueError("Geçersiz .mscx dosyası: <Score> elemanı bulunamadı.")

    # İsteğe bağlı alt başlık bilgisi ekleme
    if subtitle_addition:
        for vb in score.findall(".//VBox"):
            sub_found = False
            for tx in vb.findall("Text"):
                if (tx.findtext("style") or "").strip() == "subtitle":
                    cur = tx.findtext("text") or ""
                    if subtitle_addition not in cur:
                        tx.find("text").text = f"{subtitle_addition}\n{cur}".strip()
                    sub_found = True
                    break
            if not sub_found:
                t_sub = ET.SubElement(vb, "Text")
                ET.SubElement(t_sub, "style").text = "subtitle"
                ET.SubElement(t_sub, "text").text = subtitle_addition

    for staff in score.findall("Staff"):
        measures = staff.findall("Measure")
        for m_idx, mel in enumerate(measures):
            voices = mel.findall("voice")
            if not voices:
                continue

            # İlk ölçüdeki KeySig elemanını güncelle (yoksa ve donanım boş değilse ekle)
            v0 = voices[0]
            ks_el = v0.find("KeySig")
            if m_idx == 0:
                new_ks_el = build_keysig_element(target_ks)
                if ks_el is not None:
                    idx_ks = list(v0).index(ks_el)
                    v0.remove(ks_el)
                    v0.insert(idx_ks, new_ks_el)
                elif any(v != 0 for v in target_ks.values()):
                    clef_el = v0.find("Clef")
                    ins_pos = (list(v0).index(clef_el) + 1) if clef_el is not None else 0
                    v0.insert(ins_pos, new_ks_el)
            elif ks_el is not None:
                # Ara ölçüde modülasyon donanımı varsa onu da göçür
                idx_ks = list(v0).index(ks_el)
                v0.remove(ks_el)
                v0.insert(idx_ks, build_keysig_element(target_ks))

            # Her voice içindeki notaları ölçü bazında göçür
            orig_active: dict[tuple[str, int], int] = {}
            new_active_commas: dict[tuple[str, int], int] = {}
            new_active_tpc_alter: dict[tuple[str, int], int] = {}

            for v in voices:
                for chord in v.findall("Chord"):
                    for note_el in chord.findall("Note"):
                        s_old, o_old, d_old, is_caut = _decode_xml_note_state(
                            note_el, orig_ks, orig_active
                        )
                        if diatonic_steps == 0 and comma_shift == 0 and custom_keysig_override is None:
                            # Saf replika: görsel arızalara dokunma, yalnızca 53-EDO cent tuning uygula/temizle
                            old_tun = note_el.find("tuning")
                            if old_tun is not None:
                                note_el.remove(old_tun)
                            if apply_53edo_tuning:
                                tpc_val = int(note_el.find("tpc").text)
                                _, tpc_alt = TPC_TO_STEP_ALTER.get(tpc_val, (s_old, 0))
                                tun_cents = compute_53edo_tuning_cents(s_old, d_old, tpc_alt)
                                if abs(tun_cents) >= 0.05:
                                    tpc_idx = list(note_el).index(note_el.find("tpc"))
                                    tun_el = ET.Element("tuning")
                                    tun_el.text = f"{tun_cents:.2f}"
                                    note_el.insert(tpc_idx + 1, tun_el)
                        else:
                            s_new, o_new, d_new = transpose_pitch_and_commas(
                                s_old, o_old, d_old, diatonic_steps, comma_shift
                            )
                            _apply_transposed_note_to_xml(
                                note_el,
                                s_new,
                                o_new,
                                d_new,
                                is_caut,
                                target_ks,
                                new_active_commas,
                                new_active_tpc_alter,
                                apply_53edo_tuning,
                            )

    for elem in root.iter():
        if elem.text is not None and elem.text.strip() == "":
            elem.text = None
        if elem.tail is not None and elem.tail.strip() == "":
            elem.tail = None

    return minidom.parseString(ET.tostring(root, encoding="utf-8")).toprettyxml(indent="  ")


def mscx_to_score_doc(mscx_xml: str) -> ScoreDoc:
    """Bir .mscx dosyasını arayüzde incelemek ve düzenlemek üzere ScoreDoc modeline çevirir."""
    orig_ks = extract_keysig_from_mscx(mscx_xml)
    root = ET.fromstring(mscx_xml)
    score = root.find("Score")

    title = ""
    subtitle = ""
    composer = ""
    lyricist = ""

    vb0 = score.find(".//VBox")
    if vb0 is not None:
        for tx in vb0.findall("Text"):
            st = (tx.findtext("style") or "").strip()
            val = (tx.findtext("text") or "").strip()
            if st == "title":
                title = val
            elif st == "subtitle":
                subtitle = val
            elif st == "composer":
                composer = val
            elif st == "lyricist":
                lyricist = val

    if not title:
        for mt in score.findall("metaTag"):
            if mt.get("name") == "workTitle" and mt.text:
                title = mt.text.strip()
            elif mt.get("name") == "composer" and mt.text and not composer:
                composer = mt.text.strip()

    spatium = 1.55
    sp_el = score.find("./Style/Spatium")
    if sp_el is not None and sp_el.text:
        try:
            spatium = float(sp_el.text.strip())
        except ValueError:
            pass

    doc = ScoreDoc(
        title=title or "Türk Müziği Eseri",
        subtitle=subtitle,
        composer=composer,
        lyricist=lyricist,
        spatium=spatium,
        keysig={k: v for k, v in orig_ks.items() if v != 0},
    )

    staff = score.find("Staff[@id='1']")
    if staff is None:
        staff = score.find("Staff")
    if staff is None:
        return doc

    current_ts_str = "4/4"
    measures = staff.findall("Measure")
    for idx, mel in enumerate(measures, start=1):
        v0 = mel.find("voice")
        if v0 is None:
            continue

        nominal_ts: list[int] | None = None
        ts_el = v0.find("TimeSig")
        if ts_el is not None:
            sn = int(ts_el.findtext("sigN") or "4")
            sd = int(ts_el.findtext("sigD") or "4")
            tn = ts_el.findtext("textN")
            td = ts_el.findtext("textD")
            if tn and td:
                nominal_ts = [int(tn), int(td)]
            else:
                nominal_ts = [sn, sd]
            current_ts_str = f"{sn}/{sd}"

        actual_len = mel.get("len") or current_ts_str
        mspec = MeasureSpec(
            number=idx,
            nominal_ts=nominal_ts,
            actual_len=actual_len,
            start_repeat=(mel.find("startRepeat") is not None),
            end_repeat=(mel.find("endRepeat") is not None),
        )

        for lb in mel.findall("LayoutBreak"):
            sub = (lb.findtext("subtype") or "").strip()
            if sub == "line":
                mspec.line_break = True
            elif sub == "page":
                mspec.page_break = True

        for mk in mel.findall("Marker"):
            if mk.find(".//sym") is not None and (mk.findtext(".//sym") or "").strip() == "segno":
                mspec.segno_end = True

        tm_el = v0.find("Tempo")
        if tm_el is not None:
            mspec.tempo_text = (tm_el.findtext("text") or "").strip()
            if tm_el.findtext("tempo"):
                mspec.tempo_qpm = round(float(tm_el.findtext("tempo")) * 60.0, 2)

        bl_el = v0.find("BarLine")
        if bl_el is not None:
            mspec.barline = (bl_el.findtext("subtype") or "").strip()

        orig_active: dict[tuple[str, int], int] = {}
        pending_grace: tuple[str, int | None] | None = None

        for child in v0:
            if child.tag == "Rest":
                dtype = (child.findtext("durationType") or "quarter").strip()
                if dtype == "measure":
                    # Tam ölçü es
                    f_m = duration_to_fraction("whole")
                    dtype = "whole"
                dots = int(child.findtext("dots") or "0")
                dur_s = dtype + ("." * dots)
                mspec.events.append(NoteEvent(kind="rest", duration=dur_s))
            elif child.tag == "Chord":
                note_el = child.find("Note")
                if note_el is None:
                    continue
                s_old, o_old, d_old, is_caut = _decode_xml_note_state(note_el, orig_ks, orig_active)
                has_explicit = (note_el.find("Accidental") is not None) or (note_el.find("Symbol") is not None)
                if child.find("acciaccatura") is not None or child.find("appoggiatura") is not None:
                    pending_grace = (f"{s_old}{o_old}", d_old if has_explicit else None)
                    continue

                dtype = (child.findtext("durationType") or "quarter").strip()
                dots = int(child.findtext("dots") or "0")
                dur_s = dtype + ("." * dots)
                bm = (child.findtext("BeamMode") or "no").strip()
                stem_d = (child.findtext("StemDirection") or "").strip() or None

                tie_start = False
                tie_end = False
                for sp in note_el.findall("Spanner[@type='Tie']"):
                    if sp.find("next") is not None:
                        tie_start = True
                    if sp.find("prev") is not None:
                        tie_end = True

                lyr_text: str | None = None
                lyr_el = child.find("Lyrics")
                if lyr_el is not None and lyr_el.findtext("text"):
                    lyr_text = lyr_el.findtext("text")
                    syl = (lyr_el.findtext("syllabic") or "").strip()
                    if syl in ("begin", "middle"):
                        lyr_text += "-"

                ev = NoteEvent(
                    kind="note",
                    step=s_old,
                    octave=o_old,
                    duration=dur_s,
                    beam=bm,
                    accidental_commas=d_old if (has_explicit or is_caut) else None,
                    cautionary=is_caut,
                    tie_start=tie_start,
                    tie_end=tie_end,
                    grace_before=pending_grace[0] if pending_grace else None,
                    grace_commas=pending_grace[1] if pending_grace else None,
                    lyric=lyr_text,
                    stem_dir=stem_d,
                )
                pending_grace = None
                mspec.events.append(ev)

        doc.measures.append(mspec)

    return doc


def score_doc_to_mscx(doc: ScoreDoc, apply_53edo_tuning: bool = True) -> str:
    """ScoreDoc modelini yayın kalitesinde MuseScore 4.6 .mscx XML metnine derler."""
    root = ET.Element("museScore", version="4.60")
    score = ET.SubElement(root, "Score")
    ET.SubElement(score, "Division").text = "480"

    style = ET.SubElement(score, "Style")
    ET.SubElement(style, "pageWidth").text = "8.27"
    ET.SubElement(style, "pageHeight").text = "11.69"
    ET.SubElement(style, "pagePrintableWidth").text = "6.95"
    ET.SubElement(style, "pageEvenLeftMargin").text = "0.72"
    ET.SubElement(style, "pageOddLeftMargin").text = "0.72"
    ET.SubElement(style, "Spatium").text = f"{doc.spatium:.2f}"
    ET.SubElement(style, "showMeasureNumber").text = "0"
    ET.SubElement(style, "showMeasureNumberOne").text = "0"
    ET.SubElement(style, "measureNumberSystem").text = "0"
    ET.SubElement(style, "genCourtesyTimesig").text = "0"
    ET.SubElement(style, "genCourtesyKeysig").text = "0"
    ET.SubElement(style, "showHeader").text = "0"
    ET.SubElement(style, "showFooter").text = "0"

    ET.SubElement(score, "metaTag", name="workTitle").text = doc.title
    ET.SubElement(score, "metaTag", name="composer").text = doc.composer

    part = ET.SubElement(score, "Part", id="1")
    st_def = ET.SubElement(part, "Staff", id="1")
    st_type = ET.SubElement(st_def, "StaffType", group="pitched")
    ET.SubElement(st_type, "name").text = "stdNormal"
    ET.SubElement(part, "trackName").text = doc.instrument_name
    inst = ET.SubElement(part, "Instrument", id="flute")
    ET.SubElement(inst, "longName").text = ""
    ET.SubElement(inst, "shortName").text = ""
    ch = ET.SubElement(inst, "Channel")
    ET.SubElement(ch, "program", value=str(doc.midi_program))

    staff = ET.SubElement(score, "Staff", id="1")

    def emit_vbox(page_label: str | None = None):
        vb = ET.SubElement(staff, "VBox")
        sub_txt = doc.subtitle
        if page_label:
            sub_txt = f"{sub_txt}\n— {page_label} —".strip()
        right_credits = []
        if doc.lyricist:
            right_credits.append(f"Güfte: {doc.lyricist}")
        if doc.composer:
            right_credits.append(doc.composer)
        vbox_h = "15.5" if (sub_txt and right_credits) else "10.5"
        ET.SubElement(vb, "height").text = vbox_h
        t1 = ET.SubElement(vb, "Text")
        ET.SubElement(t1, "style").text = "title"
        ET.SubElement(t1, "text").text = doc.title
        if sub_txt:
            t2 = ET.SubElement(vb, "Text")
            ET.SubElement(t2, "style").text = "subtitle"
            ET.SubElement(t2, "text").text = sub_txt
        if right_credits:
            t3 = ET.SubElement(vb, "Text")
            ET.SubElement(t3, "style").text = "composer"
            ET.SubElement(t3, "text").text = "\n".join(right_credits)

    emit_vbox(None)

    ks_map = {s: doc.keysig.get(s, 0) for s in STEPS}
    ks_sign_count = sum(1 for v in ks_map.values() if v != 0)
    in_lyric_hyphen: dict[int, bool] = {}
    open_volta_from_prev = False
    pending_cross_bar_tie_frac: Fraction | None = None

    for idx, m in enumerate(doc.measures):
        if m.vbox_page_label:
            emit_vbox(m.vbox_page_label)

        auto_beam_measure_events(m)
        mel = ET.SubElement(staff, "Measure", len=m.actual_len)
        if m.start_repeat:
            ET.SubElement(mel, "startRepeat")
        if m.end_repeat:
            ET.SubElement(mel, "endRepeat").text = "2"

        voice = ET.SubElement(mel, "voice")

        if open_volta_from_prev:
            sp_v_close = ET.SubElement(voice, "Spanner", type="Volta")
            prv_v = ET.SubElement(sp_v_close, "prev")
            loc_v = ET.SubElement(prv_v, "location")
            ET.SubElement(loc_v, "measures").text = "-1"
            open_volta_from_prev = False

        if idx == 0:
            clef = ET.SubElement(voice, "Clef")
            ET.SubElement(clef, "concertClefType").text = "G"
            ET.SubElement(clef, "transposingClefType").text = "G"
            ET.SubElement(clef, "isHeader").text = "1"
            voice.append(build_keysig_element(ks_map))

        if m.nominal_ts or idx == 0:
            nom = m.nominal_ts
            act_n, act_d = [int(x) for x in m.actual_len.split("/")]
            ts = ET.SubElement(voice, "TimeSig")
            ET.SubElement(ts, "sigN").text = str(act_n)
            ET.SubElement(ts, "sigD").text = str(act_d)
            if nom and (nom[0] != act_n or nom[1] != act_d):
                ET.SubElement(ts, "textN").text = str(nom[0])
                ET.SubElement(ts, "textD").text = str(nom[1])
            ET.SubElement(ts, "showCourtesySig").text = "0"

        if m.volta in (1, 2):
            sp_v = ET.SubElement(voice, "Spanner", type="Volta")
            vt = ET.SubElement(sp_v, "Volta")
            ET.SubElement(vt, "endHookType").text = "1"
            ET.SubElement(vt, "beginText").text = f"{m.volta}."
            ET.SubElement(vt, "endings").text = str(m.volta)
            nxt_v = ET.SubElement(sp_v, "next")
            loc_v = ET.SubElement(nxt_v, "location")
            ET.SubElement(loc_v, "measures").text = "1"
            open_volta_from_prev = True

        if m.tempo_text:
            qpm = m.tempo_qpm or 80.0
            tm = ET.SubElement(voice, "Tempo")
            ET.SubElement(tm, "tempo").text = f"{qpm / 60.0:.6f}"
            ET.SubElement(tm, "followText").text = "0"
            ET.SubElement(tm, "offset", x="-5.5", y="-2.8")
            ET.SubElement(tm, "text").text = m.tempo_text

        if m.section_label:
            base_ox = -14.8 if (idx == 0 or m.nominal_ts) else -11.8
            ox = base_ox - (ks_sign_count * 1.1)
            st = ET.SubElement(voice, "StaffText")
            ET.SubElement(st, "placement").text = "above"
            ET.SubElement(st, "align").text = "center,center"
            ET.SubElement(st, "autoplace").text = "0"
            ET.SubElement(st, "size").text = "10.0"
            ET.SubElement(st, "offset", x=f"{ox:.1f}", y="2.0")
            ET.SubElement(st, "text").text = m.section_label

        if m.segno_start:
            st_s = ET.SubElement(voice, "StaffText")
            ET.SubElement(st_s, "style").text = "repeat_left"
            ET.SubElement(st_s, "placement").text = "above"
            ET.SubElement(st_s, "autoplace").text = "0"
            ET.SubElement(st_s, "offset", x=f"{-6.8 - ks_sign_count * 0.9:.1f}", y="-2.2")
            tx_s = ET.SubElement(st_s, "text")
            ET.SubElement(tx_s, "sym").text = "segno"

        if m.footnote_text:
            st_f = ET.SubElement(voice, "StaffText")
            ET.SubElement(st_f, "placement").text = "below"
            ET.SubElement(st_f, "align").text = "left,top"
            ET.SubElement(st_f, "autoplace").text = "0"
            ET.SubElement(st_f, "size").text = "8.5"
            ET.SubElement(st_f, "offset", x="-9.0", y="5.5")
            tx_f = ET.SubElement(st_f, "text")
            ET.SubElement(tx_f, "i").text = m.footnote_text

        active_commas: dict[tuple[str, int], int] = {}
        active_tpc_alter: dict[tuple[str, int], int] = {}
        prev_tie_frac_in_bar: Fraction | None = None

        for ev_i, ev in enumerate(m.events):
            ev_frac = duration_to_fraction(ev.duration)
            if ev.kind == "rest":
                r_el = ET.SubElement(voice, "Rest")
                d_s = ev.duration
                if d_s.endswith("."):
                    ET.SubElement(r_el, "dots").text = "1"
                    d_s = d_s[:-1]
                ET.SubElement(r_el, "durationType").text = d_s
                continue

            # Varsa çarpma notası (acciaccatura)
            slur_from_grace = False
            if ev.grace_before:
                gs = ev.grace_before[0].upper()
                go = int(ev.grace_before[1:])
                g_commas = ev.grace_commas if ev.grace_commas is not None else active_commas.get((gs, go), ks_map.get(gs, 0))
                g_info = nearest_aeu_accidental(g_commas)
                g_alt = g_info.tpc_alter if ev.grace_commas is not None else active_tpc_alter.get((gs, go), 0)
                gp, gt = note_midi_and_tpc(gs, go, g_alt)

                gc = ET.SubElement(voice, "Chord")
                ET.SubElement(gc, "acciaccatura")
                ET.SubElement(gc, "durationType").text = "eighth"
                ET.SubElement(gc, "StemDirection").text = "up"
                sp_g = ET.SubElement(gc, "Spanner", type="Slur")
                sl_g = ET.SubElement(sp_g, "Slur")
                ET.SubElement(sl_g, "up").text = "down"
                nxt_g = ET.SubElement(sp_g, "next")
                ET.SubElement(nxt_g, "location")
                gn = ET.SubElement(gc, "Note")
                if ev.grace_commas is not None and g_info.note_subtype:
                    gac = ET.SubElement(gn, "Accidental")
                    ET.SubElement(gac, "role").text = "1"
                    ET.SubElement(gac, "subtype").text = g_info.note_subtype
                ET.SubElement(gn, "pitch").text = str(gp)
                ET.SubElement(gn, "tpc").text = str(gt)
                slur_from_grace = True

            ch_el = ET.SubElement(voice, "Chord")
            d_s = ev.duration
            if d_s.endswith("."):
                ET.SubElement(ch_el, "dots").text = "1"
                d_s = d_s[:-1]
            ET.SubElement(ch_el, "durationType").text = d_s
            if ev.beam:
                ET.SubElement(ch_el, "BeamMode").text = ev.beam
            if ev.stem_dir:
                ET.SubElement(ch_el, "StemDirection").text = ev.stem_dir

            if slur_from_grace:
                sp_sg = ET.SubElement(ch_el, "Spanner", type="Slur")
                prv_sg = ET.SubElement(sp_sg, "prev")
                loc_sg = ET.SubElement(prv_sg, "location")
                ET.SubElement(loc_sg, "grace").text = "0"

            # Güfte (<Lyrics> — çok kıtalı destekli)
            verse_list: list[str] = list(ev.lyrics) if ev.lyrics else ([ev.lyric] if ev.lyric else [])
            for v_no, raw_item in enumerate(verse_list):
                if not raw_item:
                    continue
                lyr = ET.SubElement(ch_el, "Lyrics")
                if v_no > 0:
                    ET.SubElement(lyr, "no").text = str(v_no)
                raw_l = raw_item.strip()
                if raw_l.endswith("-") and len(raw_l) > 1:
                    syl_mode = "middle" if in_lyric_hyphen.get(v_no, False) else "begin"
                    in_lyric_hyphen[v_no] = True
                    raw_l = raw_l[:-1]
                    ET.SubElement(lyr, "syllabic").text = syl_mode
                elif in_lyric_hyphen.get(v_no, False):
                    ET.SubElement(lyr, "syllabic").text = "end"
                    in_lyric_hyphen[v_no] = False
                ET.SubElement(lyr, "text").text = raw_l

            # Perde, TPC, AEU arıza ve tuning hesabı
            s_cur = ev.step.upper()
            o_cur = ev.octave
            if ev.accidental_commas is not None:
                true_commas = ev.accidental_commas
                active_commas[(s_cur, o_cur)] = true_commas
                acc_info = nearest_aeu_accidental(true_commas)
                tpc_alt = acc_info.tpc_alter
                active_tpc_alter[(s_cur, o_cur)] = tpc_alt
                emit_acc = True
            else:
                true_commas = active_commas.get((s_cur, o_cur), ks_map.get(s_cur, 0))
                tpc_alt = active_tpc_alter.get((s_cur, o_cur), 0)
                acc_info = nearest_aeu_accidental(true_commas)
                emit_acc = False

            n_el = ET.SubElement(ch_el, "Note")

            # Bağ (Tie) başlangıç / bitiş
            if ev.tie_end:
                sp_te = ET.SubElement(n_el, "Spanner", type="Tie")
                prv_te = ET.SubElement(sp_te, "prev")
                loc_te = ET.SubElement(prv_te, "location")
                if prev_tie_frac_in_bar is not None:
                    ET.SubElement(loc_te, "fractions").text = f"-{prev_tie_frac_in_bar.numerator}/{prev_tie_frac_in_bar.denominator}"
                    prev_tie_frac_in_bar = None
                elif pending_cross_bar_tie_frac is not None:
                    ET.SubElement(loc_te, "measures").text = "-1"
                    pending_cross_bar_tie_frac = None

            if ev.tie_start:
                sp_ts = ET.SubElement(n_el, "Spanner", type="Tie")
                ET.SubElement(sp_ts, "Tie")
                nxt_ts = ET.SubElement(sp_ts, "next")
                loc_ts = ET.SubElement(nxt_ts, "location")
                if ev_i < len(m.events) - 1:
                    ET.SubElement(loc_ts, "fractions").text = f"{ev_frac.numerator}/{ev_frac.denominator}"
                    prev_tie_frac_in_bar = ev_frac
                else:
                    ET.SubElement(loc_ts, "measures").text = "1"
                    pending_cross_bar_tie_frac = ev_frac

            if emit_acc:
                if acc_info.note_symbol_Attach:
                    sym_el = ET.SubElement(n_el, "Symbol")
                    ET.SubElement(sym_el, "name").text = acc_info.note_symbol_Attach
                    ET.SubElement(sym_el, "offset", x="-1.25", y="0.0")
                elif acc_info.note_subtype:
                    ac_el = ET.SubElement(n_el, "Accidental")
                    if ev.cautionary:
                        ET.SubElement(ac_el, "bracket").text = "1"
                    ET.SubElement(ac_el, "role").text = "1"
                    ET.SubElement(ac_el, "subtype").text = acc_info.note_subtype

            p_val, tpc_val = note_midi_and_tpc(s_cur, o_cur, tpc_alt)
            ET.SubElement(n_el, "pitch").text = str(p_val)
            ET.SubElement(n_el, "tpc").text = str(tpc_val)

            if apply_53edo_tuning:
                tun_cents = compute_53edo_tuning_cents(s_cur, true_commas, tpc_alt)
                if abs(tun_cents) >= 0.05:
                    ET.SubElement(n_el, "tuning").text = f"{tun_cents:.2f}"

        if m.end_text or (idx == len(doc.measures) - 1 and doc.footer_right):
            etxt = m.end_text or doc.footer_right
            st_e = ET.SubElement(voice, "StaffText")
            ET.SubElement(st_e, "placement").text = "below"
            ET.SubElement(st_e, "align").text = "right,top"
            ET.SubElement(st_e, "text").text = etxt

        if m.segno_end:
            mk = ET.SubElement(mel, "Marker")
            ET.SubElement(mk, "style").text = "repeat_right"
            tx_m = ET.SubElement(mk, "text")
            ET.SubElement(tx_m, "sym").text = "segno"
            ET.SubElement(mk, "label").text = "segno"

        if m.barline:
            bl = ET.SubElement(voice, "BarLine")
            ET.SubElement(bl, "subtype").text = m.barline

        if m.beat3_quarter_rest:
            v2 = ET.SubElement(mel, "voice")
            r1 = ET.SubElement(v2, "Rest")
            ET.SubElement(r1, "visible").text = "0"
            ET.SubElement(r1, "durationType").text = "half"
            r2 = ET.SubElement(v2, "Rest")
            ET.SubElement(r2, "durationType").text = "quarter"
            ET.SubElement(r2, "offset", x="0", y="1.6")
            r3 = ET.SubElement(v2, "Rest")
            ET.SubElement(r3, "visible").text = "0"
            ET.SubElement(r3, "durationType").text = "quarter"

        if m.page_break:
            lb = ET.SubElement(mel, "LayoutBreak")
            ET.SubElement(lb, "subtype").text = "page"
        elif m.line_break:
            lb = ET.SubElement(mel, "LayoutBreak")
            ET.SubElement(lb, "subtype").text = "line"

    if open_volta_from_prev and len(staff.findall("Measure")) > 0:
        last_v = staff.findall("Measure")[-1].find("voice")
        sp_vc = ET.SubElement(last_v, "Spanner", type="Volta")
        prv_vc = ET.SubElement(sp_vc, "prev")
        loc_vc = ET.SubElement(prv_vc, "location")
        ET.SubElement(loc_vc, "measures").text = "-1"

    return minidom.parseString(ET.tostring(root, encoding="utf-8")).toprettyxml(indent="  ")
