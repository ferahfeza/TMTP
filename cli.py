"""
TMTP (MakamNotaApp) Komut Satırı Arayüzü (CLI).
Örnek kullanımlar:
  python cli.py list-presets
  python cli.py process --input "nota.mscz" --out-dir "./cikti" --steps 0 --commas 0
  python cli.py process --input "nota.pdf" --out-dir "./cikti" --steps 1 --commas 9 --omr-mode auto
  python cli.py process --input "nota.pdf" --out-dir "./cikti" --from-ahenk 7 --to-ahenk 0
"""

import argparse
import sys
from pathlib import Path

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
)
from makam_app.config import load_settings
from makam_app.pipeline import export_transposed_score, load_score_source


def _format_keysig_summary(ks: dict[str, int]) -> str:
    items = []
    for s in STEPS:
        c = ks.get(s, 0)
        if c != 0:
            info = nearest_aeu_accidental(c)
            items.append(f"{STEP_TR_NAMES[s]} ({s}): {info.tr_name}")
    return ", ".join(items) if items else "İşaretsiz (Doğal / Çargâh-Bûselik)"


def cmd_list_presets() -> int:
    print("\n=== 1. SES / KOMA GÖÇÜRME ÖN-AYARLARI ===")
    for idx, (label, st, cm) in enumerate(INTERVAL_PRESETS):
        print(f"  [{idx:2d}] {label:<46} -> Adım: {st:+d}, Koma: {cm:+d}")

    print("\n=== 2. TÜRK MÜZİĞİ KARAR PERDELERİ (--from-perde / --to-perde) ===")
    for idx, (label, d_idx, c_val) in enumerate(PERDE_LIST):
        print(f"  [{idx:2d}] {label:<38} (Diyatonik={d_idx}, Koma={c_val})")

    print("\n=== 3. NEY AHENKLERİ (--from-ahenk / --to-ahenk) ===")
    for idx, (label, d_idx, c_val) in enumerate(NEY_AHENKLERI):
        print(f"  [{idx:2d}] {label:<42} ({d_idx:+d} adım, {c_val:+d} koma)")

    print("\n=== 4. 53-KOMA AEU STANDART ARIZA TABLOSU ===")
    for k, info in sorted(AEU_ACCIDENTALS.items()):
        print(f"  {k:+3d} koma : {info.tr_name}")
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    cfg = load_settings()
    if args.api_key:
        cfg["gemini_api_key"] = args.api_key

    # Göçürme aralığını belirle
    if args.preset is not None:
        if not (0 <= args.preset < len(INTERVAL_PRESETS)):
            print(f"Hata: Geçersiz --preset indeksi: {args.preset}", file=sys.stderr)
            return 2
        _, steps, commas = INTERVAL_PRESETS[args.preset]
    elif args.from_perde is not None and args.to_perde is not None:
        steps, commas = interval_between_perdeler(args.from_perde, args.to_perde)
    elif args.from_ahenk is not None and args.to_ahenk is not None:
        steps, commas = interval_between_ahenkler(args.from_ahenk, args.to_ahenk)
    else:
        steps = args.steps
        commas = args.commas

    def _progress(pct: int, msg: str) -> None:
        print(f"[{pct:3d}%] {msg}")

    state = load_score_source(
        input_path=args.input,
        omr_mode=args.omr_mode,
        settings=cfg,
        progress_cb=_progress,
    )

    print(f"\nKaynak Donanım : {_format_keysig_summary(state.orig_keysig)}")
    print(f"Göçürme Aralığı: {steps:+d} diyatonik adım, {commas:+d} koma")

    res = export_transposed_score(
        state=state,
        out_dir=args.out_dir,
        base_name=args.name,
        diatonic_steps=steps,
        comma_shift=commas,
        apply_53edo_tuning=not args.no_tuning,
        export_midi=args.midi,
        export_mp3=args.mp3,
        save_json=not args.no_json,
        subtitle_note=args.subtitle_note,
        musescore_exe=cfg.get("musescore_exe"),
        progress_cb=_progress,
    )

    print(f"Yeni Donanım   : {_format_keysig_summary(res.target_keysig)}")
    print(f"Üretilen .mscz : {res.mscz_path}")
    print(f"Üretilen .pdf  : {res.pdf_path}")
    if res.midi_path:
        print(f"Üretilen .mid  : {res.midi_path}")
    if res.mp3_path:
        print(f"Üretilen .mp3  : {res.mp3_path}")
    if res.json_path:
        print(f"Üretilen .json : {res.json_path}")

    if res.validation_issues:
        print(f"\nUyarı: {len(res.validation_issues)} ölçüde usûl süre farkı raporlandı:")
        for iss in res.validation_issues[:10]:
            print(f"  - {iss.message}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="makam-nota",
        description="MakamNotaApp — 53-Koma Arel-Ezgi-Uzdilek Nota Replikası ve Göçürme Aracı",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list-presets", help=" Ön-ayarları, karar perdelerini ve Ney ahenklerini listeler.")

    pr = sub.add_parser("process", help="Verilen notanın replikasını veya transpozesini (.mscz + .pdf) üretir.")
    pr.add_argument("--input", "-i", required=True, help="Girdi dosyası (.pdf, .png, .jpg, .mscz, .mscx, .musicxml, .json)")
    pr.add_argument("--out-dir", "-o", required=True, help="Çıktı klasörü")
    pr.add_argument("--name", "-n", default=None, help="Çıktı dosya kök adı (uzantısız)")
    pr.add_argument("--steps", type=int, default=0, help="Diyatonik adım kaydırma (örn. +1 tam ses yukarı için 1)")
    pr.add_argument("--commas", type=int, default=0, help="53-koma kaydırma (örn. +1 tam ses yukarı için 9)")
    pr.add_argument("--preset", type=int, default=None, help="list-presets içindeki aralık indeksi")
    pr.add_argument("--from-perde", type=int, default=None, help="Kaynak karar perdesi indeksi (0..11)")
    pr.add_argument("--to-perde", type=int, default=None, help="Hedef karar perdesi indeksi (0..11)")
    pr.add_argument("--from-ahenk", type=int, default=None, help="Elimdeki Ney indeksi (list-presets'e bakın)")
    pr.add_argument("--to-ahenk", type=int, default=None, help="İcra edilecek hedef Ney ahengi indeksi")
    pr.add_argument("--omr-mode", choices=["auto", "gemini", "opencv"], default="auto", help="PDF/resim OMR motoru")
    pr.add_argument("--api-key", default=None, help="İsteğe bağlı Gemini API anahtarı")
    pr.add_argument("--subtitle-note", default=None, help="Alt başlığa eklenecek bilgi notu")
    pr.add_argument("--no-tuning", action="store_true", help="53-EDO cent akordunu devre dışı bırak")
    pr.add_argument("--midi", action="store_true", default=True, help="MIDI (.mid) çıktısı üret (varsayılan: açık)")
    pr.add_argument("--no-midi", action="store_false", dest="midi", help="MIDI çıktısını kapat")
    pr.add_argument("--mp3", action="store_true", default=False, help="MP3 ses çıktısı üret")
    pr.add_argument("--no-json", action="store_true", help="Ara JSON kaydını kapat")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "list-presets":
        return cmd_list_presets()
    if args.command == "process":
        return cmd_process(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
