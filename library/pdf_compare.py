"""
pdf_compare.py - Master vs Child PDF comparison for packaging artwork (pharma).

Every difference found in the CHILD pdf (relative to the MASTER) is highlighted
on the child, numbered, and listed on a summary page appended to the output.

Detected differences
--------------------
Text       : changed / misspelt / extra words, character level detail, case,
             punctuation, numbers (any text change - nothing is auto-ignored)
Missing    : words present in master but absent in child (marker at the gap)
Format     : font family, font variant, bold, italic, font size
Colour     : text colour (RGB / CMYK / Gray, incl. colour-space changes)
Layout     : position / alignment / line spacing, word spacing,
             character spacing (tracking / horizontal scaling), baseline shift
Graphic    : anything visual that is not text-detectable: images, barcodes,
             lines, boxes, fills, colour builds, outlined text, underline, etc.
Page       : page size mismatch, extra / missing pages

Dependencies (all permissive licences - safe for commercial use):
    pip install pdfplumber pypdfium2 pypdf reportlab opencv-python-headless numpy

Usage (CLI):
    python pdf_compare.py master.pdf child.pdf -o child_highlighted.pdf --report diffs.json

Usage (Python):
    from pdf_compare import compare_pdfs
    result = compare_pdfs("master.pdf", "child.pdf", "child_highlighted.pdf")
    result.pdf_bytes        # highlighted child pdf (bytes) -> return to end user
    result.differences      # list[Difference]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple, Union
from xml.sax.saxutils import escape

import cv2
import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors as rl_colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

PdfSource = Union[str, bytes, io.IOBase]
Box = Tuple[float, float, float, float]  # x0, top, x1, bottom  (points, origin top-left)


# --------------------------------------------------------------------------- #
# Options / result objects
# --------------------------------------------------------------------------- #
@dataclass
class CompareOptions:
    # tolerances (points unless stated). Set to 0 for a zero-tolerance proof.
    pos_tol: float = 1.0          # position / alignment / line spacing shift
    spacing_tol: float = 1.0      # gap between words
    width_tol: float = 0.6        # word width (tracking / horizontal scale)
    size_tol: float = 0.1         # font size
    color_tol: float = 0.03       # colour component (0-1)
    strict_color_space: bool = True  # flag K-only black vs RGB black etc.
    # word extraction
    x_tolerance: float = 3.0
    y_tolerance: float = 3.0
    min_move_len: int = 4         # min word length to be treated as "moved" text
    # visual (pixel) comparison for non-text artwork
    detect_graphics: bool = True
    dpi: int = 150
    pixel_threshold: int = 40     # 0-255 colour change that counts as different
    pixel_tolerance_px: int = 1   # anti-aliasing / sub-pixel tolerance
    min_graphic_area_pt2: float = 4.0
    merge_distance_pt: float = 4.0
    add_summary_page: bool = True


@dataclass
class Difference:
    id: int
    page: int                     # 1-based child page, 0 = document level
    category: str                 # Text / Missing / Format / Colour / Layout / Graphic / Page
    type: str
    description: str
    bbox: Optional[Box] = None    # child coordinates, points, origin top-left


@dataclass
class ComparisonResult:
    differences: List[Difference]
    pdf_bytes: bytes
    output_path: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not self.differences

    def summary(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for d in self.differences:
            out[d.category] = out.get(d.category, 0) + 1
        return out


# category -> RGB colour (0-1)
CAT_COLOR = {
    "Text": (0.90, 0.10, 0.10),
    "Missing": (0.55, 0.15, 0.80),
    "Format": (1.00, 0.55, 0.00),
    "Colour": (0.95, 0.10, 0.65),
    "Layout": (0.10, 0.40, 0.95),
    "Graphic": (0.00, 0.65, 0.25),
    "Page": (0.10, 0.10, 0.10),
}
CAT_MEANING = {
    "Text": "Changed / misspelt / extra text",
    "Missing": "Text missing in child (marker at gap)",
    "Format": "Font, size, bold, italic",
    "Colour": "Text colour",
    "Layout": "Position, alignment, spacing",
    "Graphic": "Image / artwork / line / fill difference",
    "Page": "Page size / page count",
}
TYPE_CAT = {
    "Text changed": "Text", "Extra text": "Text", "Missing text": "Missing",
    "Font": "Format", "Size": "Format", "Bold": "Format", "Italic": "Format",
    "Colour": "Colour", "Position": "Layout", "Spacing": "Layout",
    "Graphic": "Graphic", "Page": "Page",
}


# --------------------------------------------------------------------------- #
# Text / font helpers
# --------------------------------------------------------------------------- #
_LIGS = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl"}


def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    for k, v in _LIGS.items():
        s = s.replace(k, v)
    return s.replace("\u200b", "").replace("\u00ad", "")


def _clean_font(name: Optional[str]) -> str:
    return re.sub(r"^[A-Z]{6}\+", "", name or "Unknown")


def _font_family(name: str) -> str:
    n = re.split(r"[-,]", _clean_font(name))[0]
    return re.sub(r"(?i)(MT|PS)$", "", n).lower()


def _is_bold(name: str) -> bool:
    return bool(re.search(r"(?i)bold|black|heavy|demi|[-,]bd$", _clean_font(name)))


def _is_italic(name: str) -> bool:
    return bool(re.search(r"(?i)italic|oblique|kursiv|[-,]it(alic)?$", _clean_font(name)))


def _norm_color(c) -> tuple:
    if c is None:
        return (0.0,)
    if isinstance(c, (int, float)):
        return (float(c),)
    if isinstance(c, str):
        return (c,)
    try:
        t = tuple(round(float(x), 4) for x in c)
        return t if t else (0.0,)
    except Exception:
        return (str(c),)


def _to_rgb(c: tuple) -> Optional[tuple]:
    if not c or isinstance(c[0], str):
        return None
    if len(c) == 1:
        return (c[0],) * 3
    if len(c) == 3:
        return c
    if len(c) == 4:
        cc, m, y, k = c
        return ((1 - cc) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return None


def _fmt_color(c: tuple) -> str:
    if not c:
        return "default"
    if isinstance(c[0], str):
        return c[0]
    if len(c) == 1:
        return f"Gray {c[0] * 100:.0f}%"
    if len(c) == 3:
        return "RGB(%d,%d,%d)" % tuple(round(v * 255) for v in c)
    if len(c) == 4:
        return "CMYK(%d,%d,%d,%d)" % tuple(round(v * 100) for v in c)
    return str(c)


def _color_diff(a: tuple, b: tuple, o: CompareOptions) -> Optional[str]:
    if a == b:
        return None
    if len(a) == len(b) and not isinstance(a[0], str) and not isinstance(b[0], str):
        if max(abs(x - y) for x, y in zip(a, b)) <= o.color_tol:
            return None
        return f"{_fmt_color(a)} -> {_fmt_color(b)}"
    ra, rb = _to_rgb(a), _to_rgb(b)
    if ra is None or rb is None:
        return f"{_fmt_color(a)} -> {_fmt_color(b)}"
    if max(abs(x - y) for x, y in zip(ra, rb)) > o.color_tol:
        return f"{_fmt_color(a)} -> {_fmt_color(b)}"
    if o.strict_color_space:
        return f"colour space/build differs: {_fmt_color(a)} -> {_fmt_color(b)}"
    return None


def _char_changes(a: str, b: str) -> str:
    out = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "delete":
            out.append(f"missing '{a[i1:i2]}'")
        elif tag == "insert":
            out.append(f"extra '{b[j1:j2]}'")
        elif tag == "replace":
            out.append(f"'{a[i1:i2]}' -> '{b[j1:j2]}'")
    return ", ".join(out[:5])


def _short(s: str, n: int = 140) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


# --------------------------------------------------------------------------- #
# Word model
# --------------------------------------------------------------------------- #
@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    fonts: tuple
    sizes: tuple
    colors: tuple
    upright: bool
    base: float = 0.0   # baseline y (top-down), independent of font size

    @property
    def box(self) -> Box:
        return (self.x0, self.top, self.x1, self.bottom)

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 1.0)


def _uniq(seq):
    return tuple(dict.fromkeys(seq))


def _extract_words(page, o: CompareOptions) -> List[Word]:
    words: List[Word] = []
    raw = page.extract_words(
        x_tolerance=o.x_tolerance, y_tolerance=o.y_tolerance,
        keep_blank_chars=False, use_text_flow=False, return_chars=True,
    )
    for w in raw:
        text = _norm_text(w["text"])
        if not text.strip():
            continue
        ch = w["chars"]
        words.append(Word(
            text=text, x0=w["x0"], x1=w["x1"], top=w["top"], bottom=w["bottom"],
            fonts=_uniq(_clean_font(c.get("fontname")) for c in ch),
            sizes=_uniq(round(float(c.get("size", 0)), 2) for c in ch),
            colors=_uniq(_norm_color(c.get("non_stroking_color")) for c in ch),
            upright=all(c.get("upright", True) for c in ch),
            base=(-float(ch[0]["matrix"][5]) if ch[0].get("matrix") else w["bottom"]),
        ))
    return words


def _union(a: Box, b: Box) -> Box:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _same_line(a: Word, b: Word) -> bool:
    return abs(a.base - b.base) < 0.5 * min(a.height, b.height)


# --------------------------------------------------------------------------- #
# Text comparison for one page
# --------------------------------------------------------------------------- #
@dataclass
class _Item:
    type: str
    c0: int                # first child word index (-1 = n/a)
    c1: int
    box: Box
    detail: str


def _text_diff(mw: List[Word], cw: List[Word], o: CompareOptions) -> Tuple[List[_Item], List[Box]]:
    items: List[_Item] = []
    masks: List[Box] = []
    mt, ct = [w.text for w in mw], [w.text for w in cw]

    # ---- 1. align word streams ------------------------------------------------
    ops = SequenceMatcher(None, mt, ct, autojunk=False).get_opcodes()
    pairs: List[Tuple[int, int]] = []
    unm_m, unm_c = set(), set()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            pairs.extend(zip(range(i1, i2), range(j1, j2)))
        else:
            unm_m.update(range(i1, i2))
            unm_c.update(range(j1, j2))

    # ---- 2. rescue moved text (same word, different place in reading order) --
    by_text: Dict[str, List[int]] = {}
    for i in sorted(unm_m):
        by_text.setdefault(mt[i], []).append(i)
    moved_c = set()
    for j in sorted(unm_c):
        cands = by_text.get(ct[j])
        if len(ct[j]) < o.min_move_len or not cands:
            continue
        best = min(cands, key=lambda i: (mw[i].x0 - cw[j].x0) ** 2 + (mw[i].base - cw[j].base) ** 2)
        cands.remove(best)
        pairs.append((best, j))
        unm_m.discard(best)
        unm_c.discard(j)
        moved_c.add(j)
    pairs.sort(key=lambda p: p[1])

    # ---- 3. text changes / extra / missing -----------------------------------
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        rm = [i for i in range(i1, i2) if i in unm_m]
        rc = [j for j in range(j1, j2) if j in unm_c]
        masks.extend(mw[i].box for i in rm)
        masks.extend(cw[j].box for j in rc)
        if rc:
            mtxt, ctxt = " ".join(mt[i] for i in rm), " ".join(ct[j] for j in rc)
            if rm:
                typ = "Text changed"
                detail = f"Master: '{_short(mtxt)}' -> Child: '{_short(ctxt)}'"
                if SequenceMatcher(None, mtxt, ctxt).ratio() >= 0.5 and len(mtxt) < 80:
                    detail += f"  [{_char_changes(mtxt, ctxt)}]"
            else:
                typ, detail = "Extra text", f"Extra text not in master: '{_short(ctxt)}'"
            for k, j in enumerate(rc):
                items.append(_Item(typ, j, j, cw[j].box, detail if k == 0 else ""))
        elif rm:
            mtxt = " ".join(mt[i] for i in rm)
            # place the marker at the gap in the child
            if j1 > 0:
                ref = cw[j1 - 1]
                box = (ref.x1, ref.top, ref.x1 + 1.5, ref.bottom)
            elif cw:
                ref = cw[0]
                box = (ref.x0 - 1.5, ref.top, ref.x0, ref.bottom)
            else:
                mb = mw[rm[0]].box
                box = (mb[0], mb[1], mb[0] + 1.5, mb[3])
            items.append(_Item("Missing text", -1, -1, box, f"Missing in child: '{_short(mtxt)}'"))

    # ---- 4. formatting / layout of matched words ------------------------------
    prev = None  # (mi, ci, dx, dy)
    for p, (mi, ci) in enumerate(pairs):
        m, c = mw[mi], cw[ci]
        dx, dy = c.x0 - m.x0, c.base - m.base
        adj = (prev is not None and ci == prev[1] + 1 and mi == prev[0] + 1
               and _same_line(mw[prev[0]], m) and _same_line(cw[prev[1]], c)
               and m.upright and c.upright)

        if abs(dx) > o.pos_tol or abs(dy) > o.pos_tol:
            masks.extend([m.box, c.box])   # never double-report moved text as graphics

        # -- style --
        font_flagged = False
        if m.fonts != c.fonts:
            fam_m = tuple(_font_family(f) for f in m.fonts)
            fam_c = tuple(_font_family(f) for f in c.fonts)
            if fam_m != fam_c:
                items.append(_Item("Font", ci, ci, c.box,
                                   f"Font: {', '.join(m.fonts)} -> {', '.join(c.fonts)}"))
            bm, bc = any(map(_is_bold, m.fonts)), any(map(_is_bold, c.fonts))
            if bm != bc:
                items.append(_Item("Bold", ci, ci, c.box,
                                   f"Weight: {'Bold' if bm else 'Not bold'} -> {'Bold' if bc else 'Not bold'}"))
            im, ic = any(map(_is_italic, m.fonts)), any(map(_is_italic, c.fonts))
            if im != ic:
                items.append(_Item("Italic", ci, ci, c.box,
                                   f"Style: {'Italic' if im else 'Not italic'} -> {'Italic' if ic else 'Not italic'}"))
            if fam_m == fam_c and bm == bc and im == ic:
                items.append(_Item("Font", ci, ci, c.box,
                                   f"Font variant: {', '.join(m.fonts)} -> {', '.join(c.fonts)}"))
            font_flagged = True
            masks.extend([m.box, c.box])
        size_flagged = (len(m.sizes) != len(c.sizes)
                        or any(abs(a - b) > o.size_tol for a, b in zip(m.sizes, c.sizes)))
        if size_flagged:
            items.append(_Item("Size", ci, ci, c.box,
                               f"Font size: {'/'.join(map(str, m.sizes))} -> {'/'.join(map(str, c.sizes))} pt"))
            masks.extend([m.box, c.box])
        cd = None
        if m.colors != c.colors:
            if len(m.colors) != len(c.colors):
                cd = f"{', '.join(map(_fmt_color, m.colors))} -> {', '.join(map(_fmt_color, c.colors))}"
            else:
                for a, b in zip(m.colors, c.colors):
                    cd = _color_diff(a, b, o)
                    if cd:
                        break
        if cd:
            items.append(_Item("Colour", ci, ci, c.box, f"Text colour: {cd}"))
            masks.extend([m.box, c.box])
        if (not font_flagged and not size_flagged and m.upright and c.upright
                and abs((c.x1 - c.x0) - (m.x1 - m.x0)) > o.width_tol):
            items.append(_Item("Spacing", ci, ci, c.box,
                               "Character spacing / horizontal scaling changed "
                               f"(word width {m.x1 - m.x0:.1f} -> {c.x1 - c.x0:.1f} pt)"))
            masks.extend([m.box, c.box])

        # -- position / spacing --
        if adj:
            pm, pc = mw[prev[0]], cw[prev[1]]
            gap_m, gap_c = m.x0 - pm.x1, c.x0 - pc.x1
            if abs(gap_c - gap_m) > o.spacing_tol:
                items.append(_Item("Spacing", ci - 1, ci, _union(pc.box, c.box),
                                   f"Word spacing {gap_m:.1f} -> {gap_c:.1f} pt"))
                masks.extend([_union(pm.box, m.box), _union(pc.box, c.box)])
            rel_dy = dy - prev[3]
            if abs(rel_dy) > o.pos_tol:
                items.append(_Item("Position", ci, ci, c.box,
                                   f"Baseline offset changed by {rel_dy:+.1f} pt"))
                masks.extend([m.box, c.box])
        elif m.upright and c.upright and (
                (prev is not None and _same_line(mw[prev[0]], m) and _same_line(cw[prev[1]], c)
                 and abs(dy - prev[3]) <= o.pos_tol)
                or (ci - 1 in unm_c and _same_line(cw[ci - 1], c) and abs(dy) <= o.pos_tol)
                or (mi - 1 in unm_m and _same_line(mw[mi - 1], m) and abs(dy) <= o.pos_tol)):
            pass  # text was edited just before this word on the same line: horizontal reflow only
        elif abs(dx) > o.pos_tol or abs(dy) > o.pos_tol:
            # highlight the whole run of words that moved together
            end_c, box, mbox, q = ci, c.box, m.box, p + 1
            while q < len(pairs):
                nmi, nci = pairs[q]
                if not (nci == end_c + 1 and nmi == pairs[q - 1][0] + 1
                        and _same_line(cw[end_c], cw[nci])
                        and abs((cw[nci].x0 - mw[nmi].x0) - dx) <= o.pos_tol
                        and abs((cw[nci].base - mw[nmi].base) - dy) <= o.pos_tol):
                    break
                box, mbox, end_c = _union(box, cw[nci].box), _union(mbox, mw[nmi].box), nci
                q += 1
            what = "Moved" if ci in moved_c else "Shifted"
            items.append(_Item("Position", ci, end_c, box,
                               f"{what} dx={dx:+.1f}, dy={dy:+.1f} pt "
                               "(position / alignment / line spacing)"))
            masks.extend([mbox, box])
        prev = (mi, ci, dx, dy)
    return items, masks


def _merge_items(items: List[_Item], cw: List[Word]) -> List[Tuple[str, Box, str]]:
    """Merge neighbouring same-type word findings on the same line."""
    out: List[Tuple[str, Box, str]] = []
    single = [it for it in items if it.c0 < 0]
    for it in single:
        out.append((it.type, it.box, it.detail))
    by_type: Dict[str, List[_Item]] = {}
    for it in items:
        if it.c0 >= 0:
            by_type.setdefault(it.type, []).append(it)
    for typ, lst in by_type.items():
        lst.sort(key=lambda i: (i.c0, i.c1))
        cur: Optional[List] = None  # [c1, box, [details]]
        for it in lst:
            if (cur is not None and it.c0 <= cur[0] + 1
                    and abs(it.box[1] - cur[1][1]) < 0.6 * (cur[1][3] - cur[1][1])):
                cur[0], cur[1] = max(cur[0], it.c1), _union(cur[1], it.box)
                if it.detail and it.detail not in cur[2]:
                    cur[2].append(it.detail)
            else:
                if cur:
                    out.append((typ, cur[1], "; ".join(cur[2]) or "(continued)"))
                cur = [it.c1, it.box, [it.detail] if it.detail else []]
        if cur:
            out.append((typ, cur[1], "; ".join(cur[2]) or "(continued)"))
    return out


# --------------------------------------------------------------------------- #
# Visual (pixel) comparison - catches everything that is not extractable text
# --------------------------------------------------------------------------- #
def _render(pdf_page, scale: float) -> np.ndarray:
    img = pdf_page.render(scale=scale).to_pil().convert("RGB")
    return np.asarray(img)


def _violation(a: np.ndarray, b: np.ndarray, ker: np.ndarray) -> np.ndarray:
    """How far each pixel of b lies outside the local (tolerance) range of a."""
    lo = cv2.erode(a, ker).astype(np.int16)
    hi = cv2.dilate(a, ker).astype(np.int16)
    b16 = b.astype(np.int16)
    return np.maximum(lo - b16, b16 - hi).max(axis=2)


def _graphic_diffs(m_img: np.ndarray, c_img: np.ndarray, mask_boxes: List[Box],
                   scale: float, o: CompareOptions) -> List[Box]:
    if m_img.shape != c_img.shape:
        m_img = cv2.resize(m_img, (c_img.shape[1], c_img.shape[0]), interpolation=cv2.INTER_AREA)
    k = 2 * o.pixel_tolerance_px + 1
    ker = np.ones((k, k), np.uint8)
    d = np.maximum(_violation(m_img, c_img, ker), _violation(c_img, m_img, ker))
    mask = (d > o.pixel_threshold).astype(np.uint8) * 255

    pad = 2.0
    H, W = mask.shape
    for x0, t, x1, b in mask_boxes:  # areas already reported as text differences
        xa, ya = max(int((x0 - pad) * scale), 0), max(int((t - pad) * scale), 0)
        xb, yb = min(int((x1 + pad) * scale) + 1, W), min(int((b + pad) * scale) + 1, H)
        mask[ya:yb, xa:xb] = 0
    if not mask.any():
        return []

    r = max(int(o.merge_distance_pt * scale / 2), 1)
    merged = cv2.dilate(mask, np.ones((2 * r + 1, 2 * r + 1), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)
    boxes: List[Box] = []
    for lab in range(1, n):
        x, y, w, h, _a = stats[lab]
        sub = (labels[y:y + h, x:x + w] == lab) & (mask[y:y + h, x:x + w] > 0)
        ys, xs = np.nonzero(sub)
        if len(xs) == 0:
            continue
        bx0, bx1 = (x + xs.min()) / scale, (x + xs.max() + 1) / scale
        by0, by1 = (y + ys.min()) / scale, (y + ys.max() + 1) / scale
        if (bx1 - bx0) * (by1 - by0) >= o.min_graphic_area_pt2:
            boxes.append((float(bx0), float(by0), float(bx1), float(by1)))
    return boxes


# --------------------------------------------------------------------------- #
# Overlay + summary rendering
# --------------------------------------------------------------------------- #
_FONT = "Helvetica"
_FONT_B = "Helvetica-Bold"
_UNICODE_OK = False


def _setup_fonts():
    global _FONT, _FONT_B, _UNICODE_OK
    cands = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
        ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ]
    for reg, bold in cands:
        if os.path.exists(reg) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("PCSans", reg))
                pdfmetrics.registerFont(TTFont("PCSans-Bold", bold))
                _FONT, _FONT_B, _UNICODE_OK = "PCSans", "PCSans-Bold", True
                return
            except Exception:
                pass


def _safe(s: str) -> str:
    return s if _UNICODE_OK else s.encode("cp1252", "replace").decode("cp1252")


def _build_overlay(child_pages, diffs_by_page: Dict[int, List[Difference]]) -> bytes:
    buf = io.BytesIO()
    c = None
    for idx, pg in enumerate(child_pages, start=1):
        mb, cb = pg.mediabox, pg.cropbox
        size = (float(mb.right), float(mb.top))
        if c is None:
            c = canvas.Canvas(buf, pagesize=size)
        else:
            c.setPageSize(size)
        left, top = float(cb.left), float(cb.top)
        labels = []
        for d in diffs_by_page.get(idx, []):
            if d.bbox is None:
                continue
            rgb = CAT_COLOR[d.category]
            x0, t, x1, b = d.bbox
            X0, X1 = left + x0, left + x1
            Y1, Y0 = top - t, top - b  # Y1 = upper edge in PDF space
            if d.category == "Missing":
                c.setStrokeColorRGB(*rgb)
                c.setFillColorRGB(*rgb)
                c.setLineWidth(1.6)
                c.line(X0, Y0 - 1, X0, Y1 + 1)
                p = c.beginPath()  # small caret
                p.moveTo(X0 - 3, Y0 - 5)
                p.lineTo(X0 + 3, Y0 - 5)
                p.lineTo(X0, Y0 - 1)
                p.close()
                c.drawPath(p, fill=1, stroke=0)
            elif d.category == "Page":
                c.setStrokeColorRGB(*rgb)
                c.setLineWidth(4)
                c.rect(X0 + 2, Y0 + 2, X1 - X0 - 4, Y1 - Y0 - 4, fill=0, stroke=1)
            elif d.category == "Graphic":
                c.setStrokeColorRGB(*rgb)
                c.setFillColorRGB(*rgb)
                c.setLineWidth(1.3)
                c.setDash(4, 2)
                c.saveState()
                c.setFillAlpha(0.12)
                c.rect(X0 - 1, Y0 - 1, X1 - X0 + 2, Y1 - Y0 + 2, fill=1, stroke=0)
                c.restoreState()
                c.rect(X0 - 1, Y0 - 1, X1 - X0 + 2, Y1 - Y0 + 2, fill=0, stroke=1)
                c.setDash()
            else:
                c.setFillColorRGB(*rgb)
                c.setStrokeColorRGB(*rgb)
                c.saveState()
                c.setFillAlpha(0.30)
                c.rect(X0 - 1, Y0 - 1, X1 - X0 + 2, Y1 - Y0 + 2, fill=1, stroke=0)
                c.restoreState()
                c.setLineWidth(0.8)
                c.rect(X0 - 1, Y0 - 1, X1 - X0 + 2, Y1 - Y0 + 2, fill=0, stroke=1)
            labels.append((d.id, X0, Y1, rgb, size))
        for did, X0, Y1, rgb, (pw, ph) in labels:  # numbered tags on top
            cx, cy, r = min(max(X0, 6), pw - 6), min(Y1 + 6, ph - 6), 5.2
            c.setFillColorRGB(*rgb)
            c.setStrokeColorRGB(1, 1, 1)
            c.setLineWidth(0.6)
            c.circle(cx, cy, r, fill=1, stroke=1)
            c.setFillColorRGB(1, 1, 1)
            c.setFont(_FONT_B, 5.5 if did < 100 else 4.2)
            c.drawCentredString(cx, cy - 2, str(did))
        c.showPage()
    c.save()
    return buf.getvalue()


def _build_summary(diffs: List[Difference], warnings: List[str], n_master: int, n_child: int) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=24, rightMargin=24,
                            topMargin=24, bottomMargin=24, title="PDF comparison summary")
    st = getSampleStyleSheet()
    base = st["BodyText"]
    base.fontName, base.fontSize, base.leading = _FONT, 8, 10
    h1 = st["Title"]
    h1.fontName = _FONT_B
    story = [Paragraph("Master vs Child - Comparison Summary", h1)]
    counts: Dict[str, int] = {}
    for d in diffs:
        counts[d.category] = counts.get(d.category, 0) + 1
    story.append(Paragraph(
        f"Master pages: {n_master} &nbsp;|&nbsp; Child pages: {n_child} &nbsp;|&nbsp; "
        f"Total differences: <b>{len(diffs)}</b>"
        + ("" if diffs else " &nbsp;<b>- no differences found</b>"), base))
    story.append(Spacer(1, 6))

    legend = [["Colour", "Category", "Meaning", "Count"]]
    ts = [("FONTNAME", (0, 0), (-1, -1), _FONT), ("FONTSIZE", (0, 0), (-1, -1), 8),
          ("GRID", (0, 0), (-1, -1), 0.3, rl_colors.grey),
          ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#E8E8E8"))]
    for i, (cat, rgb) in enumerate(CAT_COLOR.items(), start=1):
        legend.append(["", cat, CAT_MEANING[cat], str(counts.get(cat, 0))])
        ts.append(("BACKGROUND", (0, i), (0, i), rl_colors.Color(*rgb)))
    lt = Table(legend, colWidths=[40, 70, 260, 45])
    lt.setStyle(TableStyle(ts))
    story += [lt, Spacer(1, 8)]
    for w in warnings:
        story.append(Paragraph(f"<b>Note:</b> {escape(_safe(w))}", base))

    rows = [["#", "Page", "Category", "Type", "Description"]]
    for d in diffs:
        rows.append([str(d.id), str(d.page) if d.page else "-", d.category, d.type,
                     Paragraph(escape(_safe(d.description)), base)])
    if len(rows) > 1:
        t = Table(rows, colWidths=[28, 34, 55, 75, 560], repeatRows=1)
        style = [("FONTNAME", (0, 0), (-1, -1), _FONT), ("FONTSIZE", (0, 0), (-1, -1), 8),
                 ("GRID", (0, 0), (-1, -1), 0.3, rl_colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#E8E8E8"))]
        for i, d in enumerate(diffs, start=1):
            style.append(("TEXTCOLOR", (2, i), (2, i), rl_colors.Color(*CAT_COLOR[d.category])))
        t.setStyle(TableStyle(style))
        story.append(t)
    doc.build(story)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _read(src: PdfSource) -> bytes:
    if isinstance(src, (bytes, bytearray)):
        return bytes(src)
    if hasattr(src, "read"):
        return src.read()
    with open(src, "rb") as f:
        return f.read()


def compare_pdfs(master: PdfSource, child: PdfSource, output: Optional[str] = None,
                 options: Optional[CompareOptions] = None) -> ComparisonResult:
    """Compare CHILD against MASTER; return child pdf with all differences highlighted."""
    o = options or CompareOptions()
    _setup_fonts()
    mbytes, cbytes = _read(master), _read(child)
    warnings: List[str] = []
    raw: List[Tuple[int, str, Box, str]] = []  # page, type, bbox, detail
    scale = o.dpi / 72.0

    with pdfplumber.open(io.BytesIO(mbytes)) as mpl, pdfplumber.open(io.BytesIO(cbytes)) as cpl:
        mdoc, cdoc = pdfium.PdfDocument(mbytes), pdfium.PdfDocument(cbytes)
        nm, nc = len(mpl.pages), len(cpl.pages)

        for p in range(nc):
            pno = p + 1
            cpage = cpl.pages[p]
            if p >= nm:
                raw.append((pno, "Page", (0, 0, float(cpage.width), float(cpage.height)),
                            "Extra page - does not exist in master"))
                continue
            mpage = mpl.pages[p]
            if cpage.rotation or mpage.rotation:
                warnings.append(f"Page {pno} has a /Rotate value; highlight positions may be offset.")
            if abs(mpage.width - cpage.width) > 1 or abs(mpage.height - cpage.height) > 1:
                raw.append((pno, "Page", (0, 0, float(cpage.width), float(cpage.height)),
                            f"Page size differs: master {mpage.width:.1f}x{mpage.height:.1f} pt, "
                            f"child {cpage.width:.1f}x{cpage.height:.1f} pt"))
            mw, cw = _extract_words(mpage, o), _extract_words(cpage, o)
            if not mw and not cw:
                warnings.append(f"Page {pno}: no extractable text (outlined/raster artwork) - "
                                "compared visually only.")
            items, masks = _text_diff(mw, cw, o)
            for typ, box, detail in _merge_items(items, cw):
                raw.append((pno, typ, box, detail))
            if o.detect_graphics:
                mi = _render(mdoc[p], scale)
                ci = _render(cdoc[p], scale)
                for box in _graphic_diffs(mi, ci, masks, scale, o):
                    raw.append((pno, "Graphic", box,
                                "Visual difference (image / artwork / colour / line / shape / "
                                "outlined text) not explained by a text change"))
        if nm > nc:
            raw.append((0, "Page", None, f"Master has {nm - nc} more page(s) than child "
                                         f"(master pages {nc + 1}-{nm} missing in child)"))
        mdoc.close()
        cdoc.close()

    # number: by page, then reading order
    raw.sort(key=lambda r: (r[0] == 0, r[0], round(r[2][1] / 4) if r[2] else 0, r[2][0] if r[2] else 0))
    diffs = [Difference(i, pg, TYPE_CAT[typ], typ, detail, box)
             for i, (pg, typ, box, detail) in enumerate(raw, start=1)]
    by_page: Dict[int, List[Difference]] = {}
    for d in diffs:
        by_page.setdefault(d.page, []).append(d)

    # ---- build highlighted child ----
    creader = PdfReader(io.BytesIO(cbytes))
    overlay = PdfReader(io.BytesIO(_build_overlay(list(creader.pages), by_page)))
    writer = PdfWriter()
    for i, page in enumerate(creader.pages):
        page.merge_page(overlay.pages[i])
        writer.add_page(page)
    if o.add_summary_page:
        for sp in PdfReader(io.BytesIO(_build_summary(diffs, warnings, nm, nc))).pages:
            writer.add_page(sp)
    out = io.BytesIO()
    writer.write(out)
    data = out.getvalue()
    if output:
        with open(output, "wb") as f:
            f.write(data)
    return ComparisonResult(diffs, data, output, warnings)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main():
    ap = argparse.ArgumentParser(description="Compare child PDF against master PDF and highlight differences.")
    ap.add_argument("master")
    ap.add_argument("child")
    ap.add_argument("-o", "--output", default="child_highlighted.pdf")
    ap.add_argument("--report", help="write differences to .json or .csv")
    ap.add_argument("--no-graphics", action="store_true", help="skip pixel comparison")
    ap.add_argument("--no-summary", action="store_true", help="do not append summary page")
    ap.add_argument("--pos-tol", type=float, default=1.0)
    ap.add_argument("--dpi", type=int, default=150)
    a = ap.parse_args()
    opts = CompareOptions(pos_tol=a.pos_tol, dpi=a.dpi, detect_graphics=not a.no_graphics,
                          add_summary_page=not a.no_summary)
    res = compare_pdfs(a.master, a.child, a.output, opts)
    print(f"{len(res.differences)} difference(s) {res.summary()} -> {a.output}")
    for w in res.warnings:
        print("note:", w)
    if a.report:
        rows = [asdict(d) for d in res.differences]
        if a.report.lower().endswith(".csv"):
            with open(a.report, "w", newline="", encoding="utf-8") as f:
                wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["id"])
                wr.writeheader()
                wr.writerows(rows)
        else:
            with open(a.report, "w", encoding="utf-8") as f:
                json.dump(rows, f, indent=2)


if __name__ == "__main__":
    _main()
