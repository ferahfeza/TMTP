"""
Nota Sayfası Görüntüleme ve 5 Çizgili Porte (Staff System) Dilimleyici.
PDF ve raster nota görüntülerini yüksek çözünürlükte (300-400 DPI) okur,
morfolojik yatay çizgi tespitiyle porteleri bulur ve her satırı ayrı kırpar.
"""

from dataclasses import dataclass
from pathlib import Path

import cv2
import fitz  # PyMuPDF
import numpy as np
from PIL import Image


@dataclass
class PageRender:
    page_index: int
    dpi: int
    rgb_image: np.ndarray
    gray_image: np.ndarray
    binary_inv: np.ndarray  # Mürekkep = 255, Arka plan = 0


@dataclass
class StaffSystemCrop:
    page_index: int
    system_index: int          # Belge genelinde 1-tabanlı porte satırı numarası
    y_top: int                 # Sayfa üzerindeki üst y koordinatı
    y_bottom: int              # Sayfa üzerindeki alt y koordinatı
    x_left: int
    x_right: int
    staff_lines_y: list[int]   # Kırpılmış görüntü içindeki 5 çizginin y merkezleri (y0..y4)
    staff_space: float         # İki çizgi arası ortalama piksel mesafesi
    crop_rgb: np.ndarray
    crop_bin: np.ndarray       # Mürekkep = 255, Arka plan = 0


def _binarize_page(gray: np.ndarray) -> np.ndarray:
    """Gri tonlamalı nota sayfasını gürültüsüz ters ikili (mürekkep=255) görüntüye çevirir."""
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, otsu = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    adapt = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 15
    )
    return cv2.bitwise_and(adapt, cv2.dilate(otsu, np.ones((3, 3), np.uint8), iterations=1))


def render_input_pages(input_path: str | Path, dpi: int = 350) -> list[PageRender]:
    """PDF veya görüntü dosyasını sayfa listesi olarak yüksek DPI'da belleğe alır."""
    p = Path(input_path)
    ext = p.suffix.lower()
    pages: list[PageRender] = []

    if ext == ".pdf":
        doc = fitz.open(str(p))
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        for idx, page in enumerate(doc):
            pix = page.get_pixmap(matrix=mat, alpha=False)
            rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).copy()
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            bin_inv = _binarize_page(gray)
            pages.append(PageRender(page_index=idx, dpi=dpi, rgb_image=rgb, gray_image=gray, binary_inv=bin_inv))
        doc.close()
        return pages

    # Raster görüntü (.png, .jpg, .jpeg, .tif, .bmp, .webp)
    pil_img = Image.open(str(p)).convert("RGB")
    rgb = np.array(pil_img, dtype=np.uint8)
    h, w = rgb.shape[:2]
    eff_dpi = dpi
    if w < 1800:
        scale = 2400.0 / max(w, 1)
        rgb = cv2.resize(rgb, (int(round(w * scale)), int(round(h * scale))), interpolation=cv2.INTER_CUBIC)
        eff_dpi = int(round(dpi * scale))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    bin_inv = _binarize_page(gray)
    pages.append(PageRender(page_index=0, dpi=eff_dpi, rgb_image=rgb, gray_image=gray, binary_inv=bin_inv))
    return pages


def _find_horizontal_line_centers(bin_inv: np.ndarray) -> list[tuple[int, int, int]]:
    """
    Uzun yatay çizgileri morfolojik açma ile izole eder ve her çizgi için
    (y_center, x_min, x_max) listesi döndürür.
    """
    h, w = bin_inv.shape
    horiz_len = max(50, w // 10)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (horiz_len, 1))
    horiz = cv2.morphologyEx(bin_inv, cv2.MORPH_OPEN, kernel)

    row_sums = (horiz > 0).sum(axis=1)
    min_span = max(80, int(w * 0.35))
    active_rows = np.where(row_sums >= min_span)[0]
    if len(active_rows) == 0:
        return []

    clusters: list[list[int]] = []
    cur = [int(active_rows[0])]
    for r in active_rows[1:]:
        if int(r) <= cur[-1] + 2:
            cur.append(int(r))
        else:
            clusters.append(cur)
            cur = [int(r)]
    clusters.append(cur)

    lines: list[tuple[int, int, int]] = []
    for cl in clusters:
        weights = np.array([row_sums[r] for r in cl], dtype=np.float64)
        yc = int(round(np.average(cl, weights=weights)))
        band = horiz[max(0, cl[0]) : min(h, cl[-1] + 1), :]
        xs = np.where(band.max(axis=0) > 0)[0]
        if len(xs) >= min_span:
            lines.append((yc, int(xs[0]), int(xs[-1])))
    return lines


def _group_into_5line_staves(lines: list[tuple[int, int, int]]) -> list[tuple[list[int], int, int, float]]:
    """
    Yatay çizgi adaylarını eşit aralıklı 5'li porte gruplarına ayırır.
    Döndürülen her eleman: ([y0,y1,y2,y3,y4], x_left, x_right, staff_space).
    """
    n = len(lines)
    if n < 5:
        return []

    staves: list[tuple[list[int], int, int, float]] = []
    i = 0
    while i <= n - 5:
        sub = lines[i : i + 5]
        ys = [t[0] for t in sub]
        diffs = np.diff(ys)
        med_d = float(np.median(diffs))
        if 5.0 <= med_d <= 80.0 and np.max(np.abs(diffs - med_d)) <= max(3.5, med_d * 0.25):
            xl = int(np.median([t[1] for t in sub]))
            xr = int(np.median([t[2] for t in sub]))
            staves.append((ys, xl, xr, float(np.mean(diffs))))
            i += 5
        else:
            i += 1
    return staves


def detect_and_slice_systems(pages: list[PageRender]) -> tuple[np.ndarray | None, list[StaffSystemCrop]]:
    """
    Sayfalardaki başlık bölgesini (header_rgb) ve tüm 5-çizgili porte satırlarını (StaffSystemCrop) döndürür.
    """
    header_rgb: np.ndarray | None = None
    all_crops: list[StaffSystemCrop] = []
    sys_counter = 0

    for page in pages:
        h, w = page.binary_inv.shape
        lines = _find_horizontal_line_centers(page.binary_inv)
        staves = _group_into_5line_staves(lines)

        if page.page_index == 0 and staves:
            first_y0 = staves[0][0][0]
            first_ss = staves[0][3]
            hdr_bottom = max(40, int(round(first_y0 - 2.2 * first_ss)))
            header_rgb = page.rgb_image[0:hdr_bottom, :].copy()

        for idx, (ys, xl, xr, ss) in enumerate(staves):
            sys_counter += 1
            pad_top = int(round(3.8 * ss))
            pad_bot = int(round(3.8 * ss))

            y_top = max(0, ys[0] - pad_top)
            y_bot = min(h, ys[4] + pad_bot)

            if idx > 0:
                prev_ys = staves[idx - 1][0]
                mid_prev = (prev_ys[4] + ys[0]) // 2
                y_top = max(y_top, mid_prev - int(round(0.6 * ss)))
            if idx < len(staves) - 1:
                next_ys = staves[idx + 1][0]
                mid_next = (ys[4] + next_ys[0]) // 2
                y_bot = min(y_bot, mid_next + int(round(0.6 * ss)))

            pad_x = int(round(2.5 * ss))
            x_l = max(0, xl - pad_x * 3)  # Sol kenardaki "1. Hâne" / "Teslim" yazılarını da kapsa
            x_r = min(w, xr + pad_x)

            crop_rgb = page.rgb_image[y_top:y_bot, x_l:x_r].copy()
            crop_bin = page.binary_inv[y_top:y_bot, x_l:x_r].copy()
            rel_ys = [y - y_top for y in ys]

            all_crops.append(
                StaffSystemCrop(
                    page_index=page.page_index,
                    system_index=sys_counter,
                    y_top=y_top,
                    y_bottom=y_bot,
                    x_left=x_l,
                    x_right=x_r,
                    staff_lines_y=rel_ys,
                    staff_space=ss,
                    crop_rgb=crop_rgb,
                    crop_bin=crop_bin,
                )
            )

    return header_rgb, all_crops


def save_sliced_crops(input_path: str | Path, out_dir: str | Path, dpi: int = 350) -> list[Path]:
    """Tanılama veya dış inceleme için dilimlenmiş porte satırlarını PNG olarak kaydeder."""
    odir = Path(out_dir)
    odir.mkdir(parents=True, exist_ok=True)
    pages = render_input_pages(input_path, dpi=dpi)
    header_rgb, crops = detect_and_slice_systems(pages)
    saved: list[Path] = []
    if header_rgb is not None:
        hp = odir / "header.png"
        Image.fromarray(header_rgb).save(hp)
        saved.append(hp)
    for c in crops:
        cp = odir / f"system_{c.system_index:02d}_p{c.page_index + 1}.png"
        Image.fromarray(c.crop_rgb).save(cp)
        saved.append(cp)
    return saved
