#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
george_theme.py -- the look.

Palette definitions and the GTK4 stylesheet, built as a string so the
whole thing is testable without a display.  Two hard rules carried over
from Basilisk and kept here:

  * ASCII only.  The sheet is handed to GTK as bytes.
  * No @keyframes.  GTK's CSS parser does not implement them; every bit
    of motion in George is drawn in cairo instead (see george_hud.py).

Also avoided on purpose, because GTK4's parser does not support them:
CSS variables, transform, filter, backdrop-filter, flex and grid.
Sizing is min-width / min-height, never width / height.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

# =====================================================================
# PALETTE
#
# Neutrals are fixed -- a near-black instrument panel with three lifted
# surfaces.  Only the accent moves, so he can retune the whole HUD from
# Settings without any of the contrast maths falling apart.
# =====================================================================

NEUTRAL: Dict[str, str] = {
    "void":      "#05070a",   # window
    "plate":     "#080d13",   # sidebar
    "card":      "#0e151d",   # card body
    "raised":    "#141d27",   # hovered / nested
    "line":      "#1c2836",   # borders
    "line_soft": "#131c25",   # hairlines inside cards
    "text":      "#e8f2fb",
    "dim":       "#8ea1b4",
    "faint":     "#5a6b7d",
    "ok":        "#3ddc84",
    "warn":      "#f5a524",
    "bad":       "#ff5a63",
}

# name -> (accent, light, deep, ghost)
ACCENTS: Dict[str, Tuple[str, str, str, str]] = {
    "cyan":   ("#35c9f0", "#9fe9ff", "#0b4a5e", "#0d2029"),
    "amber":  ("#ffb52e", "#ffdf9c", "#5e3f06", "#241b0c"),
    "violet": ("#a78bfa", "#dbcdff", "#3a2a70", "#181231"),
    "green":  ("#3ddc84", "#a6f7ca", "#0f5535", "#0b2118"),
    "red":    ("#ff4d55", "#ffa8ac", "#66151a", "#26100f"),
    "white":  ("#dbe7f2", "#ffffff", "#3d4a57", "#171d24"),
}

DEFAULT_ACCENT = "cyan"


def accent_of(name: str) -> Tuple[str, str, str, str]:
    """Never raises -- an unknown or corrupt accent name falls back."""
    return ACCENTS.get(str(name or "").strip().lower(),
                       ACCENTS[DEFAULT_ACCENT])


def rgb(hex_colour: str) -> Tuple[float, float, float]:
    """'#35c9f0' -> (0.207, 0.788, 0.941), for cairo."""
    h = str(hex_colour or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        h = "35c9f0"
    try:
        return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
                int(h[4:6], 16) / 255.0)
    except ValueError:                                  # pragma: no cover
        return (0.207, 0.788, 0.941)


def palette(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Full colour map for the current config, neutrals included."""
    acc, light, deep, ghost = accent_of(cfg.get("accent", DEFAULT_ACCENT))
    p = dict(NEUTRAL)
    p.update({"accent": acc, "accent_light": light,
              "accent_deep": deep, "accent_ghost": ghost})
    return p


# =====================================================================
# STYLESHEET
# =====================================================================

_FONT_STACK = ("'Inter', 'Cantarell', 'Segoe UI Variable Text', "
               "'Segoe UI', 'Noto Sans', sans-serif")
_MONO_STACK = ("'JetBrains Mono', 'Fira Mono', 'Cascadia Mono', "
               "'Consolas', 'DejaVu Sans Mono', monospace")


def _px(value: float, scale: float, floor: int = 9) -> int:
    return max(floor, int(round(value * scale)))


def build_css(cfg: Dict[str, Any]) -> bytes:
    """Return the whole stylesheet as ASCII bytes.

    Everything that depends on config -- accent, font scale, density --
    is resolved here, so reloading the theme is one call and one
    provider swap.
    """
    try:
        scale = float(cfg.get("font_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        scale = 1.0
    scale = min(2.0, max(0.75, scale))
    compact = str(cfg.get("ui_density", "comfortable")).lower() == "compact"

    p = palette(cfg)
    f = _px(15, scale)          # body
    fs = _px(13, scale)         # small
    fx = _px(11, scale)         # eyebrow / label
    fh = _px(17, scale)         # brand
    pad = 8 if compact else 12
    gap = 6 if compact else 10
    radius = 16
    radius_sm = 11

    css = """
/* ---- adwaita overrides: keep built-in controls on our palette ---- */
@define-color accent_color            %(accent)s;
@define-color accent_bg_color         %(accent_deep)s;
@define-color accent_fg_color         #ffffff;
@define-color destructive_color       %(bad)s;
@define-color destructive_bg_color    %(bad)s;
@define-color destructive_fg_color    #ffffff;
@define-color success_color           %(ok)s;
@define-color success_bg_color        %(ok)s;
@define-color success_fg_color        %(void)s;
@define-color warning_color           %(warn)s;
@define-color warning_bg_color        %(warn)s;
@define-color warning_fg_color        %(void)s;
@define-color error_color             %(bad)s;
@define-color window_bg_color         %(void)s;
@define-color window_fg_color         %(text)s;
@define-color view_bg_color           %(card)s;
@define-color view_fg_color           %(text)s;
@define-color headerbar_bg_color      %(plate)s;
@define-color headerbar_fg_color      %(text)s;
@define-color popover_bg_color        %(card)s;
@define-color popover_fg_color        %(text)s;
@define-color dialog_bg_color         %(card)s;
@define-color dialog_fg_color         %(text)s;
@define-color card_bg_color           %(card)s;
@define-color sidebar_bg_color        %(plate)s;
@define-color borders                 %(line)s;

/* ---- shell ---- */
window, .background {
    background-color: %(void)s;
    color: %(text)s;
    font-family: %(font)s;
    font-size: %(f)dpx;
}

headerbar {
    background-image: linear-gradient(to bottom, %(plate)s, %(void)s);
    border-bottom: 1px solid %(line)s;
    min-height: %(hbh)dpx;
    padding: 0 8px;
}

headerbar button, .pill-btn {
    border-radius: 999px;
    min-width: 34px;
    min-height: 34px;
    padding: 0 6px;
    color: %(dim)s;
    background-image: none;
    background-color: transparent;
    border: 1px solid transparent;
    transition: background-color 140ms ease, color 140ms ease,
                border-color 140ms ease;
}

headerbar button:hover, .pill-btn:hover {
    background-color: %(raised)s;
    border-color: %(line)s;
    color: %(text)s;
}

headerbar button:active, headerbar button:checked, .pill-btn:checked {
    background-color: %(accent_ghost)s;
    border-color: %(accent_deep)s;
    color: %(accent)s;
}

/* ---- brand ---- */
.brand {
    font-family: %(mono)s;
    font-weight: 700;
    letter-spacing: 3px;
    color: %(text)s;
    font-size: %(fh)dpx;
}

.brand-sub {
    font-family: %(mono)s;
    font-size: %(fx)dpx;
    color: %(faint)s;
    letter-spacing: 1px;
}

/* ---- status pill in the header ---- */
.status-pill {
    background-color: %(card)s;
    border: 1px solid %(line)s;
    border-radius: 999px;
    padding: 3px 12px;
}

.status-text {
    font-family: %(mono)s;
    font-size: %(fx)dpx;
    letter-spacing: 1px;
    color: %(dim)s;
}

/* ---- sidebar ---- */
.sidebar-pane {
    background-image: linear-gradient(to bottom, %(plate)s, %(void)s);
    border-right: 1px solid %(line)s;
}

.hud-card {
    background-image: linear-gradient(to bottom, %(card)s, %(plate)s);
    border: 1px solid %(line)s;
    border-radius: %(radius)dpx;
    padding: %(pad)dpx %(pad)dpx;
}

.hud-card:hover { border-color: %(accent_deep)s; }

.core-card {
    background-image: linear-gradient(to bottom, %(accent_ghost)s, %(plate)s);
    border: 1px solid %(accent_deep)s;
    border-radius: %(radius)dpx;
    padding: %(pad)dpx;
}

.hud-title {
    font-family: %(mono)s;
    font-size: %(fx)dpx;
    font-weight: 700;
    letter-spacing: 2px;
    color: %(accent)s;
}

.hud-key   { font-size: %(fs)dpx; color: %(dim)s; }
.hud-val   { font-family: %(mono)s; font-size: %(fs)dpx; color: %(text)s; }
.hud-big   { font-family: %(mono)s; font-size: %(fbig)dpx; color: %(text)s;
             font-weight: 700; }
.hud-unit  { font-family: %(mono)s; font-size: %(fx)dpx; color: %(faint)s; }

/* ---- rows that behave like buttons ---- */
.row-btn {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: %(radius_sm)dpx;
    padding: %(gap)dpx 8px;
    transition: background-color 140ms ease, border-color 140ms ease;
}

.row-btn:hover {
    background-color: %(raised)s;
    border-color: %(line)s;
}

.news-title { font-size: %(fs)dpx; color: %(text)s; }

.news-src {
    font-family: %(mono)s;
    font-size: %(fxx)dpx;
    letter-spacing: 1px;
    color: %(accent)s;
}

/* ---- chat bubbles ---- */
.bubble-user {
    background-image: linear-gradient(to bottom, %(accent_deep)s,
                                      %(accent_ghost)s);
    border: 1px solid %(accent_deep)s;
    border-radius: %(radius)dpx %(radius)dpx 4px %(radius)dpx;
    padding: 11px 15px;
}

.bubble-ai {
    background-image: linear-gradient(to bottom, %(card)s, %(plate)s);
    border: 1px solid %(line)s;
    border-radius: 4px %(radius)dpx %(radius)dpx %(radius)dpx;
    padding: 12px 16px;
}

.bubble-watch {
    background-image: linear-gradient(to bottom, %(accent_ghost)s,
                                      %(plate)s);
    border: 1px solid %(accent_deep)s;
}

.bubble-text { font-size: %(f)dpx; color: %(text)s; }
.bubble-user .bubble-text { color: #ffffff; }

.avatar {
    background-image: linear-gradient(to bottom, %(accent)s, %(accent_deep)s);
    border-radius: 999px;
    min-width: 26px;
    min-height: 26px;
    color: %(void)s;
    font-family: %(mono)s;
    font-size: %(fx)dpx;
    font-weight: 700;
}

.stamp { font-family: %(mono)s; font-size: %(fxx)dpx; color: %(faint)s; }

/* ---- code blocks inside replies ---- */
.code-card {
    background-color: %(void)s;
    border: 1px solid %(line)s;
    border-radius: %(radius_sm)dpx;
    padding: 10px 12px;
}

.code-text {
    font-family: %(mono)s;
    font-size: %(fs)dpx;
    color: %(accent_light)s;
}

.code-lang {
    font-family: %(mono)s;
    font-size: %(fxx)dpx;
    letter-spacing: 1px;
    color: %(faint)s;
}

/* ---- tool trace ---- */
.tool-card {
    background-color: %(plate)s;
    border: 1px solid %(line_soft)s;
    border-radius: 999px;
    padding: 5px 12px;
    font-family: %(mono)s;
    font-size: %(fs)dpx;
    color: %(dim)s;
}

.tool-name { color: %(accent)s; font-family: %(mono)s;
             font-size: %(fs)dpx; font-weight: 700; }

/* ---- the strip above the composer ---- */
.feed-pill {
    background-color: %(card)s;
    border: 1px solid %(line)s;
    border-radius: 999px;
    padding: 4px 14px;
}

.feed-text {
    font-family: %(mono)s;
    font-size: %(fs)dpx;
    color: %(dim)s;
}

.think-text {
    font-family: %(mono)s;
    font-size: %(fxx)dpx;
    color: %(faint)s;
}

/* ---- composer ---- */
.composer {
    background-image: linear-gradient(to bottom, %(card)s, %(plate)s);
    border: 1px solid %(line)s;
    border-radius: %(radius)dpx;
    padding: 6px 6px 6px 14px;
    transition: border-color 160ms ease;
}

.composer:focus-within { border-color: %(accent)s; }

textview, textview text {
    background-color: transparent;
    color: %(text)s;
    font-size: %(f)dpx;
    caret-color: %(accent)s;
}

.send-btn {
    background-image: linear-gradient(to bottom, %(accent)s, %(accent_deep)s);
    color: %(void)s;
    border: none;
    border-radius: 999px;
    min-width: 38px;
    min-height: 38px;
    transition: background-image 160ms ease;
}

.send-btn:hover {
    background-image: linear-gradient(to bottom, %(accent_light)s,
                                      %(accent)s);
}

.send-btn:disabled { background-image: none;
                     background-color: %(raised)s; color: %(faint)s; }

.stop-btn {
    background-image: linear-gradient(to bottom, %(bad)s, #6d1a1f);
    color: #ffffff;
    border: none;
    border-radius: 999px;
    min-width: 38px;
    min-height: 38px;
}

.mic-live {
    background-image: linear-gradient(to bottom, %(bad)s, #6d1a1f);
    color: #ffffff;
    border-radius: 999px;
}

/* ---- empty state ---- */
.hero-title {
    font-family: %(mono)s;
    font-size: %(fhero)dpx;
    font-weight: 700;
    letter-spacing: 6px;
    color: %(text)s;
}

.hero-sub { font-size: %(fs)dpx; color: %(faint)s; }

.chip {
    background-color: %(card)s;
    border: 1px solid %(line)s;
    border-radius: 999px;
    padding: 9px 16px;
    color: %(dim)s;
    font-size: %(fs)dpx;
    transition: background-color 140ms ease, color 140ms ease,
                border-color 140ms ease;
}

.chip:hover {
    background-color: %(accent_ghost)s;
    border-color: %(accent_deep)s;
    color: %(accent_light)s;
}

/* ---- misc ---- */
.dim   { color: %(dim)s; font-size: %(fs)dpx; }
.faint { color: %(faint)s; font-size: %(fxx)dpx; }
.mono  { font-family: %(mono)s; }
.err   { color: %(bad)s; }
.ok    { color: %(ok)s; }
.warnc { color: %(warn)s; }

.section-sep { background-color: %(line_soft)s; min-height: 1px; }

progressbar trough {
    background-color: %(line_soft)s;
    min-height: 6px;
    border-radius: 999px;
}

progressbar progress {
    background-image: linear-gradient(to right, %(accent_deep)s, %(accent)s);
    min-height: 6px;
    border-radius: 999px;
}

scrollbar { background-color: transparent; border: none; }

scrollbar slider {
    background-color: %(line)s;
    border-radius: 999px;
    min-width: 7px;
    min-height: 28px;
}

scrollbar slider:hover { background-color: %(accent_deep)s; }

entry {
    background-color: %(void)s;
    border: 1px solid %(line)s;
    border-radius: %(radius_sm)dpx;
    color: %(text)s;
    padding: 7px 11px;
}

entry:focus { border-color: %(accent)s; }

popover contents {
    background-color: %(card)s;
    border: 1px solid %(line)s;
    border-radius: %(radius)dpx;
    padding: 6px;
}

popover button { border-radius: %(radius_sm)dpx; padding: 7px 12px; }
popover button:hover { background-color: %(raised)s; }

.menu-item { font-size: %(fs)dpx; color: %(text)s; }
.menu-key  { font-family: %(mono)s; font-size: %(fxx)dpx; color: %(faint)s; }

.dialog-body { background-color: %(void)s; }

preferencespage, preferencesgroup { background-color: %(void)s; }

row.activatable:hover { background-color: %(raised)s; }

toast { border-radius: 999px; }

tooltip {
    background-color: %(card)s;
    border: 1px solid %(line)s;
    border-radius: %(radius_sm)dpx;
    color: %(text)s;
}
""" % {
        "accent": p["accent"], "accent_light": p["accent_light"],
        "accent_deep": p["accent_deep"], "accent_ghost": p["accent_ghost"],
        "void": p["void"], "plate": p["plate"], "card": p["card"],
        "raised": p["raised"], "line": p["line"], "line_soft": p["line_soft"],
        "text": p["text"], "dim": p["dim"], "faint": p["faint"],
        "ok": p["ok"], "warn": p["warn"], "bad": p["bad"],
        "font": _FONT_STACK, "mono": _MONO_STACK,
        "f": f, "fs": fs, "fx": fx, "fxx": _px(10, scale),
        "fh": fh, "fbig": _px(26, scale), "fhero": _px(30, scale),
        "hbh": 54 if compact else 60,
        "pad": pad, "gap": gap,
        "radius": radius, "radius_sm": radius_sm,
    }

    # The sheet is handed to GTK as bytes; anything non-ASCII that ever
    # sneaks in gets dropped rather than taking the whole theme down.
    return css.encode("ascii", "ignore")


FALLBACK_CSS = (b"window, .background { background-color: #05070a; "
                b"color: #e8f2fb; }\n"
                b".hud-card { border: 1px solid #1c2836; border-radius: 12px; "
                b"padding: 10px; }\n"
                b".bubble-ai, .bubble-user { border: 1px solid #1c2836; "
                b"border-radius: 12px; padding: 10px; }\n")
