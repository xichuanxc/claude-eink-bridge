#!/usr/bin/env python3
"""
Claude Code E-Ink Bridge

Reads per-session Claude HUD snapshots, picks the most recently active one,
renders a 400×300 dashboard, and pushes to a Zectrix e-ink device.

Supports multiple concurrent Claude Code sessions — always shows the
most recently updated one.

Usage:
    python main.py              # Run continuously (default 60s interval)
    python main.py --once       # Single push then exit
    python main.py --preview    # Generate preview image only (no push)
    python main.py --debug      # Print raw snapshot data each cycle
    python main.py --mode mono  # Force 1-bit output for this run
"""

import json
import math
import os
import io
import sys
import signal
import atexit
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont
import requests


# ─── Configuration ───────────────────────────────────────────────────────────

# Honor CLAUDE_CONFIG_DIR so the bridge works with non-default Claude config
# directories (e.g. a separate ~/.claude-team instance). Falls back to ~/.claude.
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
PLUGIN_DIR = CLAUDE_DIR / "plugins" / "claude-hud"
SNAPSHOTS_DIR = PLUGIN_DIR / "eink-snapshots"
PID_FILE = PLUGIN_DIR / "eink-bridge.pid"
STALE_THRESHOLD = 3600  # seconds — ignore snapshots older than this

REQUIRED_CONFIG_KEYS = ("api_key", "mac_address", "page_id")


def load_config():
    config_path = Path(__file__).parent / "config.json"
    with open(config_path) as f:
        cfg = json.load(f)

    for key in REQUIRED_CONFIG_KEYS:
        if not cfg.get(key):
            print(f"❌ Missing required config field: '{key}'")
            print(f"   Copy config.example.json → config.json and fill in your values.")
            sys.exit(1)

    font_raw = cfg.get("font_path", "font.ttf")
    if not os.path.isabs(font_raw):
        font_raw = str(Path(__file__).parent / font_raw)
    cfg["font_path"] = font_raw

    mode = str(cfg.get("render_mode", "auto")).lower()
    if mode not in ("auto", "gray", "mono"):
        print(f"⚠️  Unknown render_mode '{mode}' — falling back to 'auto'.")
        mode = "auto"
    cfg["render_mode"] = mode
    return cfg


# ─── PID Management ─────────────────────────────────────────────────────────

def write_pid():
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def remove_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ─── Snapshot Scanner ────────────────────────────────────────────────────────

def scan_snapshots():
    """
    Single-pass scan of the snapshots directory.

    Returns (latest_data, active_count, newest_age_seconds):
      latest_data       — dict from the most recently modified snapshot, or None
      active_count      — number of non-stale snapshot files
      newest_age_seconds — seconds since the most recent snapshot was written
                           (float("inf") when no snapshots exist)

    Also deletes stale files (older than STALE_THRESHOLD) as a side effect.
    Each Claude Code instance writes:
        ~/.claude/plugins/claude-hud/eink-snapshots/{session_hash}.json
    """
    if not SNAPSHOTS_DIR.exists():
        return None, 0, float("inf")

    now = time.time()
    latest_data = None
    latest_mtime = 0
    active_count = 0
    newest_mtime = 0

    for f in SNAPSHOTS_DIR.glob("*.json"):
        try:
            mtime = f.stat().st_mtime
            if now - mtime > STALE_THRESHOLD:
                f.unlink(missing_ok=True)
                continue
        except Exception:
            continue

        active_count += 1
        if mtime > newest_mtime:
            newest_mtime = mtime

        try:
            if mtime > latest_mtime:
                latest_data = json.loads(f.read_text())
                latest_mtime = mtime
        except Exception:
            continue

    newest_age = (now - newest_mtime) if newest_mtime > 0 else float("inf")
    return latest_data, active_count, newest_age


# ─── Helpers ─────────────────────────────────────────────────────────────────

def format_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1000}k"
    return str(n)


def format_reset_time(reset_iso):
    if not reset_iso:
        return None
    try:
        reset_dt = datetime.fromisoformat(reset_iso.replace("Z", "+00:00"))
        delta = reset_dt - datetime.now(timezone.utc)
        total_sec = int(delta.total_seconds())
        if total_sec <= 0:
            return "now"
        hours, remainder = divmod(total_sec, 3600)
        minutes = remainder // 60
        if hours >= 24:
            days = hours // 24
            remaining_hours = hours % 24
            if remaining_hours > 0:
                return f"{days}d {remaining_hours}h"
            return f"{days}d"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return None


# ─── Image Renderer ──────────────────────────────────────────────────────────

# Curved geometry (rings, rounded bar caps) is drawn on a layer this many times
# larger than the canvas and box-filtered back down, which gives clean edges
# that ImageDraw cannot produce on its own.
SS = 4


class EinkRenderer:
    """Renders the 400×300 dashboard as an 8-bit greyscale image.

    Everything is composed in "L" so glyphs and curves come out anti-aliased.
    Nothing is drawn in a mid-tone — unfilled bars and the ring are hairline
    outlines — because a flat grey has to be dithered somewhere, and on this
    panel the halftone reads as dirt. ``render_mode`` decides what leaves the
    renderer:
      "gray" — the greyscale image, anti-aliased edges intact
      "mono" — thresholded to 1-bit
    """

    W, H = 400, 300

    # ── Tones ─────────────────────────────────────────────────────
    # Solid ink only. A flat mid-grey has to be dithered somewhere — by us or
    # by the panel — and on a ~120 DPI e-ink at this contrast the halftone
    # reads as dirt rather than as a lighter shade. Unfilled areas are drawn
    # as hairline outlines instead.
    INK = 0

    # ── Layout grid ───────────────────────────────────────────────
    PAD        = 16
    HDR_BASE   = 25          # header text baseline
    RULE_HDR   = 37
    RULE_BODY  = 190
    RULE_FOOT  = 266
    FOOT_BASE  = 288

    L_RIGHT    = 268         # right edge of the left body column
    MODEL_BASE = 78
    META_BASE  = 104
    STAT_RULE  = 122
    STAT_LBL   = 141
    STAT_VAL   = 166

    RING_CX, RING_CY = 330, 100
    RING_R, RING_TH  = 48, 10
    RING_CAP_BASE    = 168   # "48k / 200k" under the ring

    ROW_TOP  = 197           # first rate-limit row
    ROW_H    = 34
    BAR_X0, BAR_X1 = 48, 248
    BAR_H    = 12
    PCT_RIGHT   = 314
    RESET_RIGHT = 384

    def __init__(self, font_path, greeting="今天的Token用完了吗？", render_mode="gray"):
        self.greeting = greeting
        self.render_mode = render_mode if render_mode in ("gray", "mono") else "gray"
        self._font_path = font_path
        self._fonts = {}
        self._digit_w = {}

    # ── Fonts ─────────────────────────────────────────────────────

    def f(self, size):
        if size not in self._fonts:
            self._fonts[size] = ImageFont.truetype(self._font_path, size)
        return self._fonts[size]

    def _dw(self, font):
        """Widest digit advance — used to fake tabular figures."""
        if font not in self._digit_w:
            self._digit_w[font] = max(font.getlength(str(d)) for d in range(10))
        return self._digit_w[font]

    # ── Text ──────────────────────────────────────────────────────

    def _text(self, draw, x, baseline, text, font, fill=INK, align="l"):
        anchor = {"l": "ls", "r": "rs", "c": "ms"}[align]
        draw.text((x, baseline), text, font=font, fill=fill, anchor=anchor)

    def _tab_w(self, font, text):
        dw = self._dw(font)
        return sum(dw if c.isdigit() else font.getlength(c) for c in text)

    def _tab(self, draw, x, baseline, text, font, fill=INK, align="l"):
        """Draw text with fixed-width digits.

        MiSans' figures are proportional, so a plain "9%" → "10%" transition
        reflows the whole line. On a screen that redraws every minute that
        reads as jitter, so numerals get a uniform advance instead.
        """
        dw = self._dw(font)
        total = self._tab_w(font, text)
        if align == "r":
            x -= total
        elif align == "c":
            x -= total / 2
        for c in text:
            if c.isdigit():
                draw.text((x + (dw - font.getlength(c)) / 2, baseline), c,
                          font=font, fill=fill, anchor="ls")
                x += dw
            else:
                draw.text((x, baseline), c, font=font, fill=fill, anchor="ls")
                x += font.getlength(c)

    def _tracked(self, draw, x, baseline, text, font, spacing, fill=INK):
        """Letter-spaced text, for the small uppercase labels."""
        for c in text:
            draw.text((x, baseline), c, font=font, fill=fill, anchor="ls")
            x += font.getlength(c) + spacing

    def _fit(self, text, sizes, max_w):
        """Largest of `sizes` at which `text` still fits, else the smallest."""
        for size in sizes:
            font = self.f(size)
            if font.getlength(text) <= max_w:
                return font
        return self.f(sizes[-1])

    def _truncate(self, text, font, max_w):
        if font.getlength(text) <= max_w:
            return text
        while text and font.getlength(text + "…") > max_w:
            text = text[:-1]
        return text + "…"

    # ── Geometry (drawn supersampled, composited darkest-wins) ─────

    def _layer(self):
        img = Image.new("L", (self.W * SS, self.H * SS), 255)
        return img, ImageDraw.Draw(img)

    def _flatten(self, base, layer):
        # BOX is an exact area average: no ringing halos around the shapes.
        return ImageChops.darker(base, layer.resize((self.W, self.H), Image.BOX))

    def _rule(self, draw, y, x0=None, x1=None):
        x0 = self.PAD if x0 is None else x0
        x1 = (self.W - self.PAD) if x1 is None else x1
        draw.rectangle([(x0 * SS, y * SS), (x1 * SS, (y + 1) * SS - 1)], fill=self.INK)

    def _bar(self, layer, x0, y0, x1, y1, percent):
        """Hairline pill, filled solid up to `percent`."""
        w, h = (x1 - x0) * SS, (y1 - y0) * SS
        r = h / 2
        ox, oy = x0 * SS, y0 * SS

        ImageDraw.Draw(layer).rounded_rectangle(
            [(ox, oy), (ox + w - 1, oy + h - 1)],
            radius=r, outline=self.INK, width=SS,
        )

        fill_w = int(w * min(max(percent, 0), 100) / 100)
        if fill_w > 0:
            mask = Image.new("L", (w, h), 0)
            md = ImageDraw.Draw(mask)
            md.rounded_rectangle([(0, 0), (w - 1, h - 1)], radius=r, fill=255)
            md.rectangle([(fill_w, 0), (w, h)], fill=0)
            layer.paste(Image.new("L", (w, h), self.INK), (ox, oy), mask)

    def _ring(self, layer, cx, cy, r, thickness, percent):
        """Hairline annulus, filled solid over the swept arc."""
        d = 2 * r * SS
        ox, oy = (cx - r) * SS, (cy - r) * SS
        box = [(0, 0), (d - 1, d - 1)]
        pad = thickness * SS
        inner = [(pad, pad), (d - 1 - pad, d - 1 - pad)]

        ld = ImageDraw.Draw(layer)
        ld.ellipse([(ox, oy), (ox + d - 1, oy + d - 1)], outline=self.INK, width=SS)
        ld.ellipse([(ox + pad, oy + pad), (ox + d - 1 - pad, oy + d - 1 - pad)],
                   outline=self.INK, width=SS)

        pct = min(max(percent, 0), 100)
        if pct <= 0:
            return

        sweep = 360 * pct / 100
        mask = Image.new("L", (d, d), 0)
        md = ImageDraw.Draw(mask)
        md.pieslice(box, -90, -90 + sweep, fill=255)
        # Round off both ends of the arc.
        mid = (r - thickness / 2) * SS
        cap = thickness * SS / 2
        for ang in (-90, -90 + sweep):
            ax = d / 2 + mid * math.cos(math.radians(ang))
            ay = d / 2 + mid * math.sin(math.radians(ang))
            md.ellipse([(ax - cap, ay - cap), (ax + cap, ay + cap)], fill=255)
        md.ellipse(inner, fill=0)
        layer.paste(Image.new("L", (d, d), self.INK), (ox, oy), mask)

    # ── Output conversion ─────────────────────────────────────────

    def _compose(self, shapes, text):
        """Merge the shape and text planes and convert for the output mode.

        Since nothing is drawn in a mid-tone, mono simply thresholds: no
        dithering is involved and the only greys in play are the anti-aliased
        edges of glyphs and curves.
        """
        merged = ImageChops.darker(shapes, text)
        if self.render_mode == "mono":
            return merged.point(lambda p: 0 if p < 128 else 255).convert("1")
        return merged

    # ── Public entry point ────────────────────────────────────────

    def render(self, snapshot, active_sessions=0):
        shapes = Image.new("L", (self.W, self.H), 255)
        layer, ld = self._layer()
        text = Image.new("L", (self.W, self.H), 255)
        draw = ImageDraw.Draw(text)

        if snapshot is None:
            self._waiting_shapes(layer, ld)
            self._waiting_text(draw)
        else:
            self._chrome_shapes(ld)
            self._body_shapes(layer, ld, snapshot)
            self._limit_shapes(layer, snapshot)
            self._header(draw, snapshot)
            self._body(draw, snapshot)
            self._limits(draw, snapshot)
            self._footer(draw, snapshot, active_sessions)

        return self._compose(self._flatten(shapes, layer), text)

    # ── Waiting screen ────────────────────────────────────────────

    def _waiting_shapes(self, layer, ld):
        self._rule(ld, self.RULE_HDR, 0, self.W)
        self._ring(layer, self.W // 2, 145, 46, 10, 0)

    def _waiting_text(self, draw):
        avail = self.W - 2 * self.PAD
        f_greet = self._fit(self.greeting, (17, 16, 15, 14, 13), avail)
        self._text(draw, self.PAD, self.HDR_BASE,
                   self._truncate(self.greeting, f_greet, avail), f_greet)
        draw.text((self.W // 2, 145), "···", font=self.f(26),
                  fill=self.INK, anchor="mm")
        self._text(draw, self.W // 2, 232, "Waiting for a session", self.f(17), align="c")
        self._text(draw, self.W // 2, 256, "Start Claude Code to begin", self.f(13), align="c")

    # ── Chrome ────────────────────────────────────────────────────

    def _chrome_shapes(self, ld):
        self._rule(ld, self.RULE_HDR, 0, self.W)
        self._rule(ld, self.RULE_BODY, 0, self.W)
        self._rule(ld, self.RULE_FOOT, 0, self.W)

    def _header(self, draw, snapshot):
        ts = self._parse_ts(snapshot)
        date_str = ts.strftime("%Y-%m-%d") if ts else "----------"
        date_w = self._tab_w(self.f(13), date_str)
        # A longer greeting steps down a size or two rather than losing its tail.
        avail = self.W - 2 * self.PAD - date_w - 16
        f_greet = self._fit(self.greeting, (17, 16, 15, 14, 13), avail)
        self._text(draw, self.PAD, self.HDR_BASE,
                   self._truncate(self.greeting, f_greet, avail), f_greet)
        self._tab(draw, self.W - self.PAD, self.HDR_BASE, date_str, self.f(13), align="r")

    def _footer(self, draw, snapshot, active_sessions):
        y = self.FOOT_BASE
        session = snapshot.get("sessionDuration", "")
        if session:
            self._tab(draw, self.PAD, y, f"Session {session}", self.f(12))

        if active_sessions > 1:
            self._tab(draw, self.W // 2, y, f"{active_sessions} sessions",
                      self.f(12), align="c")

        ts = self._parse_ts(snapshot)
        self._tab(draw, self.W - self.PAD, y,
                  ts.strftime("%H:%M") if ts else "--:--", self.f(12), align="r")

    # ── Body ──────────────────────────────────────────────────────

    def _body_shapes(self, layer, ld, snapshot):
        self._rule(ld, self.STAT_RULE, self.PAD, self.L_RIGHT - 10)
        pct = (snapshot.get("context") or {}).get("percent", 0)
        self._ring(layer, self.RING_CX, self.RING_CY, self.RING_R, self.RING_TH, pct)

    def _body(self, draw, snapshot):
        max_w = self.L_RIGHT - self.PAD

        model = snapshot.get("model", "Unknown")
        self._text(draw, self.PAD, self.MODEL_BASE,
                   self._truncate(model, self.f(32), max_w), self.f(32))

        # project · branch
        # The branch is the more perishable half, so the directory gives up
        # room first rather than the whole line truncating from the right.
        f_meta = self.f(13)
        git = snapshot.get("git") or {}
        branch = (git["branch"] + ("*" if git.get("isDirty") else "")) if git.get("branch") else ""
        branch = self._truncate(branch, f_meta, max_w * 0.55) if branch else ""
        project = snapshot.get("project", "")
        name = project.replace("\\", "/").rstrip("/").split("/")[-1] if project else ""
        sep = "  ·  " if (name and branch) else ""
        if name:
            name = self._truncate(
                name, f_meta, max_w - f_meta.getlength(sep + branch))
        if name or branch:
            self._text(draw, self.PAD, self.META_BASE, name + sep + branch, f_meta)

        # in / out / cache
        ctx = snapshot.get("context") or {}
        cols = [
            ("IN",    format_tokens(ctx.get("inputTokens", 0))),
            ("OUT",   format_tokens(ctx.get("outputTokens", 0))),
            ("CACHE", format_tokens(ctx.get("cacheTokens", 0))),
        ]
        col_w = (self.L_RIGHT - 10 - self.PAD) / 3
        for i, (label, value) in enumerate(cols):
            x = self.PAD + i * col_w
            self._tracked(draw, x, self.STAT_LBL, label, self.f(11), 1.4)
            self._tab(draw, x, self.STAT_VAL, value, self.f(18))

        # Context ring label
        pct = ctx.get("percent", 0)
        self._tab(draw, self.RING_CX, self.RING_CY + 10, f"{pct}%", self.f(28), align="c")
        total, size = ctx.get("totalTokens", 0), ctx.get("windowSize", 0)
        cap = (f"{format_tokens(total)} / {format_tokens(size)}"
               if size > 0 else "CONTEXT")
        self._tab(draw, self.RING_CX, self.RING_CAP_BASE, cap, self.f(12), align="c")

    # ── Rate limits ───────────────────────────────────────────────

    def _rows(self, snapshot):
        usage = snapshot.get("usage") or {}
        rows = []
        if usage.get("fiveHour") is not None:
            rows.append(("5H", usage["fiveHour"], usage.get("fiveHourResetAt")))
        if usage.get("sevenDay") is not None:
            rows.append(("7D", usage["sevenDay"], usage.get("sevenDayResetAt")))
        return rows

    def _row_top(self, rows):
        """Centre the block when only one window reported a limit."""
        if len(rows) >= 2:
            return self.ROW_TOP
        top, bottom = self.RULE_BODY + 1, self.RULE_FOOT
        return top + (bottom - top - len(rows) * self.ROW_H) // 2

    def _limit_shapes(self, layer, snapshot):
        rows = self._rows(snapshot)
        top = self._row_top(rows)
        for i, (_, percent, _) in enumerate(rows):
            cy = top + i * self.ROW_H + self.ROW_H // 2
            self._bar(layer, self.BAR_X0, cy - self.BAR_H // 2,
                      self.BAR_X1, cy + self.BAR_H // 2, percent)

    def _limits(self, draw, snapshot):
        rows = self._rows(snapshot)
        if not rows:
            self._text(draw, self.W // 2, (self.RULE_BODY + self.RULE_FOOT) // 2 + 5,
                       "No rate-limit data", self.f(13), align="c")
            return
        top = self._row_top(rows)
        for i, (label, percent, reset_iso) in enumerate(rows):
            base = top + i * self.ROW_H + self.ROW_H // 2 + 6
            self._text(draw, self.PAD, base, label, self.f(15))
            self._tab(draw, self.PCT_RIGHT, base, f"{percent}%", self.f(15), align="r")
            reset = format_reset_time(reset_iso)
            if reset:
                self._tab(draw, self.RESET_RIGHT, base, reset, self.f(15), align="r")

    # ── Misc ──────────────────────────────────────────────────────

    def _parse_ts(self, snapshot):
        try:
            return datetime.fromisoformat(
                snapshot["timestamp"].replace("Z", "+00:00")
            ).astimezone()
        except Exception:
            return None


# ─── Zectrix API ─────────────────────────────────────────────────────────────

def detect_render_mode(config):
    """Ask the cloud what the panel can actually show.

    A 1-bit panel has no grey levels, so anti-aliased edges only give its own
    dithering something to speckle — those boards want a plain threshold.
    Anything else can use the greyscale render. Falls back to "mono", which is
    the safe answer for the black-and-white boards these devices usually are.
    """
    try:
        res = requests.get(
            "https://cloud.zectrix.com/open/v1/devices",
            headers={"X-API-Key": config["api_key"]}, timeout=15,
        )
        res.raise_for_status()
        for dev in res.json().get("data") or []:
            if str(dev.get("deviceId", "")).lower() == config["mac_address"].lower():
                colour = str(dev.get("screenColor", "")).lower()
                mode = "mono" if colour == "1bit" else "gray"
                print(f"  Detected: {dev.get('board', 'device')} / {colour} → {mode}")
                return mode
        print("  ⚠️  Device not found in account — defaulting to 'mono'.")
    except Exception as e:
        print(f"  ⚠️  Could not query device ({e}) — defaulting to 'mono'.")
    return "mono"



def push_to_device(img, config):
    # Greyscale output is handed to the device with dithering on so it can map
    # the tones to its own palette; a 1-bit image is already final.
    dither = "false" if config.get("render_mode") == "mono" else "true"

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    push_url = (
        f"https://cloud.zectrix.com/open/v1/devices/"
        f"{config['mac_address']}/display/image"
    )
    headers = {"X-API-Key": config["api_key"]}
    data = {"dither": dither, "pageId": str(config["page_id"])}

    try:
        files = {"images": ("claude-hud.png", buf, "image/png")}
        res = requests.post(
            push_url, headers=headers, files=files, data=data, timeout=30
        )
        res.raise_for_status()
        print(f"  ✅ Push OK ({res.status_code})")
        return True
    except requests.exceptions.RequestException as e:
        err_msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            err_msg += f" - {e.response.text}"
        print(f"  ❌ Push failed: {err_msg}")
        return False
    except Exception as e:
        print(f"  ❌ Push failed: {e}")
        return False


# ─── Main Loop ───────────────────────────────────────────────────────────────

# Bridge exits after this many seconds with no fresh snapshot.
# The wrapper re-launches it automatically next time Claude Code starts.
IDLE_SHUTDOWN_SECONDS = 10 * 60  # 10 minutes


def run(config, *, once=False, preview=False, debug=False):
    interval = config.get("interval_seconds", 60)

    print("╔══════════════════════════════════════╗")
    print("║   Claude Code → E-Ink Bridge  v1.4   ║")
    print("╚══════════════════════════════════════╝")
    print(f"  Device  : {config['mac_address']}")
    print(f"  Page    : {config['page_id']}")
    print(f"  Interval: {interval}s")
    print(f"  Idle off: {IDLE_SHUTDOWN_SECONDS // 60}min")

    if config["render_mode"] == "auto":
        config["render_mode"] = detect_render_mode(config)
    print(f"  Render  : {config['render_mode']}")
    print(f"  Snapshots: {SNAPSHOTS_DIR}")
    print()

    renderer = EinkRenderer(
        config["font_path"],
        greeting=config.get("greeting", "今天的Token用完了吗？"),
        render_mode=config["render_mode"],
    )

    if preview:
        snapshot, active, _ = scan_snapshots()
        if debug:
            print(f"  [DEBUG] Loaded snapshot: {snapshot}")
        print("  Generating preview...")
        img = renderer.render(snapshot, active)
        out = Path(__file__).parent / "preview-local.png"
        img.save(str(out))
        print(f"  Preview saved to {out}")
        return

    last_hash = None

    while True:
        now = datetime.now().strftime("%H:%M:%S")
        snapshot, active, idle_age = scan_snapshots()

        if debug:
            print(f"  [DEBUG] Loaded snapshot: {snapshot}")

        if idle_age > IDLE_SHUTDOWN_SECONDS:
            print(
                f"  [{now}] No fresh snapshots for "
                f"{int(idle_age // 60)}min — shutting down."
            )
            print("  (Will restart automatically when Claude Code runs.)")
            return

        snap_hash = json.dumps(snapshot, sort_keys=True) if snapshot else None
        session_info = (
            f" ({active} active session{'s' if active != 1 else ''})"
            if active > 0 else ""
        )

        if snap_hash != last_hash:
            print(f"  [{now}] Data changed — rendering & pushing...{session_info}")
            img = renderer.render(snapshot, active)
            push_to_device(img, config)
            last_hash = snap_hash
        else:
            print(f"  [{now}] No change — skipping{session_info}")

        if once:
            return

        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Claude Code E-Ink Bridge")
    parser.add_argument("--once", action="store_true", help="Push once then exit")
    parser.add_argument(
        "--preview", action="store_true",
        help="Generate preview.png without pushing",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Print raw snapshot data and debug info",
    )
    parser.add_argument(
        "--mode", choices=("auto", "gray", "mono"),
        help="Override config.json's render_mode for this run",
    )
    args = parser.parse_args()

    config = load_config()
    if args.mode:
        config["render_mode"] = args.mode

    if not os.path.exists(config["font_path"]):
        print(f"❌ Font not found: {config['font_path']}")
        print("   Please make sure the font file exists at the specified path.")
        print("   Tip: You can use an absolute path in config.json's 'font_path'.")
        sys.exit(1)

    if not args.preview:
        write_pid()
        atexit.register(remove_pid)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    run(config, once=args.once, preview=args.preview, debug=args.debug)


if __name__ == "__main__":
    main()
