"""
Çevrimdışı (Offline) Yerel OpenCV Tek Porteli Nota Tanıma (OMR) Motoru.
Salt görüntü (raster PDF / PNG / JPG) notalarında internet veya API anahtarı olmadan:
  1. 5 porte çizgisini dikey kalınlık filtresiyle siler ve çizgi üstü nota başlarını onarır,
  2. İlk satırda Sol anahtarından hemen sonraki donanım işaretlerini tespit eder,
  3. Ölçü çizgilerini (barline) iç saplardan ayırarak porteyi ölçülere böler,
  4. Eğik/oval dolu nota başlarını, yalnızca porte kuşağındaki içi boş nota başlarını (güfte harflerine inmeden!),
     uzatma noktalarını, sap/kiriş sayılarını, esleri ve nota önü arızalarını tespit eder.
"""

from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .aeu_theory import STEPS
from .omr_slicer import StaffSystemCrop, detect_and_slice_systems, render_input_pages
from .score_model import (
    MeasureSpec,
    NoteEvent,
    ScoreDoc,
    auto_beam_measure_events,
    duration_to_fraction,
)


def _estimate_staff_line_thickness(crop_bin: np.ndarray, staff_ys: list[int]) -> int:
    h, w = crop_bin.shape
    runs: list[int] = []
    sample_xs = np.linspace(int(w * 0.15), int(w * 0.85), 60, dtype=int)
    for x in sample_xs:
        for yc in staff_ys:
            if 0 <= yc < h and crop_bin[yc, x] > 0:
                t = yc
                while t > 0 and crop_bin[t - 1, x] > 0:
                    t -= 1
                b = yc
                while b + 1 < h and crop_bin[b + 1, x] > 0:
                    b += 1
                run_len = b - t + 1
                if run_len <= 8:
                    runs.append(run_len)
    return max(1, int(round(np.median(runs)))) if runs else 2


def _remove_staff_lines(crop_bin: np.ndarray, staff_ys: list[int], line_thick: int) -> np.ndarray:
    out = crop_bin.copy()
    h, w = out.shape
    max_erase = line_thick + 2
    half_band = max(2, line_thick + 1)

    for yc in staff_ys:
        y_min = max(0, yc - half_band)
        y_max = min(h - 1, yc + half_band)
        for x in range(w):
            col_seg = out[y_min : y_max + 1, x]
            if not np.any(col_seg > 0):
                continue
            y_seed = y_min + int(np.argmax(col_seg > 0))
            t = y_seed
            while t > 0 and out[t - 1, x] > 0:
                t -= 1
            b = y_seed
            while b + 1 < h and out[b + 1, x] > 0:
                b += 1
            if (b - t + 1) <= max_erase:
                out[t : b + 1, x] = 0

    # Çizgi silme sırasında ortasından incelen nota başlarını dikey 1x3 kapama ile birleştir
    repair_k = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 3))
    out = cv2.morphologyEx(out, cv2.MORPH_CLOSE, repair_k)
    return out


def _y_to_treble_step_octave(cy: float, y4: float, ss: float) -> tuple[str, int]:
    half_steps_from_e4 = int(round((y4 - cy) / (0.5 * ss)))
    diatonic_idx = max(21, min(46, 30 + half_steps_from_e4))
    octave, step_i = divmod(diatonic_idx, 7)
    return STEPS[step_i], octave


def _detect_barlines(
    crop_bin: np.ndarray,
    no_staff: np.ndarray,
    staff_ys: list[int],
    ss: float,
) -> list[tuple[int, int]]:
    """
    Porte yüksekliğinde kesintisiz inen ve yanında nota başı yapışık olmayan gerçek ölçü çizgilerini bulur.
    """
    h, w = crop_bin.shape
    y0, y4 = staff_ys[0], staff_ys[4]
    staff_h = max(1, y4 - y0)

    # Önce nota başı yoğun bölgeleri bul ki sapları barline sanmayalım
    kw = max(4, int(round(0.68 * ss)))
    kh = max(3, int(round(0.46 * ss)))
    nh_mask = cv2.morphologyEx(no_staff, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kw, kh)))

    candidates: list[int] = []
    for x in range(w):
        inside = crop_bin[y0 : y4 + 1, x]
        if float((inside > 0).sum()) / float(staff_h + 1) < 0.90:
            continue
        above = crop_bin[max(0, int(round(y0 - 1.3 * ss))) : max(0, y0 - 2), x]
        below = crop_bin[min(h, y4 + 3) : min(h, int(round(y4 + 1.3 * ss))), x]
        if (above > 0).sum() > max(2, int(0.20 * ss)) or (below > 0).sum() > max(2, int(0.20 * ss)):
            continue
        # y0 veya y4 ucunda yapışık nota başı varsa bu bir nota sapıdır, barline değildir!
        nx0 = max(0, int(round(x - 1.3 * ss)))
        nx1 = min(w, int(round(x + 1.3 * ss)))
        top_nh = (nh_mask[max(0, int(round(y0 - 0.6 * ss))) : min(h, int(round(y0 + 0.6 * ss))), nx0:nx1] > 0).sum()
        bot_nh = (nh_mask[max(0, int(round(y4 - 0.6 * ss))) : min(h, int(round(y4 + 0.6 * ss))), nx0:nx1] > 0).sum()
        if top_nh > 0.15 * ss * ss or bot_nh > 0.15 * ss * ss:
            continue
        candidates.append(x)

    if not candidates:
        return []

    clusters: list[tuple[int, int]] = []
    s = p = candidates[0]
    merge_gap = max(3, int(round(0.65 * ss)))
    for x in candidates[1:]:
        if x <= p + merge_gap:
            p = x
        else:
            clusters.append((s, p))
            s = p = x
    clusters.append((s, p))
    return clusters


def _detect_system1_keysig(
    no_staff: np.ndarray,
    staff_ys: list[int],
    staff_x_left: int,
    ss: float,
) -> dict[str, int]:
    """İlk porte satırında Sol anahtarının hemen sağındaki donanım arızalarını yaklaşık okur."""
    y0, y4 = staff_ys[0], staff_ys[4]
    kx0 = min(no_staff.shape[1], staff_x_left + int(round(3.3 * ss)))
    kx1 = min(no_staff.shape[1], staff_x_left + int(round(7.4 * ss)))
    ky0 = max(0, int(round(y0 - 1.2 * ss)))
    ky1 = min(no_staff.shape[0], int(round(y4 + 1.0 * ss)))
    roi = no_staff[ky0:ky1, kx0:kx1]
    ks: dict[str, int] = {}
    if roi.size == 0:
        return ks

    nl, _, st, cent = cv2.connectedComponentsWithStats(roi)
    for i in range(1, nl):
        rx, ry, rw, rh, area = st[i]
        _, cy = cent[i]
        if not (1.15 * ss <= rh <= 2.9 * ss and 0.30 * ss <= rw <= 1.25 * ss):
            continue
        if area < 0.16 * ss * ss:
            continue
        comp = roi[ry : ry + rh, rx : rx + rw]
        top_ink = (comp[: rh // 2, :] > 0).sum()
        bot_ink = (comp[rh // 2 :, :] > 0).sum()
        # Bemol türevlerinde alt göbek daha yoğundur; diyezlerde üst-alt dengelidir
        if bot_ink > top_ink * 1.18:
            # Göbek y merkezi alt yarıdadır
            pitch_y = ky0 + ry + rh * 0.72
            step, _ = _y_to_treble_step_octave(pitch_y, float(y4), ss)
            ks[step] = -4 if rw >= 0.62 * ss else -1
        elif rw >= 0.62 * ss:
            pitch_y = ky0 + cy
            step, _ = _y_to_treble_step_octave(pitch_y, float(y4), ss)
            ks[step] = 4
    return ks


def _detect_noteheads_in_measure(
    no_staff: np.ndarray,
    x0: int,
    x1: int,
    staff_ys: list[int],
    ss: float,
) -> list[dict]:
    """Ölçü dilimi içindeki dolu ve boş nota başlarını (güfte bandına inmeden!) tespit eder."""
    h, _ = no_staff.shape
    y0, y4 = staff_ys[0], staff_ys[4]
    # Porte altı güfte kuşağına (y4 + 2.0*ss altı) kesinlikle inme!
    y_lim_top = max(0, int(round(y0 - 2.8 * ss)))
    y_lim_bot = min(h, int(round(y4 + 1.65 * ss)))

    sub = no_staff[y_lim_top:y_lim_bot, x0:x1].copy()
    if sub.size == 0 or sub.shape[1] < int(ss):
        return []

    heads: list[dict] = []

    # 1. İçi dolu siyah nota başları (eğik elipsleri silmeyecek kompakt çekirdek)
    kw = max(4, int(round(0.68 * ss)))
    kh = max(3, int(round(0.46 * ss)))
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kw, kh))
    opened = cv2.morphologyEx(sub, cv2.MORPH_OPEN, kern)

    num_l, _, stats, centroids = cv2.connectedComponentsWithStats(opened)
    min_area = 0.20 * (ss * ss)
    max_area = 3.2 * (ss * ss)

    for lbl in range(1, num_l):
        bx, by, bw, bh, area = stats[lbl]
        cx, cy = centroids[lbl]
        if area < min_area or area > max_area:
            continue
        if bh < 0.36 * ss or bh > 1.50 * ss:
            continue
        aspect = float(bw) / max(1.0, float(bh))
        if aspect < 0.68 or aspect > 3.4:
            continue

        if aspect > 2.1 and bw > 1.80 * ss:
            heads.append({
                "cx": x0 + bx + bw * 0.27,
                "cy": y_lim_top + cy,
                "bx": x0 + bx,
                "by": y_lim_top + by,
                "bw": int(bw * 0.5),
                "bh": bh,
                "hollow": False,
            })
            heads.append({
                "cx": x0 + bx + bw * 0.73,
                "cy": y_lim_top + cy,
                "bx": x0 + bx + int(bw * 0.5),
                "by": y_lim_top + by,
                "bw": int(bw * 0.5),
                "bh": bh,
                "hollow": False,
            })
        else:
            heads.append({
                "cx": x0 + float(cx),
                "cy": y_lim_top + float(cy),
                "bx": x0 + int(bx),
                "by": y_lim_top + int(by),
                "bw": int(bw),
                "bh": int(bh),
                "hollow": False,
            })

    # 2. İçi boş nota başları (yalnızca sıkı porte kuşağında: y0 - 1.5*ss .. y4 + 1.2*ss)
    hol_top = max(0, int(round(y0 - 1.5 * ss)))
    hol_bot = min(h, int(round(y4 + 1.2 * ss)))
    hol_sub = no_staff[hol_top:hol_bot, x0:x1].copy()
    if hol_sub.size > 0:
        closed_sub = cv2.morphologyEx(hol_sub, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
        ff = cv2.bitwise_not(closed_sub)
        sh, sw = ff.shape
        mask = np.zeros((sh + 2, sw + 2), np.uint8)
        for sx in range(sw):
            if ff[0, sx] == 255:
                cv2.floodFill(ff, mask, (sx, 0), 0)
            if ff[sh - 1, sx] == 255:
                cv2.floodFill(ff, mask, (sx, sh - 1), 0)
        for sy in range(sh):
            if ff[sy, 0] == 255:
                cv2.floodFill(ff, mask, (0, sy), 0)
            if ff[sy, sw - 1] == 255:
                cv2.floodFill(ff, mask, (sw - 1, sy), 0)

        h_num, _, h_stats, h_cent = cv2.connectedComponentsWithStats(ff)
        for lbl in range(1, h_num):
            hx, hy, hw, hh, harea = h_stats[lbl]
            hcx, hcy = h_cent[lbl]
            if not (0.14 * ss * ss <= harea <= 0.85 * ss * ss):
                continue
            if not (0.42 * ss <= hw <= 1.30 * ss and 0.30 * ss <= hh <= 0.95 * ss and hw >= hh):
                continue
            abs_cx = x0 + float(hcx)
            abs_cy = hol_top + float(hcy)
            if any(abs(hd["cx"] - abs_cx) < 0.80 * ss and abs(hd["cy"] - abs_cy) < 0.80 * ss for hd in heads):
                continue
            rx0 = max(0, int(hx - 0.35 * ss))
            rx1 = min(sw, int(hx + hw + 0.35 * ss))
            ry0 = max(0, int(hy - 0.35 * ss))
            ry1 = min(sh, int(hy + hh + 0.35 * ss))
            ring_ink = (closed_sub[ry0:ry1, rx0:rx1] > 0).mean()
            if 0.28 <= ring_ink <= 0.75:
                heads.append({
                    "cx": abs_cx,
                    "cy": abs_cy,
                    "bx": x0 + rx0,
                    "by": hol_top + ry0,
                    "bw": rx1 - rx0,
                    "bh": ry1 - ry0,
                    "hollow": True,
                })

    heads.sort(key=lambda d: d["cx"])
    dedup: list[dict] = []
    for hd in heads:
        if dedup and abs(hd["cx"] - dedup[-1]["cx"]) < 0.62 * ss and abs(hd["cy"] - dedup[-1]["cy"]) < 0.55 * ss:
            continue
        dedup.append(hd)
    return dedup


def _classify_stem_and_beams(no_staff: np.ndarray, hd: dict, ss: float) -> tuple[str, str | None]:
    h, w = no_staff.shape
    bx, _, bw, _ = hd["bx"], hd["by"], hd["bw"], hd["bh"]
    _, cy = hd["cx"], hd["cy"]

    up_y0 = max(0, int(round(cy - 3.4 * ss)))
    up_y1 = max(0, int(round(cy - 0.45 * ss)))
    dn_y0 = min(h, int(round(cy + 0.45 * ss)))
    dn_y1 = min(h, int(round(cy + 3.4 * ss)))

    sx_l = max(0, bx - 3)
    sx_r = min(w, bx + bw + 4)

    up_patch = no_staff[up_y0:up_y1, sx_l:sx_r]
    dn_patch = no_staff[dn_y0:dn_y1, sx_l:sx_r]

    up_max_col = int((up_patch > 0).sum(axis=0).max()) if up_patch.size > 0 else 0
    dn_max_col = int((dn_patch > 0).sum(axis=0).max()) if dn_patch.size > 0 else 0

    min_stem_len = int(round(1.35 * ss))
    has_up = up_max_col >= min_stem_len
    has_dn = dn_max_col >= min_stem_len

    if hd["hollow"]:
        if has_up or has_dn:
            return "half", ("up" if up_max_col >= dn_max_col else "down")
        return "whole", None

    if not has_up and not has_dn:
        return "quarter", None

    stem_dir = "up" if up_max_col >= dn_max_col else "down"
    if stem_dir == "up":
        stem_x = sx_l + int(np.argmax((up_patch > 0).sum(axis=0)))
        probe_x_r = min(w - 1, int(round(stem_x + 0.55 * ss)))
        probe_x_l = max(0, int(round(stem_x - 0.55 * ss)))
        y_top_stem = up_y0
        for yy in range(up_y0, up_y1):
            if no_staff[yy, stem_x] > 0:
                y_top_stem = yy
                break
        band_y0 = y_top_stem
        band_y1 = min(up_y1, int(round(y_top_stem + 1.85 * ss)))
    else:
        stem_x = sx_l + int(np.argmax((dn_patch > 0).sum(axis=0)))
        probe_x_r = min(w - 1, int(round(stem_x + 0.55 * ss)))
        probe_x_l = max(0, int(round(stem_x - 0.55 * ss)))
        y_bot_stem = dn_y1 - 1
        for yy in range(dn_y1 - 1, dn_y0 - 1, -1):
            if no_staff[yy, stem_x] > 0:
                y_bot_stem = yy
                break
        band_y0 = max(dn_y0, int(round(y_bot_stem - 1.85 * ss)))
        band_y1 = y_bot_stem + 1

    def _count_runs_1d(arr: np.ndarray) -> int:
        if arr.size == 0:
            return 0
        active = (arr > 0).astype(np.uint8)
        runs = 0
        in_r = False
        r_len = 0
        for v in active:
            if v:
                in_r = True
                r_len += 1
            else:
                if in_r and r_len >= max(2, int(0.16 * ss)):
                    runs += 1
                in_r = False
                r_len = 0
        if in_r and r_len >= max(2, int(0.16 * ss)):
            runs += 1
        return runs

    beam_count = max(
        _count_runs_1d(no_staff[band_y0:band_y1, probe_x_r]),
        _count_runs_1d(no_staff[band_y0:band_y1, probe_x_l]),
    )
    if beam_count == 0:
        return "quarter", stem_dir
    if beam_count == 1:
        return "eighth", stem_dir
    if beam_count == 2:
        return "16th", stem_dir
    return "32nd", stem_dir


def _has_augmentation_dot(no_staff: np.ndarray, hd: dict, next_cx: float | None, ss: float) -> bool:
    h, w = no_staff.shape
    dx0 = min(w, int(round(hd["bx"] + hd["bw"] + 0.18 * ss)))
    dx1 = min(w, int(round(hd["bx"] + hd["bw"] + 1.15 * ss)))
    if next_cx is not None:
        dx1 = min(dx1, int(round(next_cx - 0.65 * ss)))
    if dx1 <= dx0 + 2:
        return False
    dy0 = max(0, int(round(hd["cy"] - 0.65 * ss)))
    dy1 = min(h, int(round(hd["cy"] + 0.65 * ss)))
    roi = no_staff[dy0:dy1, dx0:dx1]
    if roi.size == 0:
        return False
    nl, _, st, _ = cv2.connectedComponentsWithStats(roi)
    for i in range(1, nl):
        _, _, bw, bh, area = st[i]
        if 0.025 * ss * ss <= area <= 0.30 * ss * ss and bw <= 0.65 * ss and bh <= 0.65 * ss:
            return True
    return False


def _detect_pre_note_accidental(
    no_staff: np.ndarray,
    hd: dict,
    prev_bx_end: int,
    ss: float,
) -> int | None:
    h, _ = no_staff.shape
    ax1 = max(0, int(round(hd["bx"] - 0.15 * ss)))
    ax0 = max(prev_bx_end + int(round(0.25 * ss)), int(round(hd["bx"] - 1.85 * ss)))
    if ax1 <= ax0 + int(0.35 * ss):
        return None
    ay0 = max(0, int(round(hd["cy"] - 1.35 * ss)))
    ay1 = min(h, int(round(hd["cy"] + 1.35 * ss)))
    roi = no_staff[ay0:ay1, ax0:ax1]
    if roi.size == 0:
        return None

    nl, _, st, _ = cv2.connectedComponentsWithStats(roi)
    for i in range(1, nl):
        rx, ry, rw, rh, area = st[i]
        if rh < 1.15 * ss or rw < 0.28 * ss or rw > 1.35 * ss:
            continue
        if area < 0.18 * ss * ss:
            continue
        comp = roi[ry : ry + rh, rx : rx + rw]
        upper_ink = (comp[: rh // 2, :] > 0).sum()
        lower_ink = (comp[rh // 2 :, :] > 0).sum()
        if rw >= 0.72 * ss and abs(int(upper_ink) - int(lower_ink)) < 0.25 * area:
            return 4
        if lower_ink > upper_ink * 1.25:
            return -4
        return 0
    return None


def _detect_rests_in_gaps(
    no_staff: np.ndarray,
    heads: list[dict],
    x0: int,
    x1: int,
    staff_ys: list[int],
    ss: float,
) -> list[tuple[float, str]]:
    rests: list[tuple[float, str]] = []
    y1, y3 = staff_ys[1], staff_ys[3]
    ry0 = max(0, int(round(y1 - 0.5 * ss)))
    ry1 = min(no_staff.shape[0], int(round(y3 + 0.5 * ss)))

    intervals: list[tuple[int, int]] = []
    cur_x = x0 + int(round(0.5 * ss))
    for hd in heads:
        gx1 = int(round(hd["bx"] - 0.9 * ss))
        if gx1 - cur_x >= int(round(1.8 * ss)):
            intervals.append((cur_x, gx1))
        cur_x = int(round(hd["bx"] + hd["bw"] + 0.9 * ss))
    if (x1 - int(round(0.4 * ss))) - cur_x >= int(round(1.6 * ss)):
        intervals.append((cur_x, x1 - int(round(0.4 * ss))))

    for gx0, gx1 in intervals:
        roi = no_staff[ry0:ry1, gx0:gx1]
        if roi.size == 0:
            continue
        nl, _, st, cent = cv2.connectedComponentsWithStats(roi)
        for i in range(1, nl):
            _, _, rw, rh, area = st[i]
            rcx, _ = cent[i]
            if area < 0.16 * ss * ss or area > 1.8 * ss * ss:
                continue
            if 0.85 * ss <= rh <= 2.9 * ss and 0.30 * ss <= rw <= 1.35 * ss:
                r_dur = "quarter" if rh >= 1.75 * ss else "eighth"
                rests.append((gx0 + float(rcx), r_dur))
    return rests


def _recognize_system_measures(
    crop: StaffSystemCrop,
    is_first_system: bool,
    start_measure_num: int,
) -> tuple[list[MeasureSpec], dict[str, int]]:
    ss = crop.staff_space
    staff_ys = crop.staff_lines_y
    y4 = float(staff_ys[4])
    _, w = crop.crop_bin.shape

    line_thick = _estimate_staff_line_thickness(crop.crop_bin, staff_ys)
    no_staff = _remove_staff_lines(crop.crop_bin, staff_ys, line_thick)
    bars = _detect_barlines(crop.crop_bin, no_staff, staff_ys, ss)

    staff_x_left = 0
    xs_row = np.where(crop.crop_bin[staff_ys[2], :] > 0)[0]
    if len(xs_row) > 0:
        staff_x_left = int(xs_row[0])

    detected_ks = _detect_system1_keysig(no_staff, staff_ys, staff_x_left, ss) if is_first_system else {}

    header_skip = staff_x_left + int(round((8.5 if is_first_system else 5.8) * ss))
    interior_bars = [b for b in bars if b[0] > header_skip + int(round(5.0 * ss))]

    spans: list[tuple[int, int]] = []
    cur_left = header_skip
    for b_start, b_end in interior_bars:
        if b_start - cur_left >= int(round(6.5 * ss)):
            spans.append((cur_left, b_start))
            cur_left = b_end + 1

    right_edge = int(xs_row[-1]) if len(xs_row) > 0 else w - 1
    if right_edge - cur_left >= int(round(5.5 * ss)):
        spans.append((cur_left, right_edge))

    if not spans:
        spans = [(header_skip, max(header_skip + 10, right_edge))]

    measures: list[MeasureSpec] = []
    m_num = start_measure_num

    for sp_i, (mx0, mx1) in enumerate(spans):
        heads = _detect_noteheads_in_measure(no_staff, mx0, mx1, staff_ys, ss)
        rests = _detect_rests_in_gaps(no_staff, heads, mx0, mx1, staff_ys, ss)

        timed_items: list[tuple[float, NoteEvent]] = []
        prev_end = mx0
        for idx_h, hd in enumerate(heads):
            next_cx = heads[idx_h + 1]["cx"] if idx_h + 1 < len(heads) else None
            base_dur, stem_dir = _classify_stem_and_beams(no_staff, hd, ss)
            has_dot = _has_augmentation_dot(no_staff, hd, next_cx, ss)
            dur_str = base_dur + ("." if has_dot and base_dur != "32nd" else "")
            step, octave = _y_to_treble_step_octave(hd["cy"], y4, ss)
            acc_c = _detect_pre_note_accidental(no_staff, hd, prev_end, ss)
            prev_end = hd["bx"] + hd["bw"]

            ev = NoteEvent(
                kind="note",
                step=step,
                octave=octave,
                duration=dur_str,
                accidental_commas=acc_c,
                stem_dir=stem_dir,
            )
            timed_items.append((hd["cx"], ev))

        for rx, r_dur in rests:
            timed_items.append((rx, NoteEvent(kind="rest", duration=r_dur)))

        timed_items.sort(key=lambda t: t[0])
        evs = [t[1] for t in timed_items]
        if not evs:
            continue

        tot_frac = sum((duration_to_fraction(e.duration) for e in evs), Fraction(0, 1))
        if tot_frac <= 0:
            continue

        den = tot_frac.denominator
        num = tot_frac.numerator
        if den < 4:
            mult = 4 // den
            num *= mult
            den = 4
        actual_len_str = f"{num}/{den}"

        mspec = MeasureSpec(
            number=m_num,
            actual_len=actual_len_str,
            line_break=(sp_i == len(spans) - 1),
            events=evs,
        )
        auto_beam_measure_events(mspec)
        measures.append(mspec)
        m_num += 1

    return measures, detected_ks


def recognize_score_opencv(
    input_path: str | Path,
    dpi: int = 350,
    progress_cb: Callable[[int, str], None] | None = None,
) -> ScoreDoc:
    """Çevrimdışı OpenCV OMR motoruyla raster PDF veya görüntü notasını tarar ve ScoreDoc üretir."""
    in_p = Path(input_path)
    if progress_cb:
        progress_cb(10, "Sayfalar yüksek çözünürlükte taranıyor...")

    pages = render_input_pages(in_p, dpi=dpi)
    _, crops = detect_and_slice_systems(pages)

    if not crops:
        raise RuntimeError("Görüntüde 5 çizgili nota portesi tespit edilemedi.")

    all_measures: list[MeasureSpec] = []
    detected_ks: dict[str, int] = {}
    next_m_num = 1
    total_sys = len(crops)

    for idx, crop in enumerate(crops):
        if progress_cb:
            pct = 15 + int(75 * (idx + 1) / max(1, total_sys))
            progress_cb(pct, f"Porte satırı çözümleniyor ({idx + 1}/{total_sys})...")
        sys_measures, ks_first = _recognize_system_measures(
            crop, is_first_system=(idx == 0), start_measure_num=next_m_num
        )
        if idx == 0 and ks_first:
            detected_ks = ks_first
        all_measures.extend(sys_measures)
        next_m_num += len(sys_measures)

    if all_measures:
        lens = [m.actual_len for m in all_measures]
        dom_len, _ = Counter(lens).most_common(1)[0]
        dn, dd = [int(x) for x in dom_len.split("/")]
        all_measures[0].nominal_ts = [dn, dd]
        all_measures[-1].barline = "end"
        all_measures[-1].line_break = False

    clean_title = in_p.stem.replace("_", " ").strip() or "Nota"
    doc = ScoreDoc(
        title=clean_title,
        subtitle="",
        composer="",
        keysig=detected_ks,
        measures=all_measures,
    )
    if progress_cb:
        progress_cb(95, f"Toplam {len(all_measures)} ölçü başarıyla çözümlendi.")
    return doc
