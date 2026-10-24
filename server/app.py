#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hardened Production Backend API for Liu Chunlin Small Regular Script (刘春霖小楷书房)
- 100% Font Asset Protection:
  * Fonts are loaded from a private directory (`/opt/liuchunlin_private_fonts` or local `v003`).
  * ZERO static file serving, ZERO vector/SVG/TTF endpoints, OpenAPI docs disabled.
  * Returns ONLY flattened raster PNG images with baked-in Xuan paper texture & Mode B QR Seal footer.
- Strict Rate Limiting:
  * Maximum 2 generations per IP per day (persisted in SQLite `quota.db`).
- Smart Poem Title / Author Parser & Classical Layout Router:
  * Automatically extracts title & author from raw pasted text.
  * Automatically routes 7-char poetry (`10x14`), 5-char poetry (`10x15`), or prose (`篇尾落款`).
- Viral Mode B Museum Silk Mounting Footer:
  * Automatically attaches the Palace-Museum style silk mounting footer with a WeChat-scannable
    Cinnabar Seal QR Code pointing to `https://dvbs2000.github.io/liuchunlin-shufa/`.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import segno
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

# Locate v003 engine & private font directory
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from generate_liuchunlin_copybook import render_copybook_page

PRIVATE_FONT_DIR = Path(os.environ.get("LIUCHUNLIN_FONT_DIR", str(PROJECT_ROOT / "output")))
TRAD_TTF_PATH = PRIVATE_FONT_DIR / "LiuChunlin-ShengJiaoXu-Trad.ttf"
SIMP_TTF_PATH = PRIVATE_FONT_DIR / "LiuChunlin-ShengJiaoXu-Simp.ttf"
VARIANTS_MAP_PATH = PRIVATE_FONT_DIR / "LiuChunlin-Variants-Map.json"
QUOTA_DB_PATH = Path(os.environ.get("LIUCHUNLIN_QUOTA_DB", str(HERE / "ip_quota.sqlite3")))
SITE_URL = os.environ.get("LIUCHUNLIN_SITE_URL", "https://dvbs2000.github.io/liuchunlin-shufa/")
DAILY_IP_LIMIT = int(os.environ.get("LIUCHUNLIN_DAILY_LIMIT", "2"))

# Load PUA variant lookup tables once at startup
with open(VARIANTS_MAP_PATH, "r", encoding="utf-8") as f:
    _VARIANTS_PAYLOAD = json.load(f)["pua_variants"]

_DB_LOCK = threading.Lock()


def _init_quota_db() -> None:
    QUOTA_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(QUOTA_DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ip_daily_quota (
                ip TEXT NOT NULL,
                day TEXT NOT NULL,
                used_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (ip, day)
            )
            """
        )
        conn.commit()


_init_quota_db()


def get_client_ip(request: Request) -> str:
    """Extract real client IP (prioritizing Cloudflare `CF-Connecting-IP`)."""
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    xri = request.headers.get("X-Real-IP")
    if xri:
        return xri.strip()
    return request.client.host if request.client else "unknown"


def check_and_consume_quota(ip: str, consume: bool = True) -> Tuple[bool, int]:
    """
    Check if `ip` has remaining quota today (`DAILY_IP_LIMIT = 2`).
    Returns `(allowed, remaining_after)`.
    """
    today = datetime.date.today().isoformat()
    with _DB_LOCK, sqlite3.connect(str(QUOTA_DB_PATH)) as conn:
        row = conn.execute(
            "SELECT used_count FROM ip_daily_quota WHERE ip = ? AND day = ?",
            (ip, today),
        ).fetchone()
        used = int(row[0]) if row else 0
        if not consume:
            return (used < DAILY_IP_LIMIT), max(0, DAILY_IP_LIMIT - used)
        if used >= DAILY_IP_LIMIT:
            return False, 0
        new_used = used + 1
        conn.execute(
            """
            INSERT INTO ip_daily_quota (ip, day, used_count)
            VALUES (?, ?, ?)
            ON CONFLICT(ip, day) DO UPDATE SET used_count = excluded.used_count
            """,
            (ip, today, new_used),
        )
        conn.commit()
        return True, max(0, DAILY_IP_LIMIT - new_used)


# ---------------------------------------------------------------------------
# Smart Poem Title & Author Parser + Classical Layout Router
# ---------------------------------------------------------------------------
def smart_parse_poem_input(
    raw_text: str, explicit_title: str = "", explicit_author: str = ""
) -> Tuple[str, str, str]:
    """
    Intelligently separate `(title, author, body_text)` from raw user input.
    Handles inputs like:
      - `《春江花月夜》\n【唐】张若虚\n春江潮水连海平...`
      - `春江花月夜 张若虚\n春江潮水连海平...`
      - Pure body text
    """
    title = explicit_title.strip().strip("《》「」『』")
    author = explicit_author.strip()
    lines = [ln.strip() for ln in raw_text.strip().splitlines() if ln.strip()]
    if not lines:
        return title, author, ""

    def _clean_dynasty(s: str) -> str:
        s = re.sub(r"^[\[【（(][^\]】）)]{1,4}[\]】）)]\s*", "", s)
        s = re.sub(r"^(?:作者|撰人|书者)[:：]\s*", "", s)
        s = re.sub(r"^(?:唐|宋|元|明|清|汉|魏|晋|南北朝|隋|近代|现代)[·・\s]+", "", s)
        return s.strip()

    # If user didn't explicitly fill title/author, inspect first 1~2 lines
    if not title and len(lines) >= 2:
        l0 = lines[0]
        # Pattern 1: `《题名》 作者` or `《题名》`
        m_book = re.match(r"^[《「『]([^》」』]{1,12})[》」』]\s*(.*)$", l0)
        if m_book:
            title = m_book.group(1).strip()
            rem = _clean_dynasty(m_book.group(2))
            if rem and len(rem) <= 5:
                author = author or rem
                lines = lines[1:]
            else:
                lines = lines[1:]
                if len(lines) >= 2:
                    cand_a = _clean_dynasty(lines[0])
                    if 1 <= len(cand_a) <= 5 and not re.search(r"[，。！？；：,.!?]", lines[0]):
                        author = author or cand_a
                        lines = lines[1:]
        else:
            # Pattern 2: Short first line without punctuation (`春江花月夜` or `春江花月夜 张若虚`)
            parts = l0.split()
            if (
                len(parts) == 2
                and 1 <= len(parts[0]) <= 8
                and 1 <= len(_clean_dynasty(parts[1])) <= 5
                and not re.search(r"[，。！？；：,.!?]", l0)
            ):
                title = parts[0]
                author = author or _clean_dynasty(parts[1])
                lines = lines[1:]
            elif 1 <= len(l0) <= 8 and not re.search(r"[，。！？；：,.!?]", l0) and len(lines[1]) > len(l0):
                title = l0
                lines = lines[1:]
                if len(lines) >= 2:
                    cand_a = _clean_dynasty(lines[0])
                    if 1 <= len(cand_a) <= 5 and not re.search(r"[，。！？；：,.!?]", lines[0]):
                        author = author or cand_a
                        lines = lines[1:]

    body = "".join(lines)
    return title[:10], author[:6], body


def detect_optimal_rows_per_col(body_text: str, has_title_col: bool, ruling_style: str) -> int:
    """
    Smart Classical Layout Router:
      - If 7-syllable poetry (`七言诗`, clauses of 7 chars): 14 rows/col (exact 1 couplet = 14 chars per col!)
      - If 5-syllable poetry (`五言诗`, clauses of 5 chars): 15 rows/col (exact 3 lines = 15 chars per col!)
      - Otherwise: 14 rows/col when `has_title_col` is True, or standard 15 (`zhalan`) / 13 (`clean`).
    """
    clauses = [
        re.sub(r"\s+", "", seg)
        for seg in re.split(r"[，。！？；：、,.!?;:\n]+", body_text)
        if seg.strip()
    ]
    if len(clauses) >= 4:
        lengths = [len(c) for c in clauses[:8]]
        if sum(1 for L in lengths if L == 7) >= len(lengths) * 0.75:
            return 14
        if sum(1 for L in lengths if L == 5) >= len(lengths) * 0.75:
            return 15
    if has_title_col:
        return 14
    return 15 if ruling_style == "zhalan" else 13


# ---------------------------------------------------------------------------
# Mode B Viral Cinnabar Seal QR Code & Museum Silk Mounting Footer
# ---------------------------------------------------------------------------
def make_cinnabar_qr_seal(url: str, size_px: int = 122, edition: str = "simp") -> Image.Image:
    """Render a WeChat-scannable Cinnabar Seal QR Code (`朱砂印章二维码`)."""
    qr = segno.make(url, error="h", boost_error=False)
    matrix = np.array(qr.matrix, dtype=np.uint8)
    n_mod = matrix.shape[0]
    quiet = 4
    tot_mod = n_mod + 2 * quiet
    padded = np.zeros((tot_mod, tot_mod), dtype=np.uint8)
    padded[quiet : quiet + n_mod, quiet : quiet + n_mod] = matrix

    S = 400
    seal_img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dr = ImageDraw.Draw(seal_img)

    cinnabar = (174, 34, 26, 255)
    ivory = (248, 244, 235, 255)
    dr.rounded_rectangle([2, 2, S - 3, S - 3], radius=10, fill=ivory, outline=cinnabar, width=10)

    inner_pad = 14
    inner_span = S - 2 * inner_pad
    mod_px = inner_span / float(tot_mod)

    for r in range(tot_mod):
        for c in range(tot_mod):
            if padded[r, c]:
                x0 = int(round(inner_pad + c * mod_px))
                y0 = int(round(inner_pad + r * mod_px))
                x1 = int(round(inner_pad + (c + 1) * mod_px))
                y1 = int(round(inner_pad + (r + 1) * mod_px))
                dr.rectangle([x0, y0, x1 - 1, y1 - 1], fill=cinnabar)

    b_half = int(S * 0.082)
    cx, cy = S // 2, S // 2
    dr.rounded_rectangle(
        [cx - b_half, cy - b_half, cx + b_half, cy + b_half],
        radius=6,
        fill=cinnabar,
        outline=ivory,
        width=3,
    )
    ttf_p = TRAD_TTF_PATH if edition == "trad" else SIMP_TTF_PATH
    center_ch = "書" if edition == "trad" else "书"
    f_seal = ImageFont.truetype(str(ttf_p), int(b_half * 1.45))
    bb = dr.textbbox((0, 0), center_ch, font=f_seal)
    dr.text(
        (cx - (bb[0] + bb[2]) / 2, cy - (bb[1] + bb[3]) / 2 - 2),
        center_ch,
        fill=ivory,
        font=f_seal,
    )

    return seal_img.resize((size_px, size_px), Image.Resampling.LANCZOS)


def attach_museum_qr_footer(page_img: Image.Image, edition: str = "simp", site_url: str = SITE_URL) -> Image.Image:
    """
    Attach Mode B Palace-Museum style Silk Mounting Footer (`故宫文创式宋锦装裱底边 + 朱砂印码`).
    """
    rgba_page = page_img.convert("RGBA")
    W, H = rgba_page.size
    footer_h = 148
    out = Image.new("RGBA", (W, H + footer_h), (214, 205, 188, 255))
    out.paste(rgba_page, (0, 0))

    dr = ImageDraw.Draw(out)
    dr.line([(0, H), (W, H)], fill=(168, 142, 112, 255), width=3)
    dr.line([(0, H + 5), (W, H + 5)], fill=(188, 168, 140, 255), width=1)

    qr_seal = make_cinnabar_qr_seal(site_url, size_px=122, edition=edition)
    out.alpha_composite(qr_seal, (W - 128 - 122, H + 13))

    ttf_p = TRAD_TTF_PATH if edition == "trad" else SIMP_TTF_PATH
    f_m1 = ImageFont.truetype(str(ttf_p), 32)
    f_m2 = ImageFont.truetype(str(ttf_p), 24)
    if edition == "trad":
        line1 = "劉春霖小楷書房 · 在線生成您的專屬詩詞書法"
        line2 = "微信長按右側朱砂印章 → 輸入任意詩詞文字，一秒生成宣紙字帖作品"
    else:
        line1 = "刘春霖小楷书房 · 在线生成您的专属诗词书法"
        line2 = "微信长按右侧朱砂印章 → 输入任意诗词文字，一秒生成宣纸字帖作品"

    dr.text((128, H + 34), line1, fill=(55, 45, 38, 255), font=f_m1)
    dr.text((128, H + 80), line2, fill=(148, 38, 30, 255), font=f_m2)
    return out.convert("RGB")


# ---------------------------------------------------------------------------
# FastAPI Application (Zero Static Files, Docs Disabled for Maximum Security)
# ---------------------------------------------------------------------------
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Quota-Remaining", "X-Detected-Title", "X-Detected-Author"],
)


@app.middleware("http")
async def block_font_probing_middleware(request: Request, call_next):
    """Block any attempt to probe or download font/vector/source files."""
    path_lower = request.url.path.lower()
    if any(ext in path_lower for ext in (".ttf", ".otf", ".woff", ".woff2", ".svg", ".py", ".zip", ".db", "..")):
        return Response(status_code=403, content="Forbidden")
    return await call_next(request)


class RenderRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=600)
    title: str = Field(default="", max_length=16)
    author: str = Field(default="", max_length=10)
    colophon: str = Field(default="", max_length=12)
    edition: str = Field(default="simp", pattern="^(trad|simp)$")
    style: str = Field(default="zhalan", pattern="^(zhalan|clean)$")


@app.get("/api/quota")
async def get_quota_status(request: Request):
    ip = get_client_ip(request)
    allowed, rem = check_and_consume_quota(ip, consume=False)
    return {"allowed": allowed, "remaining": rem, "daily_limit": DAILY_IP_LIMIT}


@app.post("/api/render")
async def render_calligraphy_endpoint(req: RenderRequest, request: Request):
    ip = get_client_ip(request)

    # Smart parse title, author, and body text
    title, author, body = smart_parse_poem_input(req.text, req.title, req.author)
    clean_body = [c for c in body if not c.isspace() and c not in "，。、；：？！“”‘’（）《》「」『』—…"]
    if len(clean_body) < 2:
        raise HTTPException(status_code=400, detail="请输入至少 2 个汉字的正文内容。")
    if len(clean_body) > 260:
        raise HTTPException(status_code=400, detail="单幅册页正文建议不超过 260 个汉字。")

    # Enforce strict 2 generations per IP per day quota
    allowed, remaining = check_and_consume_quota(ip, consume=True)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"今日墨池已歇（每 IP 每日最多免费生成 {DAILY_IP_LIMIT} 幅作品），请明日再来雅集！",
        )

    edition = req.edition
    style = req.style
    ttf_path = TRAD_TTF_PATH if edition == "trad" else SIMP_TTF_PATH
    vmap = _VARIANTS_PAYLOAD[edition]

    has_title_col = bool(title)
    rows_per_col = detect_optimal_rows_per_col(body, has_title_col=has_title_col, ruling_style=style)
    cell_pitch_px = 96 if rows_per_col == 14 else (92 if rows_per_col >= 15 else 100)
    col_pitch_px = 126 if style == "zhalan" else 128
    glyph_scale = 1.16 if rows_per_col == 14 else (1.18 if style == "zhalan" else 1.12)

    default_colophon = "" if has_title_col else ("春霖敬書" if edition == "trad" else "刘春霖书")
    colophon = req.colophon.strip() or default_colophon

    page_img = render_copybook_page(
        text=body,
        ttf_path=ttf_path,
        pua_variants=vmap,
        colophon=colophon,
        poem_title=title,
        poem_author=author,
        num_cols=10,
        rows_per_col=rows_per_col,
        cell_pitch_px=cell_pitch_px,
        col_pitch_px=col_pitch_px,
        glyph_scale_in_cell=glyph_scale,
        ruling_style=style,
        seed=2026,
        export_svg_path=None,  # NEVER export SVG in public web service
    )

    final_img = attach_museum_qr_footer(page_img, edition=edition, site_url=SITE_URL)

    buf = io.BytesIO()
    final_img.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()

    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "X-Quota-Remaining": str(remaining),
            "Cache-Control": "no-store",
        },
    )
