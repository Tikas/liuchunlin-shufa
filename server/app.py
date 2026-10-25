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
import random
import re
import sqlite3
import string
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import cv2
import numpy as np
import segno
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

# Locate v003 engine & private font directory
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE if (HERE / "generate_liuchunlin_copybook.py").exists() else HERE.parent.parent
for _p in (str(HERE), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from generate_liuchunlin_copybook import render_copybook_page

PRIVATE_FONT_DIR = Path(os.environ.get("LIUCHUNLIN_FONT_DIR", str(PROJECT_ROOT / "output")))
TRAD_TTF_PATH = PRIVATE_FONT_DIR / "LiuChunlin-ShengJiaoXu-Trad.ttf"
SIMP_TTF_PATH = PRIVATE_FONT_DIR / "LiuChunlin-ShengJiaoXu-Simp.ttf"
VARIANTS_MAP_PATH = PRIVATE_FONT_DIR / "LiuChunlin-Variants-Map.json"
QUOTA_DB_PATH = Path(os.environ.get("LIUCHUNLIN_QUOTA_DB", str(HERE / "ip_quota.sqlite3")))
SITE_URL = os.environ.get("LIUCHUNLIN_SITE_URL", "https://dvbs2000.github.io/liuchunlin-shufa/")
HOURLY_IP_LIMIT = int(os.environ.get("LIUCHUNLIN_HOURLY_LIMIT", "10"))
ADMIN_SECRET = os.environ.get("LIUCHUNLIN_ADMIN_KEY", "2480555")

# Load PUA variant lookup tables once at startup
with open(VARIANTS_MAP_PATH, "r", encoding="utf-8") as f:
    _VARIANTS_PAYLOAD = json.load(f)["pua_variants"]

# Load Simplified <-> Traditional character conversion dictionaries
_TS_MAP_PATH = HERE / "trad_simp_map.json"
if _TS_MAP_PATH.exists():
    with open(_TS_MAP_PATH, "r", encoding="utf-8") as f:
        _ts_data = json.load(f)
        _S2T: Dict[str, str] = _ts_data.get("s2t", {})
        _T2S: Dict[str, str] = _ts_data.get("t2s", {})
else:
    _S2T, _T2S = {}, {}


def to_simp_text(s: str) -> str:
    return "".join(_T2S.get(c, c) for c in s)


def to_trad_text(s: str) -> str:
    return "".join(_S2T.get(c, c) for c in s)


def convert_for_edition(s: str, edition: str) -> str:
    return to_trad_text(s) if edition == "trad" else to_simp_text(s)


def strip_cjk_punct(s: str) -> str:
    return "".join(
        c for c in s if not c.isspace() and c not in "，。、；：？！“”‘’（）《》「」『』—…·.,!?;:()[]【】"
    )


# Load Classical Anthology Whitelist (`anthology.json`)
_ANTHOLOGY_PATH = HERE / "anthology.json"
if not _ANTHOLOGY_PATH.exists():
    _ANTHOLOGY_PATH = HERE.parent / "docs" / "anthology.json"

_WHITELIST_POEM_IDS: Dict[str, dict] = {}
_WHITELIST_CLEAN_SIMP_CORPUS: List[str] = []
if _ANTHOLOGY_PATH.exists():
    with open(_ANTHOLOGY_PATH, "r", encoding="utf-8") as f:
        for item in json.load(f):
            pid = item.get("id", "")
            if pid:
                _WHITELIST_POEM_IDS[pid] = item
            clean_s = strip_cjk_punct(to_simp_text(item.get("c_simp", "") or item.get("c_trad", "")))
            if len(clean_s) >= 8:
                _WHITELIST_CLEAN_SIMP_CORPUS.append(clean_s)


def is_in_classical_whitelist(body_text: str, poem_id: str = "") -> bool:
    """Verify if `body_text` belongs to the 963-poem classical anthology whitelist."""
    clean_in = strip_cjk_punct(to_simp_text(body_text))
    if len(clean_in) < 8:
        return False
    if poem_id and poem_id in _WHITELIST_POEM_IDS:
        item = _WHITELIST_POEM_IDS[poem_id]
        ref_s = strip_cjk_punct(to_simp_text(item.get("c_simp", "")))
        if clean_in in ref_s or ref_s in clean_in:
            return True
    for ref_s in _WHITELIST_CLEAN_SIMP_CORPUS:
        if clean_in in ref_s or ref_s in clean_in:
            return True
    return False


# Multi-category Illegal & Sensitive Content Filter + Acrostic Horizontal Row Scanner
_SENSITIVE_KEYWORDS = [
    # Political / Subversive / Sensitive
    "习近平", "毛泽东", "江泽民", "胡锦涛", "温家宝", "李克强", "邓小平", "周恩来",
    "共产党", "国民党", "法轮功", "真善忍", "天安门", "六四", "八九", "学潮", "独裁",
    "暴政", "推翻", "打倒", "政权", "游行", "示威", "罢工", "维权", "台独", "藏独",
    "疆独", "港独", "自由亚洲", "大纪元", "新唐人", "动态网", "翻墙", "梯子", "靖国神社",
    # Profanity / Insults / Abuse
    "傻逼", "煞笔", "草泥马", "操你", "日你", "妈的", "他妈", "去死", "混蛋", "王八蛋",
    "狗日", "贱人", "婊子", "妓女", "脑残", "智障", "废物", "畜生", "杂种", "滚蛋",
    "尼玛", "卧槽", "我操", "狗官", "贪官", "黑警", "杀光", "砍死", "炸死", "自杀",
    # Porn / Gambling / Fraud / Illegal Ads
    "色情", "淫秽", "强奸", "乱伦", "裸聊", "约炮", "援交", "嫖娼", "卖淫", "成人网站",
    "赌场", "赌博", "博彩", "六合彩", "时时彩", "百家乐", "网贷", "套现", "洗钱", "毒品",
    "冰毒", "海洛因", "大麻", "枪支", "弹药", "发票", "办证", "加微", "加V", "招代理",
]


def check_text_legality(
    title: str, author: str, colophon: str, body: str, rows_per_col: int = 14
) -> Tuple[bool, str]:
    """
    2-Layer Content Legality Verification for Paid VIP Custom Text:
    1. Direct normalized Simplified scan of `title`, `author`, `colophon`, and `body`.
    2. Vertical Calligraphic Grid Horizontal / Acrostic Scan (`防藏头诗与横向连读夹带检测`):
       Builds the `10 x rows_per_col` vertical grid and inspects every horizontal row
       (right-to-left and left-to-right) so hidden vertical/horizontal acrostics are blocked.
    """
    combined = to_simp_text(f"{title} {author} {colophon} {body}")
    combined_compact = strip_cjk_punct(combined)

    # Reject phone numbers / WeChat IDs / URLs in custom calligraphy to prevent spam/scam posters
    if re.search(r"(?:1[3-9]\d{9}|http://|https://|www\.|\.com|\.cn|vx|weixin)", combined.lower()):
        return False, "自定义书法内容不可包含网址、手机号或微信号等广告引流信息。"

    for kw in _SENSITIVE_KEYWORDS:
        if kw in combined or kw in combined_compact:
            return False, "您输入的内容包含敏感或不当词汇，书房雅集仅支持健康雅正之诗词文稿。"

    # Layer 2: Construct the 10-column vertical matrix and scan every horizontal line (防藏头诗)
    clean_chars = list(strip_cjk_punct(to_simp_text(body)))
    if clean_chars and rows_per_col > 0:
        cols: List[List[str]] = []
        for idx in range(0, min(len(clean_chars), 10 * rows_per_col), rows_per_col):
            cols.append(clean_chars[idx : idx + rows_per_col])
        for r in range(rows_per_col):
            row_rtl = "".join(col[r] for col in cols if r < len(col))
            row_ltr = row_rtl[::-1]
            for kw in _SENSITIVE_KEYWORDS:
                if len(kw) >= 2 and (kw in row_rtl or kw in row_ltr):
                    return False, "检测到竖排藏头或横向连读中包含不当词汇，请修改后重试。"

    return True, "OK"


_DB_LOCK = threading.Lock()
_RENDER_LOCK = threading.Lock()


def _init_quota_db() -> None:
    QUOTA_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(QUOTA_DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ip_hourly_quota (
                ip TEXT NOT NULL,
                hour_bucket TEXT NOT NULL,
                used_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (ip, hour_bucket)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ip_geo_cache (
                ip TEXT PRIMARY KEY,
                region TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visitor_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                day TEXT NOT NULL,
                ip TEXT NOT NULL,
                channel TEXT NOT NULL,
                device TEXT NOT NULL,
                referrer TEXT NOT NULL,
                ua TEXT NOT NULL,
                region TEXT NOT NULL DEFAULT '解析中...'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS render_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                day TEXT NOT NULL,
                ip TEXT NOT NULL,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                char_count INTEGER NOT NULL,
                edition TEXT NOT NULL,
                style TEXT NOT NULL,
                region TEXT NOT NULL DEFAULT '解析中...'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vip_codes (
                code TEXT PRIMARY KEY,
                note TEXT NOT NULL,
                remaining_uses INTEGER NOT NULL,
                total_uses INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # Ensure master admin/test VIP code exists
        conn.execute(
            """
            INSERT OR IGNORE INTO vip_codes (code, note, remaining_uses, total_uses, created_at)
            VALUES ('VIP2480555', '站长自用主通行码', 9999, 9999, '2026-10-25 10:00:00')
            """
        )
        conn.commit()


_init_quota_db()


def _now_bjt() -> Tuple[str, str]:
    """Return `(ts_str, day_str)` in Beijing Time (`UTC+8`)."""
    bjt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    return bjt.strftime("%Y-%m-%d %H:%M:%S"), bjt.strftime("%Y-%m-%d")


def _classify_channel_and_device(ua: str, ref: str, src: str) -> Tuple[str, str]:
    ua_l = (ua or "").lower()
    ref_l = (ref or "").lower()
    src_l = (src or "").lower()

    is_wx = "micromessenger" in ua_l
    if "iphone" in ua_l or "ipad" in ua_l:
        dev = "微信(iPhone)" if is_wx else "iPhone/iPad"
    elif "android" in ua_l:
        dev = "微信(Android)" if is_wx else "Android手机"
    elif "macintosh" in ua_l or "mac os" in ua_l:
        dev = "微信(Mac版)" if is_wx else "Mac电脑"
    elif "windows" in ua_l:
        dev = "微信(Windows版)" if is_wx else "Windows电脑"
    else:
        dev = "微信内置浏览器" if is_wx else "其他终端"

    if is_wx or src_l == "qr":
        channel = "微信长按扫码 / 朋友圈裂变"
    elif "github.com" in ref_l:
        channel = "GitHub 项目主页跳转"
    elif any(k in ref_l for k in ("google.", "baidu.", "bing.", "sogou.", "so.com")):
        channel = "搜索引擎自然流量"
    elif ref_l and "dvbs2000.github.io" not in ref_l:
        channel = f"外部网站引荐 ({ref[:36]})"
    else:
        channel = "直接访问 / 书签或系统相机扫码"
    return channel, dev


def _resolve_ip_region_bg(ip: str) -> None:
    if not ip or ip in ("127.0.0.1", "unknown", "::1"):
        region = "本地回环"
    else:
        with _DB_LOCK, sqlite3.connect(str(QUOTA_DB_PATH)) as conn:
            row = conn.execute("SELECT region FROM ip_geo_cache WHERE ip = ?", (ip,)).fetchone()
            if row and row[0] and row[0] != "解析中...":
                region = row[0]
            else:
                region = ""
        if not region:
            try:
                url = f"http://ip-api.com/json/{ip}?lang=zh-CN&fields=status,country,regionName,city,isp"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                if data.get("status") == "success":
                    parts = [p for p in (data.get("country"), data.get("regionName"), data.get("city")) if p]
                    isp = data.get("isp", "")
                    region = " · ".join(parts) + (f" ({isp[:18]})" if isp else "")
                else:
                    region = "海外或未知节点"
            except Exception:
                region = "未知地区"
            with _DB_LOCK, sqlite3.connect(str(QUOTA_DB_PATH)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO ip_geo_cache (ip, region) VALUES (?, ?)",
                    (ip, region),
                )
                conn.commit()

    with _DB_LOCK, sqlite3.connect(str(QUOTA_DB_PATH)) as conn:
        conn.execute("UPDATE visitor_logs SET region = ? WHERE ip = ? AND region = '解析中...'", (region, ip))
        conn.execute("UPDATE render_logs SET region = ? WHERE ip = ? AND region = '解析中...'", (region, ip))
        conn.commit()


def record_visitor_event(ip: str, ua: str, ref: str, src: str) -> None:
    ts, day = _now_bjt()
    channel, dev = _classify_channel_and_device(ua, ref, src)
    with _DB_LOCK, sqlite3.connect(str(QUOTA_DB_PATH)) as conn:
        cached = conn.execute("SELECT region FROM ip_geo_cache WHERE ip = ?", (ip,)).fetchone()
        region = cached[0] if cached else "解析中..."
        conn.execute(
            """
            INSERT INTO visitor_logs (ts, day, ip, channel, device, referrer, ua, region)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, day, ip, channel, dev, (ref or "")[:180], (ua or "")[:220], region),
        )
        conn.commit()
    if region == "解析中...":
        threading.Thread(target=_resolve_ip_region_bg, args=(ip,), daemon=True).start()


def record_render_event(ip: str, title: str, author: str, char_count: int, edition: str, style: str) -> None:
    ts, day = _now_bjt()
    with _DB_LOCK, sqlite3.connect(str(QUOTA_DB_PATH)) as conn:
        cached = conn.execute("SELECT region FROM ip_geo_cache WHERE ip = ?", (ip,)).fetchone()
        region = cached[0] if cached else "解析中..."
        conn.execute(
            """
            INSERT INTO render_logs (ts, day, ip, title, author, char_count, edition, style, region)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, day, ip, title or "（无题）", author or "（默认）", char_count, edition, style, region),
        )
        conn.commit()
    if region == "解析中...":
        threading.Thread(target=_resolve_ip_region_bg, args=(ip,), daemon=True).start()


def get_client_ip(request: Request) -> str:
    """Extract real client IP (trusting local Nginx `X-Real-IP` first to prevent header spoofing)."""
    xri = request.headers.get("X-Real-IP")
    if xri:
        return xri.strip()
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_and_consume_quota(ip: str, consume: bool = True) -> Tuple[bool, int]:
    """
    Check if `ip` has remaining quota in the current hour (`HOURLY_IP_LIMIT = 10`).
    Returns `(allowed, remaining_after)`.
    """
    hour_bucket = datetime.datetime.now().strftime("%Y-%m-%dT%H")
    with _DB_LOCK, sqlite3.connect(str(QUOTA_DB_PATH)) as conn:
        row = conn.execute(
            "SELECT used_count FROM ip_hourly_quota WHERE ip = ? AND hour_bucket = ?",
            (ip, hour_bucket),
        ).fetchone()
        used = int(row[0]) if row else 0
        if not consume:
            return (used < HOURLY_IP_LIMIT), max(0, HOURLY_IP_LIMIT - used)
        if used >= HOURLY_IP_LIMIT:
            return False, 0
        new_used = used + 1
        conn.execute(
            """
            INSERT INTO ip_hourly_quota (ip, hour_bucket, used_count)
            VALUES (?, ?, ?)
            ON CONFLICT(ip, hour_bucket) DO UPDATE SET used_count = excluded.used_count
            """,
            (ip, hour_bucket, new_used),
        )
        conn.commit()
        return True, max(0, HOURLY_IP_LIMIT - new_used)


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
        line2 = "微信長按右側朱砂印章 · 輸入任意詩詞文字，一秒生成宣紙字帖作品"
    else:
        line1 = "刘春霖小楷书房 · 在线生成您的专属诗词书法"
        line2 = "微信长按右侧朱砂印章 · 输入任意诗词文字，一秒生成宣纸字帖作品"

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
    expose_headers=[
        "X-Quota-Remaining",
        "X-VIP-Remaining",
        "X-Detected-Title",
        "X-Detected-Author",
        "X-Page-Truncated",
        "X-Max-Page-Chars",
    ],
)


@app.middleware("http")
async def block_font_probing_middleware(request: Request, call_next):
    """Block any attempt to probe or download font/vector/source files."""
    path_lower = request.url.path.lower()
    if any(ext in path_lower for ext in (".ttf", ".otf", ".woff", ".woff2", ".svg", ".py", ".zip", ".db", "..")):
        return Response(status_code=403, content="Forbidden")
    return await call_next(request)


class RenderRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2500)
    title: str = Field(default="", max_length=16)
    author: str = Field(default="", max_length=10)
    colophon: str = Field(default="", max_length=12)
    edition: str = Field(default="simp", pattern="^(trad|simp)$")
    style: str = Field(default="zhalan", pattern="^(zhalan|clean)$")
    poem_id: str = Field(default="", max_length=16)
    vip_code: str = Field(default="", max_length=32)


class VipCreateRequest(BaseModel):
    admin_key: str = Field(..., min_length=1, max_length=64)
    note: str = Field(default="微信付费用户", max_length=64)
    uses: int = Field(default=50, ge=1, le=99999)
    custom_code: str = Field(default="", max_length=24)


def verify_and_consume_vip(code: str, consume: bool = False) -> Tuple[bool, int, str]:
    clean_c = code.strip().upper()
    if not clean_c:
        return False, 0, ""
    with _DB_LOCK, sqlite3.connect(str(QUOTA_DB_PATH)) as conn:
        row = conn.execute(
            "SELECT remaining_uses, total_uses, note FROM vip_codes WHERE UPPER(code) = ?",
            (clean_c,),
        ).fetchone()
        if not row:
            return False, 0, ""
        rem, tot, note = int(row[0]), int(row[1]), str(row[2])
        if rem <= 0:
            return False, 0, note
        if consume:
            new_rem = rem - 1
            conn.execute(
                "UPDATE vip_codes SET remaining_uses = ? WHERE UPPER(code) = ?",
                (new_rem, clean_c),
            )
            conn.commit()
            return True, new_rem, note
        return True, rem, note


@app.get("/api/vip/verify")
async def verify_vip_endpoint(code: str = ""):
    ok, rem, note = verify_and_consume_vip(code, consume=False)
    return {"valid": ok, "remaining": rem, "note": note}


@app.post("/api/vip/create")
async def create_vip_endpoint(req: VipCreateRequest):
    if req.admin_key.strip() != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="管理员密钥错误")
    code = req.custom_code.strip().upper()
    if not code:
        alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
        code = "VIP" + "".join(random.choice(alphabet) for _ in range(5))
    ts, _ = _now_bjt()
    with _DB_LOCK, sqlite3.connect(str(QUOTA_DB_PATH)) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO vip_codes (code, note, remaining_uses, total_uses, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (code, req.note.strip() or "微信VIP用户", req.uses, req.uses, ts),
        )
        conn.commit()
    return {"code": code, "note": req.note, "uses": req.uses, "created_at": ts}


@app.get("/api/quota")
async def get_quota_status(request: Request, ref: str = "", src: str = "", track: int = 1):
    ip = get_client_ip(request)
    if track == 1:
        ua = request.headers.get("User-Agent", "")
        eff_ref = ref or request.headers.get("Referer", "")
        record_visitor_event(ip=ip, ua=ua, ref=eff_ref, src=src)
    allowed, rem = check_and_consume_quota(ip, consume=False)
    return {
        "allowed": allowed,
        "remaining": rem,
        "hourly_limit": HOURLY_IP_LIMIT,
        "daily_limit": HOURLY_IP_LIMIT,
    }


@app.get("/api/stats")
async def get_visitor_stats():
    _, today = _now_bjt()
    with _DB_LOCK, sqlite3.connect(str(QUOTA_DB_PATH)) as conn:
        today_pv = conn.execute("SELECT COUNT(*) FROM visitor_logs WHERE day = ?", (today,)).fetchone()[0]
        today_uv = conn.execute("SELECT COUNT(DISTINCT ip) FROM visitor_logs WHERE day = ?", (today,)).fetchone()[0]
        today_renders = conn.execute("SELECT COUNT(*) FROM render_logs WHERE day = ?", (today,)).fetchone()[0]

        total_pv = conn.execute("SELECT COUNT(*) FROM visitor_logs").fetchone()[0]
        total_uv = conn.execute("SELECT COUNT(DISTINCT ip) FROM visitor_logs").fetchone()[0]
        total_renders = conn.execute("SELECT COUNT(*) FROM render_logs").fetchone()[0]

        wx_visits = conn.execute(
            "SELECT COUNT(*) FROM visitor_logs WHERE channel LIKE '%微信%'"
        ).fetchone()[0]

        ch_rows = conn.execute(
            "SELECT channel, COUNT(*) as cnt FROM visitor_logs GROUP BY channel ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        reg_rows = conn.execute(
            "SELECT region, COUNT(*) as cnt FROM visitor_logs GROUP BY region ORDER BY cnt DESC LIMIT 15"
        ).fetchall()
        dev_rows = conn.execute(
            "SELECT device, COUNT(*) as cnt FROM visitor_logs GROUP BY device ORDER BY cnt DESC LIMIT 10"
        ).fetchall()

        rec_renders = conn.execute(
            "SELECT ts, ip, region, title, author, char_count, edition, style FROM render_logs ORDER BY id DESC LIMIT 30"
        ).fetchall()
        rec_visitors = conn.execute(
            "SELECT ts, ip, region, channel, device, referrer FROM visitor_logs ORDER BY id DESC LIMIT 50"
        ).fetchall()
        vip_rows = conn.execute(
            "SELECT code, note, remaining_uses, total_uses, created_at FROM vip_codes ORDER BY created_at DESC LIMIT 50"
        ).fetchall()

    denom = max(1, total_pv)
    return {
        "summary": {
            "today_pv": today_pv,
            "today_uv": today_uv,
            "today_renders": today_renders,
            "total_pv": total_pv,
            "total_uv": total_uv,
            "total_renders": total_renders,
            "wechat_visits": wx_visits,
            "wechat_ratio_pct": round(wx_visits * 100.0 / denom, 1),
        },
        "channels": [
            {"name": r[0], "count": r[1], "pct": round(r[1] * 100.0 / denom, 1)} for r in ch_rows
        ],
        "regions": [
            {"name": r[0], "count": r[1], "pct": round(r[1] * 100.0 / denom, 1)} for r in reg_rows
        ],
        "devices": [
            {"name": r[0], "count": r[1], "pct": round(r[1] * 100.0 / denom, 1)} for r in dev_rows
        ],
        "vip_codes": [
            {
                "code": r[0],
                "note": r[1],
                "remaining_uses": r[2],
                "total_uses": r[3],
                "created_at": r[4],
            }
            for r in vip_rows
        ],
        "recent_renders": [
            {
                "ts": r[0],
                "ip": r[1],
                "region": r[2],
                "title": r[3],
                "author": r[4],
                "char_count": r[5],
                "edition": "繁体" if r[6] == "trad" else "简体",
                "style": "乌丝栏" if r[7] == "zhalan" else "无格素笺",
            }
            for r in rec_renders
        ],
        "recent_visitors": [
            {
                "ts": r[0],
                "ip": r[1],
                "region": r[2],
                "channel": r[3],
                "device": r[4],
                "referrer": r[5] or "（直接打开/扫码）",
            }
            for r in rec_visitors
        ],
    }


@app.post("/api/render")
def render_calligraphy_endpoint(req: RenderRequest, request: Request):
    ip = get_client_ip(request)

    # Smart parse title, author, and body text
    title, author, body = smart_parse_poem_input(req.text, req.title, req.author)
    clean_body = [c for c in body if not c.isspace() and c not in "，。、；：？！“”‘’（）《》「」『』—…"]
    if len(clean_body) < 2:
        raise HTTPException(status_code=400, detail="请输入至少 2 个汉字的正文内容。")

    edition = req.edition
    style = req.style
    has_title_col = bool(title)
    rows_per_col = detect_optimal_rows_per_col(body, has_title_col=has_title_col, ruling_style=style)

    # Strictly enforce 963-work Classical Tang/Song/Prose Whitelist (`只允许搜索唐诗宋词来体验`)
    if not is_in_classical_whitelist(body, poem_id=req.poem_id):
        raise HTTPException(
            status_code=403,
            detail="当前仅支持搜索《传世唐诗宋词·古文经典库》名篇体验生成。如需输入自定义文字，请联系邮箱 sdicfund@hotmail.com",
        )
    # Enforce 10 generations per IP per hour quota
    allowed, remaining = check_and_consume_quota(ip, consume=True)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"本小时墨池已歇（每 IP 每小时最多免费生成 {HOURLY_IP_LIMIT} 幅古诗词），如需输入自定义文字请联系邮箱 sdicfund@hotmail.com",
        )
    is_vip = False
    vip_rem = -1

    ttf_path = TRAD_TTF_PATH if edition == "trad" else SIMP_TTF_PATH
    vmap = _VARIANTS_PAYLOAD[edition]

    # Automatically convert characters to match selected edition (Traditional vs Simplified)
    title_conv = convert_for_edition(title, edition)
    author_conv = convert_for_edition(author, edition)
    body_conv = convert_for_edition(body, edition)
    clean_body_conv = [
        c for c in body_conv if not c.isspace() and c not in "，。、；：？！“”‘’（）《》「」『』—…"
    ]

    cell_pitch_px = 96 if rows_per_col == 14 else (92 if rows_per_col >= 15 else 100)
    col_pitch_px = 126 if style == "zhalan" else 128
    glyph_scale = 1.16 if rows_per_col == 14 else (1.18 if style == "zhalan" else 1.12)

    default_colophon = "" if has_title_col else ("春霖敬書" if edition == "trad" else "刘春霖书")
    raw_colophon = req.colophon.strip() if is_vip else ""
    colophon = convert_for_edition(raw_colophon, edition) or default_colophon

    # Strictly process ONLY 1 page (`num_cols = 10`)
    avail_body_cols = 10 - (1 if has_title_col else 0)
    reserved_tail_rows = (len(colophon) + 2) if colophon else 0
    max_page_chars = (avail_body_cols - 1) * rows_per_col + max(2, rows_per_col - reserved_tail_rows)
    was_truncated = len(clean_body_conv) > max_page_chars
    one_page_body = "".join(clean_body_conv[:max_page_chars])

    tag_prefix = "[VIP自定义] " if is_vip else "[免费古籍] "
    record_render_event(
        ip=ip,
        title=tag_prefix + (title_conv or "无题"),
        author=author_conv or colophon,
        char_count=len(one_page_body),
        edition=edition,
        style=style,
    )

    with _RENDER_LOCK:
        page_img = render_copybook_page(
            text=one_page_body,
            ttf_path=ttf_path,
            pua_variants=vmap,
            colophon=colophon,
            poem_title=title_conv,
            poem_author=author_conv,
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
        final_img.save(buf, format="JPEG", quality=92, subsampling=0, optimize=True)
        img_bytes = buf.getvalue()

    resp_headers = {
        "X-Quota-Remaining": str(remaining),
        "X-Page-Truncated": "1" if was_truncated else "0",
        "X-Max-Page-Chars": str(max_page_chars),
        "Cache-Control": "no-store",
    }
    if is_vip:
        resp_headers["X-VIP-Remaining"] = str(vip_rem)

    return Response(
        content=img_bytes,
        media_type="image/jpeg",
        headers=resp_headers,
    )
