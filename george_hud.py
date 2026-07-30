#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
george_hud.py -- the instruments.

GTK's CSS parser has no @keyframes, so every moving part of the HUD is
drawn in cairo on a Gtk.DrawingArea and ticked from a GLib timeout.  The
timeouts start on "map" and stop on "unmap", so nothing spins while it is
off screen or while the window is closed.

pycairo is an optional dependency here on purpose.  If it is missing --
a bare python-gobject install on Arch, for instance -- every widget in
this module degrades to a plain GTK equivalent instead of taking the
window down with it.  Check HAVE_CAIRO before assuming a real ring.
"""

from __future__ import annotations

import math
import re
from collections import deque
from typing import Any, Callable, Deque, List, Tuple

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

try:
    import cairo                                    # noqa: F401
    HAVE_CAIRO = True
except Exception:                                   # pragma: no cover
    HAVE_CAIRO = False

RGB = Tuple[float, float, float]

_TAU = math.pi * 2.0


# =====================================================================
# BASE  --  a drawing area that only animates while it is visible
# =====================================================================

class _Animated(Gtk.DrawingArea):

    def __init__(self, fps: int = 15, animate: bool = True) -> None:
        super().__init__()
        self._fps = max(1, min(30, int(fps)))
        self._animate = bool(animate)
        self._tick_id = 0
        self._phase = 0.0
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        self.connect("destroy", self._on_unmap)

    def set_animate(self, on: bool) -> None:
        self._animate = bool(on)
        if self._animate:
            self._on_map()
        else:
            self._on_unmap()
            self.queue_draw()

    def _on_map(self, *_a: Any) -> None:
        if self._tick_id or not self._animate:
            return
        self._tick_id = GLib.timeout_add(int(1000 / self._fps), self._tick)

    def _on_unmap(self, *_a: Any) -> None:
        if self._tick_id:
            try:
                GLib.source_remove(self._tick_id)
            except Exception:
                pass
            self._tick_id = 0

    def _tick(self) -> bool:
        self._phase = (self._phase + 1.0 / self._fps) % 3600.0
        self.queue_draw()
        return True


def _arc(cr: Any, cx: float, cy: float, r: float, a0: float, a1: float,
         width: float, colour: RGB, alpha: float = 1.0,
         cap_round: bool = True) -> None:
    cr.save()
    cr.set_line_width(width)
    if cap_round and HAVE_CAIRO:
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_source_rgba(colour[0], colour[1], colour[2], alpha)
    cr.arc(cx, cy, max(0.5, r), a0, a1)
    cr.stroke()
    cr.restore()


def _rounded_rect(cr: Any, x: float, y: float, w: float, h: float,
                  r: float) -> None:
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


# =====================================================================
# REACTOR CORE  --  the one piece of theatre in the whole app
#
# Idles with a slow sweep, spins up while he is being answered, breathes
# while George is talking, and goes flat red when the engine is down.
# The inner ring doubles as the CPU gauge so it is an instrument, not a
# decoration.
# =====================================================================

_STATE_SPEED = {"idle": 0.10, "busy": 0.85, "speaking": 0.34,
                "listening": 0.55, "down": 0.0}


class ReactorCore(_Animated):

    def __init__(self, accent: RGB, bad: RGB, dim: RGB,
                 animate: bool = True, size: int = 132) -> None:
        super().__init__(fps=18, animate=animate)
        self.accent = accent
        self.bad = bad
        self.dim = dim
        self.state = "idle"
        self.load = 0.0
        self.caption = "IDLE"
        self.set_content_height(size)
        self.set_content_width(size)
        self.set_draw_func(self._draw)

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state if state in _STATE_SPEED else "idle"
            self.queue_draw()

    def set_load(self, frac: float) -> None:
        try:
            self.load = min(1.0, max(0.0, float(frac)))
        except (TypeError, ValueError):
            self.load = 0.0

    def set_caption(self, text: str) -> None:
        self.caption = (text or "")[:12].upper()
        self.queue_draw()

    # -- drawing ------------------------------------------------------
    def _draw(self, _area: Any, cr: Any, w: int, h: int, *_a: Any) -> None:
        try:
            self._paint(cr, w, h)
        except Exception:
            # A broken frame must never bring the window down; the next
            # tick gets another go.
            pass

    def _paint(self, cr: Any, w: int, h: int) -> None:
        cx, cy = w / 2.0, h / 2.0
        r = min(cx, cy) - 3.0
        down = self.state == "down"
        col = self.bad if down else self.accent
        spin = self._phase * _TAU * _STATE_SPEED.get(self.state, 0.1)
        breathe = 0.5 + 0.5 * math.sin(self._phase * 2.2)
        if self.state == "speaking":
            glow = 0.42 + 0.46 * breathe
        elif self.state == "busy":
            glow = 0.58 + 0.26 * breathe
        elif down:
            glow = 0.30
        else:
            # Idle is what he looks at all day on a quiet box, so it has
            # to read as powered-on rather than switched-off.
            glow = 0.34 + 0.12 * breathe

        # soft core glow
        if HAVE_CAIRO:
            grad = cairo.RadialGradient(cx, cy, r * 0.05, cx, cy, r)
            grad.add_color_stop_rgba(0.0, col[0], col[1], col[2], glow * 0.85)
            grad.add_color_stop_rgba(0.55, col[0], col[1], col[2], glow * 0.18)
            grad.add_color_stop_rgba(1.0, col[0], col[1], col[2], 0.0)
            cr.set_source(grad)
            cr.arc(cx, cy, r, 0, _TAU)
            cr.fill()

        # bezel -- always there, so the instrument has an edge even with
        # nothing happening
        _arc(cr, cx, cy, r * 0.97, 0, _TAU, 1.2, col,
             0.20 if down else 0.42, cap_round=False)

        # tick ring -- 48 ticks, lit up to the load fraction.  Unlit ticks
        # are accent-tinted rather than grey; a ring of dead grey dashes
        # is what made the first pass look switched off.
        lit = int(round(self.load * 48))
        for i in range(48):
            a = (i / 48.0) * _TAU - math.pi / 2
            inner = r * 0.79
            outer = r * (0.94 if i % 4 == 0 else 0.88)
            on = i < lit and not down
            cr.save()
            cr.set_line_width(2.0 if i % 4 == 0 else 1.2)
            if on:
                cr.set_source_rgba(col[0], col[1], col[2], 0.98)
            elif down:
                cr.set_source_rgba(self.dim[0], self.dim[1], self.dim[2], 0.30)
            else:
                cr.set_source_rgba(col[0], col[1], col[2],
                                   0.34 if i % 4 == 0 else 0.24)
            cr.move_to(cx + inner * math.cos(a), cy + inner * math.sin(a))
            cr.line_to(cx + outer * math.cos(a), cy + outer * math.sin(a))
            cr.stroke()
            cr.restore()

        # rotating sweep -- three arcs at different radii and speeds
        if not down:
            _arc(cr, cx, cy, r * 0.71, spin, spin + 1.35, 3.2, col, 0.95)
            _arc(cr, cx, cy, r * 0.71, spin + math.pi, spin + math.pi + 0.70,
                 3.2, col, 0.62)
            _arc(cr, cx, cy, r * 0.60, -spin * 1.6, -spin * 1.6 + 2.3,
                 2.0, col, 0.66)
        else:
            _arc(cr, cx, cy, r * 0.71, 0, _TAU, 2.4, self.bad, 0.40)

        # load arc -- a hair of it always shows, so the gauge reads as a
        # gauge at 0% instead of looking broken
        _arc(cr, cx, cy, r * 0.47, 0, _TAU, 4.4, col, 0.16, cap_round=False)
        if not down:
            start = -math.pi / 2
            span = max(0.05, self.load * _TAU)
            _arc(cr, cx, cy, r * 0.47, start, start + span, 4.4, col, 0.98)

        # inner disc
        cr.save()
        cr.set_source_rgba(col[0], col[1], col[2], 0.16 + 0.12 * breathe)
        cr.arc(cx, cy, r * 0.36, 0, _TAU)
        cr.fill()
        cr.restore()
        _arc(cr, cx, cy, r * 0.36, 0, _TAU, 1.2, col, 0.70, cap_round=False)

        # readout
        pct = "%d%%" % int(round(self.load * 100))
        cr.save()
        cr.select_font_face("monospace")
        cr.set_font_size(max(11.0, r * 0.30))
        ext = cr.text_extents(pct)
        cr.set_source_rgba(1, 1, 1, 0.92 if not down else 0.5)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing,
                   cy - ext.height / 2 - ext.y_bearing - r * 0.06)
        cr.show_text(pct)

        cr.set_font_size(max(7.0, r * 0.14))
        ext = cr.text_extents(self.caption)
        cr.set_source_rgba(col[0], col[1], col[2], 0.85)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy + r * 0.24)
        cr.show_text(self.caption)
        cr.restore()


# =====================================================================
# RING GAUGE  --  small circular percentage
# =====================================================================

class RingGauge(_Animated):

    def __init__(self, label: str, accent: RGB, dim: RGB,
                 size: int = 62) -> None:
        super().__init__(fps=8, animate=False)
        self.accent = accent
        self.dim = dim
        self.label = label
        self.value = 0.0
        self._shown = 0.0
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_draw_func(self._draw)

    def set_value(self, frac: float) -> None:
        try:
            self.value = min(1.0, max(0.0, float(frac)))
        except (TypeError, ValueError):
            self.value = 0.0
        self.queue_draw()

    def _draw(self, _a: Any, cr: Any, w: int, h: int, *_x: Any) -> None:
        try:
            # ease toward the target so the needle never snaps
            self._shown += (self.value - self._shown) * 0.5
            cx, cy = w / 2.0, h / 2.0
            r = min(cx, cy) - 4.0
            col = self.accent
            if self.value >= 0.90:
                col = (1.0, 0.35, 0.39)
            elif self.value >= 0.75:
                col = (0.96, 0.65, 0.14)
            _arc(cr, cx, cy, r, 0, _TAU, 4.0, self.dim, 0.18, cap_round=False)
            start = -math.pi / 2
            if self._shown > 0.004:
                _arc(cr, cx, cy, r, start, start + self._shown * _TAU,
                     4.0, col, 0.95)
            cr.save()
            cr.select_font_face("monospace")
            cr.set_font_size(max(9.0, r * 0.52))
            txt = "%d" % int(round(self.value * 100))
            ext = cr.text_extents(txt)
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.move_to(cx - ext.width / 2 - ext.x_bearing,
                       cy - ext.height / 2 - ext.y_bearing - 1)
            cr.show_text(txt)
            cr.set_font_size(max(6.5, r * 0.28))
            ext = cr.text_extents(self.label)
            cr.set_source_rgba(col[0], col[1], col[2], 0.85)
            cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy + r * 0.62)
            cr.show_text(self.label)
            cr.restore()
        except Exception:
            pass


# =====================================================================
# SPARKLINE  --  short history, gradient fill
# =====================================================================

class Sparkline(Gtk.DrawingArea):

    def __init__(self, accent: RGB, dim: RGB, points: int = 60,
                 height: int = 34) -> None:
        super().__init__()
        self.accent = accent
        self.dim = dim
        self.history: Deque[float] = deque([0.0] * points, maxlen=points)
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def push(self, value: float) -> None:
        try:
            self.history.append(min(1.0, max(0.0, float(value))))
        except (TypeError, ValueError):
            self.history.append(0.0)
        self.queue_draw()

    def _draw(self, _a: Any, cr: Any, w: int, h: int, *_x: Any) -> None:
        try:
            n = len(self.history)
            if n < 2 or w <= 2 or h <= 2:
                return
            pad = 2.0
            step = (w - pad * 2) / float(n - 1)
            pts = [(pad + i * step, h - pad - v * (h - pad * 2))
                   for i, v in enumerate(self.history)]
            col = self.accent

            # baseline
            cr.save()
            cr.set_line_width(1.0)
            cr.set_source_rgba(self.dim[0], self.dim[1], self.dim[2], 0.16)
            cr.move_to(0, h - 1)
            cr.line_to(w, h - 1)
            cr.stroke()
            cr.restore()

            # fill
            if HAVE_CAIRO:
                grad = cairo.LinearGradient(0, 0, 0, h)
                grad.add_color_stop_rgba(0.0, col[0], col[1], col[2], 0.40)
                grad.add_color_stop_rgba(1.0, col[0], col[1], col[2], 0.02)
                cr.save()
                cr.move_to(pts[0][0], h)
                for x, y in pts:
                    cr.line_to(x, y)
                cr.line_to(pts[-1][0], h)
                cr.close_path()
                cr.set_source(grad)
                cr.fill()
                cr.restore()

            # line
            cr.save()
            cr.set_line_width(1.6)
            if HAVE_CAIRO:
                cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.set_source_rgba(col[0], col[1], col[2], 0.95)
            cr.move_to(*pts[0])
            for x, y in pts[1:]:
                cr.line_to(x, y)
            cr.stroke()
            cr.restore()

            # head dot
            cr.save()
            cr.set_source_rgba(col[0], col[1], col[2], 1.0)
            cr.arc(pts[-1][0], pts[-1][1], 2.2, 0, _TAU)
            cr.fill()
            cr.restore()
        except Exception:
            pass


# =====================================================================
# METER  --  rounded bar with a soft glow
# =====================================================================

class Meter(Gtk.DrawingArea):

    def __init__(self, accent: RGB, dim: RGB, height: int = 8) -> None:
        super().__init__()
        self.accent = accent
        self.dim = dim
        self.value = 0.0
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def set_value(self, frac: float) -> None:
        try:
            self.value = min(1.0, max(0.0, float(frac)))
        except (TypeError, ValueError):
            self.value = 0.0
        self.queue_draw()

    def _draw(self, _a: Any, cr: Any, w: int, h: int, *_x: Any) -> None:
        try:
            r = h / 2.0
            cr.save()
            _rounded_rect(cr, 0, 0, w, h, r)
            cr.set_source_rgba(self.dim[0], self.dim[1], self.dim[2], 0.20)
            cr.fill()
            cr.restore()
            if self.value <= 0.004:
                return
            col = self.accent
            if self.value >= 0.90:
                col = (1.0, 0.35, 0.39)
            elif self.value >= 0.75:
                col = (0.96, 0.65, 0.14)
            cr.save()
            _rounded_rect(cr, 0, 0, max(h, w * self.value), h, r)
            if HAVE_CAIRO:
                grad = cairo.LinearGradient(0, 0, w, 0)
                grad.add_color_stop_rgba(0.0, col[0], col[1], col[2], 0.55)
                grad.add_color_stop_rgba(1.0, col[0], col[1], col[2], 1.0)
                cr.set_source(grad)
            else:
                cr.set_source_rgba(col[0], col[1], col[2], 0.9)
            cr.fill()
            cr.restore()
        except Exception:
            pass


# =====================================================================
# LEVEL DOTS  --  tiny state indicator used in the header pill
# =====================================================================

class StateDot(_Animated):

    def __init__(self, accent: RGB, size: int = 10) -> None:
        super().__init__(fps=12, animate=True)
        self.accent = accent
        self.colour = accent
        self.pulsing = False
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_draw_func(self._draw)

    def set_colour(self, colour: RGB, pulsing: bool = False) -> None:
        self.colour = colour
        self.pulsing = bool(pulsing)
        self.queue_draw()

    def _draw(self, _a: Any, cr: Any, w: int, h: int, *_x: Any) -> None:
        try:
            cx, cy = w / 2.0, h / 2.0
            r = min(cx, cy) - 1.0
            c = self.colour
            alpha = 1.0
            halo = 0.35
            if self.pulsing:
                b = 0.5 + 0.5 * math.sin(self._phase * 5.0)
                alpha = 0.55 + 0.45 * b
                halo = 0.15 + 0.45 * b
            cr.set_source_rgba(c[0], c[1], c[2], halo)
            cr.arc(cx, cy, r, 0, _TAU)
            cr.fill()
            cr.set_source_rgba(c[0], c[1], c[2], alpha)
            cr.arc(cx, cy, max(1.0, r * 0.55), 0, _TAU)
            cr.fill()
        except Exception:
            pass


# =====================================================================
# RICH TEXT
#
# A deliberately small markdown subset rendered into Pango markup: bold,
# italic, inline code, links, headings, bullets.  Fenced code comes out
# as its own card with a copy button.  Every conversion is validated
# with Pango before it reaches a label -- bad markup silently becomes
# plain text rather than an empty bubble.
# =====================================================================

_FENCE = re.compile(r"```([A-Za-z0-9_+-]*)\n?(.*?)```", re.S)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITAL = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
_CODE = re.compile(r"`([^`\n]+?)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*(\d{1,2})[.)]\s+(.*)$")


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))


def md_to_pango(text: str, code_colour: str = "#9fe9ff",
                dim_colour: str = "#8ea1b4") -> str:
    """Inline markdown -> Pango markup.  Input is escaped first, so a
    reply full of shell redirects cannot break the label."""
    out_lines: List[str] = []
    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        head = _HEAD.match(line)
        bullet = _BULLET.match(line)
        number = _NUMBERED.match(line)
        if head:
            body = _esc(head.group(2))
            line = "<b>%s</b>" % body
        elif bullet:
            line = "  <span foreground='%s'>%s</span> %s" % (
                dim_colour, "&#8226;", _esc(bullet.group(1)))
        elif number:
            line = "  <span foreground='%s'>%s.</span> %s" % (
                dim_colour, _esc(number.group(1)), _esc(number.group(2)))
        else:
            line = _esc(line)
        out_lines.append(line)
    s = "\n".join(out_lines)

    s = _LINK.sub(lambda m: "<a href='%s'>%s</a>" % (
        m.group(2).replace("'", "%27"), m.group(1)), s)
    s = _BOLD.sub(lambda m: "<b>%s</b>" % m.group(1), s)
    s = _ITAL.sub(lambda m: "<i>%s</i>" % m.group(1), s)
    s = _CODE.sub(lambda m: "<tt><span foreground='%s'>%s</span></tt>"
                  % (code_colour, m.group(1)), s)
    return s


_A_OPEN = re.compile(r"<a\b[^>]*>")
_A_CLOSE = re.compile(r"</a>")


def validate_markup(markup: str) -> bool:
    """True if Pango will accept this.

    GtkLabel understands <a href> and turns it into a real clickable
    link, but Pango's own parser does not know the tag -- so links are
    swapped for spans before validating.  Without this, every reply
    containing a URL would fail the check and lose all its formatting.
    """
    probe = _A_CLOSE.sub("</span>", _A_OPEN.sub("<span>", markup))
    try:
        Pango.parse_markup(probe, -1, "\x00")
        return True
    except Exception:
        return False


def safe_markup(label: Gtk.Label, text: str, code_colour: str = "#9fe9ff",
                dim_colour: str = "#8ea1b4") -> None:
    """set_markup, but a malformed conversion falls back to plain text."""
    markup = md_to_pango(text, code_colour, dim_colour)
    if not validate_markup(markup):
        label.set_text(text)
        return
    try:
        label.set_markup(markup)
    except Exception:
        label.set_text(text)


def split_code_blocks(text: str) -> List[Tuple[str, str, str]]:
    """-> [(kind, language, body)] where kind is 'text' or 'code'."""
    parts: List[Tuple[str, str, str]] = []
    pos = 0
    for m in _FENCE.finditer(text or ""):
        before = text[pos:m.start()]
        if before.strip():
            parts.append(("text", "", before.strip("\n")))
        parts.append(("code", m.group(1) or "", m.group(2).rstrip("\n")))
        pos = m.end()
    tail = (text or "")[pos:]
    if tail.strip():
        parts.append(("text", "", tail.strip("\n")))
    if not parts:
        parts.append(("text", "", text or ""))
    return parts


def code_card(language: str, body: str, on_copy: Callable[[str], None],
              max_lines: int = 400) -> Gtk.Widget:
    """A fenced block: language tag, copy button, monospace body."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.add_css_class("code-card")

    head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    tag = Gtk.Label(label=(language or "sh").lower())
    tag.set_xalign(0.0)
    tag.add_css_class("code-lang")
    tag.set_hexpand(True)
    head.append(tag)
    copy = Gtk.Button()
    copy.set_icon_name("edit-copy-symbolic")
    copy.add_css_class("flat")
    copy.set_tooltip_text("Copy this block")
    copy.connect("clicked", lambda *_a: on_copy(body))
    head.append(copy)
    box.append(head)

    lines = body.split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["[... %d more lines]"
                                     % (len(body.split("\n")) - max_lines)]
    lbl = Gtk.Label(label="\n".join(lines))
    lbl.set_xalign(0.0)
    lbl.set_selectable(True)
    lbl.set_wrap(True)
    lbl.set_wrap_mode(Pango.WrapMode.CHAR)
    lbl.add_css_class("code-text")
    box.append(lbl)
    return box
