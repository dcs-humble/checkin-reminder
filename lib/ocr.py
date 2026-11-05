"""ocr.py - reusable 'eyes' for this project.

Grabs the screen (or a window rect) and OCRs it with the Windows engine, printing
and saving every recognised line plus the bounding box of every word.

Usage:
    python ocr.py                 # full screen
    python ocr.py --out foo.txt   # custom output
    python ocr.py --region L T R B
    python ocr.py --grep 打卡      # only print lines containing this text

OCR notes for this machine (Windows.Media.Ocr, zh-Hans-CN):
  * CJK is recognised reliably but WITH SPACES inserted between characters, so all
    comparisons must go through norm() which strips whitespace.
  * Latin/digits get mangled occasionally (T1010xx -> TI 010xx, 24:00 -> 24 点 00).
    Treat latin matches as fuzzy; treat CJK as solid.
"""
import argparse
import asyncio
import os
import sys
import ctypes

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "..", "logs", "probe")     # 探针产物统一放这里，别再塞满 lib\

from PIL import ImageGrab  # noqa: E402

import winrt.windows.media.ocr as _ocr          # noqa: E402
import winrt.windows.globalization as _glob     # noqa: E402
import winrt.windows.graphics.imaging as _img   # noqa: E402
import winrt.windows.storage.streams as _st    # noqa: E402


def norm(s: str) -> str:
    """Collapse all whitespace - the OCR engine pads every CJK glyph with spaces."""
    return "".join(s.split())


async def ocr_png_bytes(data: bytes):
    stream = _st.InMemoryRandomAccessStream()
    writer = _st.DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(data)
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await _img.BitmapDecoder.create_async(stream)
    bmp = await decoder.get_software_bitmap_async()
    lang = _glob.Language("zh-Hans-CN")
    engine = _ocr.OcrEngine.try_create_from_language(lang) or _ocr.OcrEngine.try_create_from_user_profile_languages()
    return await engine.recognize_async(bmp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(PROBE, "ocr-last.txt"))
    ap.add_argument("--png", default=os.path.join(PROBE, "ocr-last.png"))
    ap.add_argument("--region", type=int, nargs=4, metavar=("L", "T", "R", "B"))
    ap.add_argument("--grep", default=None)
    ap.add_argument("--from-png", default=None,
                    help="OCR this image instead of grabbing the screen. Use when an "
                         "occluding window makes a desktop grab useless (see pw-capture.py). "
                         "--region is ignored/not added back in this mode.")
    args = ap.parse_args()
    # 默认输出目录可能不存在（尤其是刚清理过 probe）；用户自定的路径也要能写
    for d in {os.path.dirname(os.path.abspath(args.out)),
              os.path.dirname(os.path.abspath(args.png))}:
        if d:
            os.makedirs(d, exist_ok=True)

    ctypes.windll.user32.SetProcessDPIAware()
    if args.from_png:
        # Off-screen path: coordinates are image-relative, so do NOT add a region offset.
        from PIL import Image
        img = Image.open(args.from_png).convert("RGB")
        bbox = None
    else:
        bbox = tuple(args.region) if args.region else None
        img = ImageGrab.grab(bbox=bbox, all_screens=True)
    img.save(args.png)

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    res = asyncio.run(ocr_png_bytes(buf.getvalue()))

    lines = []
    for ln in res.lines:
        x = min(w.bounding_rect.x for w in ln.words)
        y = min(w.bounding_rect.y for w in ln.words)
        lines.append((int(x), int(y), ln.text))

    off = (bbox[0], bbox[1]) if bbox else (0, 0)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(f"# screen {img.size} region={bbox}\n")
        for x, y, t in lines:
            f.write(f"({x + off[0]:5d},{y + off[1]:5d})  {t}\n")

    print(f"screen={img.size} region={bbox} png={args.png} out={args.out}")
    print(f"lines: {len(lines)}")
    for x, y, t in lines:
        if args.grep and norm(args.grep) not in norm(t):
            continue
        print(f"  ({x + off[0]:5d},{y + off[1]:5d})  {t}")

    # word-level boxes, handy for locating click targets
    print("--- words ---")
    for ln in res.lines:
        for w in ln.words:
            if args.grep and norm(args.grep) not in norm(w.text):
                continue
            r = w.bounding_rect
            print(f"  ({int(r.x) + off[0]:5d},{int(r.y) + off[1]:5d}) {int(r.width):4d}x{int(r.height):3d}  {w.text}")


if __name__ == "__main__":
    main()
