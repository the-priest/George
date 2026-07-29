#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
george.py -- the GTK4 / libadwaita shell.

Basilisk's brother: same window, same live action feed, same voice
stack, same blunt tone.  No security tooling anywhere in it.  The brain
is a local Ollama model and there is no API key in this program.
"""

from __future__ import annotations

import os
import re
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gio, Gtk, Pango  # noqa: E402

from george_core import (
    APP_ID, APP_NAME, CONFIG_PATH, CURATED_MODELS, DEFAULT_FEEDS, DEFAULTS,
    NOTES_PATH, VERSION, ChatStore, MemoryStore, ModelManager, Ollama,
    OllamaSupervisor, clipboard_write, fetch_news, install_hint, load_config,
    log, open_in_browser, reasoning_of, save_config, system_status,
    weather,
)
from george_tools import Agent, strip_action_json
from george_voice import SpeechToText, TextToSpeech

# =====================================================================
# THEME
#
# ASCII-only bytes literal, no @keyframes -- same rule as Basilisk's
# stylesheet.  Where Basilisk is blood on black, George is arc-reactor
# cyan on black.  Adwaita named colours are overridden in one place so
# built-in controls stop rendering in the user's desktop accent.
# =====================================================================

CSS = b"""
@define-color accent_color              #35c9f0;
@define-color accent_bg_color           #12556b;
@define-color accent_fg_color           #ffffff;
@define-color destructive_color         #e5484d;
@define-color destructive_bg_color      #e5484d;
@define-color destructive_fg_color      #ffffff;
@define-color success_color             #3ddc84;
@define-color success_bg_color          #3ddc84;
@define-color success_fg_color          #070a0d;
@define-color warning_color             #f0a500;
@define-color warning_bg_color          #f0a500;
@define-color warning_fg_color          #070a0d;
@define-color error_color               #e5484d;
@define-color window_bg_color           #070a0d;
@define-color window_fg_color           #d7e2ec;
@define-color view_bg_color             #0d1218;
@define-color view_fg_color             #d7e2ec;
@define-color headerbar_bg_color        #0d1218;
@define-color headerbar_fg_color        #d7e2ec;
@define-color popover_bg_color          #0d1218;
@define-color dialog_bg_color           #0d1218;
@define-color card_bg_color             #131a22;
@define-color sidebar_bg_color          #090d12;
@define-color borders                   #1e2731;

window, .background {
    background-color: #070a0d;
    color: #d7e2ec;
    font-family: 'Inter', 'Cantarell', sans-serif;
    font-size: 15px;
}

headerbar {
    background-color: #0d1218;
    border-bottom: 1px solid #1e2731;
    min-height: 52px;
}

.brand {
    font-family: 'JetBrains Mono', 'Fira Mono', monospace;
    font-weight: 700;
    letter-spacing: 2px;
    color: #35c9f0;
    font-size: 16px;
}

.brand-sub {
    font-family: 'JetBrains Mono', 'Fira Mono', monospace;
    font-size: 11px;
    color: #7b8a99;
    letter-spacing: 1px;
}

.sidebar-pane {
    background-color: #090d12;
    border-right: 1px solid #1e2731;
}

.hud-card {
    background-color: #0d1218;
    border: 1px solid #1e2731;
    border-radius: 10px;
    padding: 10px 12px;
}

.hud-title {
    font-family: 'JetBrains Mono', 'Fira Mono', monospace;
    font-size: 11px;
    letter-spacing: 2px;
    color: #35c9f0;
}

.hud-key {
    font-size: 12px;
    color: #7b8a99;
}

.hud-val {
    font-family: 'JetBrains Mono', 'Fira Mono', monospace;
    font-size: 12px;
    color: #d7e2ec;
}

.news-row {
    border-bottom: 1px solid #141b23;
    padding: 8px 4px;
}

.news-title {
    font-size: 13px;
    color: #d7e2ec;
}

.news-src {
    font-family: 'JetBrains Mono', 'Fira Mono', monospace;
    font-size: 10px;
    letter-spacing: 1px;
    color: #35c9f0;
}

.bubble-user {
    background-color: #12232b;
    border: 1px solid #1d3a46;
    border-radius: 12px 12px 2px 12px;
    padding: 10px 14px;
}

.bubble-ai {
    background-color: #101820;
    border: 1px solid #1e2731;
    border-left: 2px solid #35c9f0;
    border-radius: 2px 12px 12px 12px;
    padding: 10px 14px;
}

.bubble-text {
    font-size: 15px;
    color: #d7e2ec;
}

.tool-card {
    background-color: #0b1016;
    border: 1px solid #182029;
    border-radius: 8px;
    padding: 6px 10px;
    font-family: 'JetBrains Mono', 'Fira Mono', monospace;
    font-size: 12px;
    color: #7b8a99;
}

.tool-name {
    color: #35c9f0;
    font-family: 'JetBrains Mono', 'Fira Mono', monospace;
    font-size: 12px;
}

.feed-strip {
    background-color: #0b1016;
    border-top: 1px solid #1e2731;
    padding: 4px 12px;
}

.feed-text {
    font-family: 'JetBrains Mono', 'Fira Mono', monospace;
    font-size: 12px;
    color: #7b8a99;
}

.think-text {
    font-family: 'JetBrains Mono', 'Fira Mono', monospace;
    font-size: 11px;
    color: #4e5c69;
}

.input-frame {
    background-color: #0d1218;
    border: 1px solid #1e2731;
    border-radius: 10px;
    padding: 6px 8px;
}

textview, textview text {
    background-color: transparent;
    color: #d7e2ec;
    font-size: 15px;
}

.send-btn {
    background-color: #12556b;
    color: #ffffff;
    border-radius: 8px;
    min-width: 42px;
    min-height: 34px;
}

.send-btn:hover { background-color: #1a6d89; }

.stop-btn {
    background-color: #7a1f22;
    color: #ffffff;
    border-radius: 8px;
    min-width: 42px;
    min-height: 34px;
}

.mic-live {
    background-color: #7a1f22;
    color: #ffffff;
    border-radius: 8px;
}

.dim { color: #7b8a99; font-size: 12px; }
.mono { font-family: 'JetBrains Mono', 'Fira Mono', monospace; }
.err  { color: #e5484d; }
.ok   { color: #3ddc84; }

progressbar trough { background-color: #141b23; min-height: 5px; }
progressbar progress { background-color: #35c9f0; min-height: 5px; }
"""

_FONT_RX = re.compile(rb"font-size:\s*(\d+)px")


def _scale_css(css: bytes, scale: float) -> bytes:
    if abs(scale - 1.0) < 0.01:
        return css

    def sub(m: "re.Match") -> bytes:
        return b"font-size: %dpx" % max(9, int(round(int(m.group(1)) * scale)))
    return _FONT_RX.sub(sub, css)


# =====================================================================
# SMALL WIDGET HELPERS
# =====================================================================

def _label(text: str, css: str = "", wrap: bool = True,
           selectable: bool = False, xalign: float = 0.0) -> Gtk.Label:
    lb = Gtk.Label(label=text)
    lb.set_xalign(xalign)
    lb.set_wrap(wrap)
    lb.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    lb.set_selectable(selectable)
    if css:
        lb.add_css_class(css)
    return lb


def _icon_button(icon: str, tooltip: str = "", css: str = "") -> Gtk.Button:
    btn = Gtk.Button()
    btn.set_icon_name(icon)
    if tooltip:
        btn.set_tooltip_text(tooltip)
    btn.add_css_class("flat")
    if css:
        btn.add_css_class(css)
    return btn


def _card(title: str) -> Tuple[Gtk.Box, Gtk.Box]:
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    outer.add_css_class("hud-card")
    outer.append(_label(title, "hud-title"))
    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    outer.append(body)
    return outer, body


def _kv_row(key: str, value: str) -> Gtk.Box:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    k = _label(key, "hud-key", wrap=False)
    k.set_hexpand(True)
    v = _label(value, "hud-val", wrap=False, xalign=1.0)
    row.append(k)
    row.append(v)
    return row


# =====================================================================
# CONFIRM DIALOG  --  blocking, callable from a worker thread
# =====================================================================

class Confirmer:
    """Worker threads call ask(); the dialog is built on the GTK thread
    and the worker blocks on an Event until he answers."""

    def __init__(self, parent: Gtk.Window) -> None:
        self.parent = parent

    def ask(self, title: str, body: str) -> bool:
        # Blocking on the GTK thread would freeze the loop that has to
        # draw the dialog.  Tools only ever run on a worker, but refuse
        # rather than deadlock if that ever stops being true.
        if threading.current_thread() is threading.main_thread():
            log("confirm called on the main thread; refusing")
            return False
        done = threading.Event()
        result = {"ok": False}

        def build() -> bool:
            win = Adw.Window(transient_for=self.parent, modal=True)
            win.set_default_size(520, -1)
            win.set_title(title)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.set_margin_top(6)
            box.set_margin_bottom(16)
            box.set_margin_start(16)
            box.set_margin_end(16)
            hb = Adw.HeaderBar()
            hb.set_show_end_title_buttons(False)
            hb.set_title_widget(_label(title, "brand", wrap=False))
            box.append(hb)
            box.append(_label(body, "mono", selectable=True))
            btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btns.set_halign(Gtk.Align.END)
            no = Gtk.Button(label="No")
            yes = Gtk.Button(label="Run it")
            yes.add_css_class("suggested-action")
            btns.append(no)
            btns.append(yes)
            box.append(btns)
            win.set_content(box)

            def close(ok: bool) -> None:
                result["ok"] = ok
                win.close()
                done.set()

            no.connect("clicked", lambda *_a: close(False))
            yes.connect("clicked", lambda *_a: close(True))
            win.connect("close-request", lambda *_a: (done.set(), False)[1])
            win.present()
            return False

        GLib.idle_add(build)
        if not done.wait(timeout=300):
            return False
        return result["ok"]


# =====================================================================
# MAIN WINDOW
# =====================================================================

class GeorgeWindow(Adw.ApplicationWindow):

    def __init__(self, app: Adw.Application, cfg: Dict[str, Any],
                 supervisor: OllamaSupervisor) -> None:
        super().__init__(application=app, title=APP_NAME)
        self.cfg = cfg
        self.supervisor = supervisor
        self.models = ModelManager(cfg)
        self.set_default_size(1260, 820)

        self.memory = MemoryStore()
        self.chats = ChatStore(cfg)
        self.tts = TextToSpeech(cfg)
        self.stt = SpeechToText(cfg)
        self.agent = Agent(cfg, self.memory, self.tts)
        self.confirmer = Confirmer(self)

        self.session_id = "s%d" % int(time.time())
        self._live_raw = ""
        self._live_label: Optional[Gtk.Label] = None
        self._pulse_id = 0
        self._stick_bottom = True
        self._recording = False

        self._install_css()
        self._build_ui()
        self._wire_agent()

        GLib.timeout_add_seconds(5, self._refresh_vitals)
        self._refresh_vitals()
        threading.Thread(target=self._startup_probe, daemon=True).start()

    # ---- chrome ------------------------------------------------------
    def _install_css(self) -> None:
        self.css_provider = Gtk.CssProvider()
        try:
            self.css_provider.load_from_data(
                _scale_css(CSS, float(self.cfg.get("font_scale", 1.0))))
        except Exception as exc:
            log("css load failed: %s" % exc)
            return
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def reload_css(self) -> None:
        try:
            self.css_provider.load_from_data(
                _scale_css(CSS, float(self.cfg.get("font_scale", 1.0))))
        except Exception as exc:
            log("css reload failed: %s" % exc)

    def _build_ui(self) -> None:
        self.toasts = Adw.ToastOverlay()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toasts.set_child(root)
        self.set_content(self.toasts)

        root.append(self._build_header())

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_position(330)
        self.paned.set_vexpand(True)
        self.paned.set_start_child(self._build_sidebar())
        self.paned.set_end_child(self._build_chat())
        self.paned.set_resize_start_child(False)
        self.paned.set_shrink_start_child(False)
        root.append(self.paned)

    def _build_header(self) -> Adw.HeaderBar:
        hb = Adw.HeaderBar()

        title = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title.set_halign(Gtk.Align.CENTER)
        title.append(_label("G E O R G E", "brand", wrap=False, xalign=0.5))
        self.subtitle = _label("", "brand-sub", wrap=False, xalign=0.5)
        title.append(self.subtitle)
        hb.set_title_widget(title)

        toggle = _icon_button("sidebar-show-symbolic", "Toggle the HUD")
        toggle.connect("clicked", self._on_toggle_sidebar)
        hb.pack_start(toggle)

        newchat = _icon_button("document-new-symbolic", "New conversation")
        newchat.connect("clicked", self._on_new_chat)
        hb.pack_start(newchat)

        self.mic_btn = _icon_button("audio-input-microphone-symbolic",
                                    "Push to talk")
        self.mic_btn.connect("clicked", self._on_mic)
        if not self.stt.available:
            self.mic_btn.set_sensitive(False)
            self.mic_btn.set_tooltip_text(self.stt.why_unavailable())
        hb.pack_end(self._build_menu())
        hb.pack_end(self.mic_btn)

        self.voice_btn = Gtk.ToggleButton()
        self.voice_btn.set_icon_name("audio-speakers-symbolic")
        self.voice_btn.set_tooltip_text("Speak replies aloud")
        self.voice_btn.add_css_class("flat")
        self.voice_btn.set_active(bool(self.cfg.get("voice_enabled")))
        self.voice_btn.connect("toggled", self._on_voice_toggle)
        hb.pack_end(self.voice_btn)
        return hb

    def _build_menu(self) -> Gtk.MenuButton:
        mb = Gtk.MenuButton()
        mb.set_icon_name("open-menu-symbolic")
        mb.add_css_class("flat")
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        def entry(label: str, cb: Callable) -> None:
            b = Gtk.Button()
            b.set_child(_label(label, "", wrap=False))
            b.add_css_class("flat")
            b.set_halign(Gtk.Align.FILL)
            b.connect("clicked", lambda *_a: (pop.popdown(), cb()))
            box.append(b)

        entry("Models", self._open_models)
        entry("Settings", self._open_settings)
        entry("Recent chats", self._open_history)
        entry("Refresh news", lambda: self._async_news(""))
        entry("Open notes", lambda: open_in_browser("file://" + NOTES_PATH,
                                                    self.cfg))
        entry("About", self._open_about)
        pop.set_child(box)
        mb.set_popover(pop)
        return mb

    # ---- sidebar / HUD ----------------------------------------------
    def _build_sidebar(self) -> Gtk.Widget:
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add_css_class("sidebar-pane")
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        col.set_margin_top(12)
        col.set_margin_bottom(12)
        col.set_margin_start(10)
        col.set_margin_end(10)

        self.clock_lbl = _label("", "brand", wrap=False)
        self.date_lbl = _label("", "dim", wrap=False)
        clock_card, clock_body = _card("LOCAL")
        clock_body.append(self.clock_lbl)
        clock_body.append(self.date_lbl)
        col.append(clock_card)
        GLib.timeout_add_seconds(20, self._tick_clock)
        self._tick_clock()

        vit_card, self.vitals_body = _card("SYSTEM")
        col.append(vit_card)

        wx_card, self.weather_body = _card("WEATHER")
        self.weather_body.append(_label("checking...", "dim"))
        col.append(wx_card)

        eng_card, self.engine_body = _card("ENGINE")
        self.engine_body.append(_label("starting...", "dim"))
        models_btn = Gtk.Button(label="Manage models")
        models_btn.add_css_class("flat")
        models_btn.connect("clicked", lambda *_a: self._open_models())
        eng_card.append(models_btn)
        col.append(eng_card)

        news_card, self.news_body = _card("NEWS")
        refresh = Gtk.Button(label="Refresh")
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_a: self._async_news(""))
        news_card.append(refresh)
        self.news_body.append(_label("loading feeds...", "dim"))
        col.append(news_card)

        chat_card, self.chats_body = _card("CHATS")
        self.chats_hint = _label("", "dim")
        chat_card.append(self.chats_hint)
        col.append(chat_card)
        self.refresh_chats()

        sw.set_child(col)
        return sw

    # ---- saved conversations ----------------------------------------
    def refresh_chats(self) -> None:
        self.chats.purge()
        rows = self.chats.listing()
        self._clear(self.chats_body)
        hours = int(self.cfg.get("chat_retention_hours") or 0)
        self.chats_hint.set_text(
            "auto-deleted after %dh" % hours if hours else "kept until deleted")
        if not rows:
            self.chats_body.append(_label("nothing saved yet", "dim"))
            return
        for sid, title, ts in rows[:14]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            row.add_css_class("news-row")
            open_btn = Gtk.Button()
            open_btn.add_css_class("flat")
            open_btn.set_hexpand(True)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            t = _label(title[:46], "news-title", wrap=False)
            t.set_ellipsize(Pango.EllipsizeMode.END)
            inner.append(t)
            inner.append(_label(time.strftime("%d %b %H:%M",
                                              time.localtime(ts)), "dim",
                                wrap=False))
            open_btn.set_child(inner)
            open_btn.connect("clicked", lambda *_a, s=sid: self._load_session(s))
            row.append(open_btn)
            rm = _icon_button("user-trash-symbolic", "Delete this chat")
            rm.connect("clicked", lambda *_a, s=sid: self._delete_chat(s))
            row.append(rm)
            self.chats_body.append(row)

    def _delete_chat(self, sid: str) -> None:
        self.chats.delete(sid)
        if sid == self.session_id:
            self.agent.reset()
            self._clear(self.transcript)
            self.session_id = "s%d" % int(time.time())
        self.refresh_chats()
        self.toast("chat deleted")

    def _tick_clock(self) -> bool:
        self.clock_lbl.set_text(time.strftime("%H:%M"))
        self.date_lbl.set_text(time.strftime("%A %d %B %Y"))
        return True

    def _clear(self, box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    def _refresh_vitals(self) -> bool:
        st = system_status()
        self.render_vitals(st)
        return True

    def render_vitals(self, st: Dict[str, str]) -> None:
        self._clear(self.vitals_body)
        self.vitals_body.append(_kv_row("host", st.get("host", "?")))
        self.vitals_body.append(_kv_row("uptime", st.get("uptime", "?")))
        self.vitals_body.append(_kv_row("load", st.get("load", "?")))
        if st.get("memory"):
            self.vitals_body.append(_kv_row("memory", st["memory"]))
            bar = Gtk.ProgressBar()
            bar.set_fraction(min(1.0, float(st.get("mem_pct", 0)) / 100.0))
            self.vitals_body.append(bar)
        if st.get("disk"):
            self.vitals_body.append(_kv_row("disk", st["disk"]))
        if st.get("battery"):
            self.vitals_body.append(_kv_row("battery", st["battery"]))
        if st.get("temp"):
            self.vitals_body.append(_kv_row("cpu temp", st["temp"]))

    def render_weather(self, w: Dict[str, str]) -> None:
        self._clear(self.weather_body)
        if w.get("error"):
            self.weather_body.append(_label(w["error"], "dim"))
            return
        self.weather_body.append(_label("%sC  %s" % (w["temp_c"], w["desc"]),
                                        "hud-val"))
        self.weather_body.append(_kv_row("place", w["place"]))
        self.weather_body.append(_kv_row("feels", "%sC" % w["feels_c"]))
        self.weather_body.append(_kv_row("today", "%s - %sC" %
                                         (w["min_c"], w["max_c"])))
        self.weather_body.append(_kv_row("wind", "%s km/h" % w["wind_kph"]))

    def render_news(self, items: List[Dict[str, str]]) -> None:
        self._clear(self.news_body)
        if not items:
            self.news_body.append(_label("no headlines", "dim"))
            return
        for it in items[:int(self.cfg.get("news_count", 12))]:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row.add_css_class("news-row")
            row.append(_label(it.get("source", ""), "news-src", wrap=False))
            row.append(_label(it.get("title", ""), "news-title"))
            url = it.get("url", "")
            if url:
                click = Gtk.GestureClick()
                click.connect("released",
                              lambda *_a, u=url: open_in_browser(u, self.cfg))
                row.add_controller(click)
                row.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
                row.set_tooltip_text("Click to open " + url)
            self.news_body.append(row)

    # ---- chat pane ---------------------------------------------------
    def _build_chat(self) -> Gtk.Widget:
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_vexpand(True)
        self.transcript = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                  spacing=10)
        self.transcript.set_margin_top(16)
        self.transcript.set_margin_bottom(16)
        self.transcript.set_margin_start(18)
        self.transcript.set_margin_end(18)
        self.scroll.set_child(self.transcript)
        adj = self.scroll.get_vadjustment()
        adj.connect("changed", self._on_adj_changed)
        adj.connect("value-changed", self._on_adj_value)
        col.append(self.scroll)

        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        strip.add_css_class("feed-strip")
        self.spinner = Gtk.Spinner()
        strip.append(self.spinner)
        self.feed_lbl = _label("ready", "feed-text", wrap=False)
        strip.append(self.feed_lbl)
        self.think_lbl = _label("", "think-text", wrap=False)
        self.think_lbl.set_hexpand(True)
        self.think_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        strip.append(self.think_lbl)
        col.append(strip)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(10)
        bar.set_margin_bottom(12)
        bar.set_margin_start(18)
        bar.set_margin_end(18)

        frame = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        frame.add_css_class("input-frame")
        frame.set_hexpand(True)
        self.entry = Gtk.TextView()
        self.entry.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.entry.set_hexpand(True)
        self.entry.set_top_margin(6)
        self.entry.set_bottom_margin(6)
        self.entry.set_left_margin(4)
        entry_scroll = Gtk.ScrolledWindow()
        entry_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        entry_scroll.set_min_content_height(38)
        entry_scroll.set_max_content_height(150)
        entry_scroll.set_hexpand(True)
        entry_scroll.set_child(self.entry)
        frame.append(entry_scroll)
        bar.append(frame)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.entry.add_controller(keys)

        self.send_btn = Gtk.Button()
        self.send_btn.set_icon_name("go-up-symbolic")
        self.send_btn.add_css_class("send-btn")
        self.send_btn.set_tooltip_text("Send  (Enter)")
        self.send_btn.set_valign(Gtk.Align.END)
        self.send_btn.connect("clicked", self._on_send_clicked)
        bar.append(self.send_btn)

        col.append(bar)
        return col

    def _on_adj_changed(self, adj: Gtk.Adjustment) -> None:
        if self._stick_bottom:
            adj.set_value(max(0.0, adj.get_upper() - adj.get_page_size()))

    def _on_adj_value(self, adj: Gtk.Adjustment) -> None:
        at_end = (adj.get_value() + adj.get_page_size()) >= \
            (adj.get_upper() - 60)
        self._stick_bottom = at_end

    def _trim_transcript(self) -> None:
        cap = int(self.cfg.get("transcript_live_rows", 40) or 40)
        rows = []
        child = self.transcript.get_first_child()
        while child is not None:
            rows.append(child)
            child = child.get_next_sibling()
        for extra in rows[:-cap] if len(rows) > cap else []:
            self.transcript.remove(extra)

    # ---- transcript rows --------------------------------------------
    def _row(self, widget: Gtk.Widget, align: Gtk.Align) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        widget.set_halign(align)
        row.append(widget)
        self.transcript.append(row)
        self._trim_transcript()
        return row

    def add_user_bubble(self, text: str) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("bubble-user")
        box.set_size_request(120, -1)
        lbl = _label(text, "bubble-text", selectable=True)
        lbl.set_max_width_chars(70)
        box.append(lbl)
        self._row(box, Gtk.Align.END)

    def add_ai_bubble(self, text: str = "") -> Gtk.Label:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("bubble-ai")
        lbl = _label(text, "bubble-text", selectable=True)
        lbl.set_max_width_chars(88)
        box.append(lbl)

        tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        tools.set_halign(Gtk.Align.END)
        play = _icon_button("media-playback-start-symbolic", "Read this aloud")
        play.connect("clicked", lambda *_a: self.tts.speak(lbl.get_text()))
        copy = _icon_button("edit-copy-symbolic", "Copy")
        copy.connect("clicked", lambda *_a: self._copy(lbl.get_text()))
        tools.append(play)
        tools.append(copy)
        box.append(tools)

        self._row(box, Gtk.Align.START)
        return lbl

    def add_tool_card(self, name: str, arg: str, result: str) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.add_css_class("tool-card")
        box.append(_label(name, "tool-name", wrap=False))
        mid = _label(arg[:110], "", wrap=False)
        mid.set_ellipsize(Pango.EllipsizeMode.END)
        mid.set_hexpand(True)
        box.append(mid)
        tail = _label(result[:60], "ok" if "REFUS" not in result.upper()
                      else "err", wrap=False)
        box.append(tail)
        self._row(box, Gtk.Align.START)

    def add_note(self, text: str, css: str = "dim") -> None:
        self._row(_label(text, css), Gtk.Align.START)

    def add_image(self, path: str) -> None:
        try:
            pic = Gtk.Picture.new_for_filename(path)
        except Exception as exc:
            self.add_note("could not show %s: %s" % (path, exc), "err")
            return
        pic.set_can_shrink(True)
        pic.set_size_request(-1, 320)
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        frame.add_css_class("bubble-ai")
        frame.append(pic)
        frame.append(_label(path, "dim", wrap=False))
        self._row(frame, Gtk.Align.START)

    def _copy(self, text: str) -> None:
        try:
            self.get_clipboard().set(text)
            self.toast("copied")
        except Exception:
            clipboard_write(text)

    def toast(self, text: str) -> None:
        try:
            self.toasts.add_toast(Adw.Toast.new(text))
        except Exception:
            pass

    # ---- agent wiring ------------------------------------------------
    def _wire_agent(self) -> None:
        ag = self.agent
        ag.on_step = lambda s: GLib.idle_add(self._ui_step, s)
        ag.on_token = self._collect_token
        ag.on_tool = lambda n, a, r: GLib.idle_add(self.add_tool_card, n, a, r)
        ag.on_final = lambda s: GLib.idle_add(self._ui_final, s)
        ag.on_error = lambda s: GLib.idle_add(self._ui_error, s)
        ag.on_news = lambda i: GLib.idle_add(self.render_news, i)
        ag.on_weather = lambda w: GLib.idle_add(self.render_weather, w)
        ag.on_vitals = lambda v: GLib.idle_add(self.render_vitals, v)
        ag.on_image = lambda p: GLib.idle_add(self.add_image, p)
        ag.on_done = lambda: GLib.idle_add(self._ui_done)
        ag.ask_confirm = self.confirmer.ask
        self.tts.on_state = self._ui_voice_state

    def _collect_token(self, piece: str) -> None:
        self._live_raw += piece

    def _pulse(self) -> bool:
        """Repaint the streaming bubble ~8x a second instead of once per
        token -- a 7B model on a local GPU emits faster than GTK can
        usefully redraw, and this also proves the app is not stuck."""
        raw = self._live_raw
        if raw:
            visible = strip_action_json(raw)
            if self._live_label is None and visible:
                self._live_label = self.add_ai_bubble("")
            if self._live_label is not None:
                self._live_label.set_text(visible)
            tail = reasoning_of(raw).replace("\n", " ")
            self.think_lbl.set_text(tail[-140:] if tail else "")
        return True

    def _ui_step(self, text: str) -> bool:
        self.feed_lbl.set_text(text)
        return False

    def _ui_final(self, text: str) -> bool:
        if self._live_label is not None:
            self._live_label.set_text(text)
            self._live_label = None
        else:
            self.add_ai_bubble(text)
        self._live_raw = ""
        self.think_lbl.set_text("")
        self.feed_lbl.set_text("done")
        self._save_session()
        return False

    def _ui_error(self, text: str) -> bool:
        self.add_note("[%s]" % text, "err")
        self.feed_lbl.set_text("error")
        return False

    def _ui_done(self) -> bool:
        if self._pulse_id:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = 0
        self._live_raw = ""
        self._live_label = None
        self.spinner.stop()
        self.send_btn.set_icon_name("go-up-symbolic")
        self.send_btn.remove_css_class("stop-btn")
        self.send_btn.add_css_class("send-btn")
        self.send_btn.set_tooltip_text("Send  (Enter)")
        if self.feed_lbl.get_text() not in ("error", "stopped"):
            self.feed_lbl.set_text("ready")
        return False

    def _ui_voice_state(self, state: str) -> bool:
        self.voice_btn.set_icon_name(
            "audio-volume-high-symbolic" if state == "speaking"
            else "audio-speakers-symbolic")
        return False

    # ---- sending ------------------------------------------------------
    def _on_key(self, _ctrl, keyval, _code, state) -> bool:
        enter = keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if enter and not shift:
            self._send()
            return True
        return False

    def _on_send_clicked(self, _btn) -> None:
        if self.agent.busy:
            self.agent.stop()
            self.feed_lbl.set_text("stopped")
        else:
            self._send()

    def _send(self, text: str = "") -> None:
        if self.agent.busy:
            return
        buf = self.entry.get_buffer()
        if not text:
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(),
                                False).strip()
        if not text:
            return
        buf.set_text("")
        self.tts.stop()
        self.add_user_bubble(text)
        self._stick_bottom = True

        self.spinner.start()
        self.feed_lbl.set_text("thinking")
        self.send_btn.set_icon_name("media-playback-stop-symbolic")
        self.send_btn.remove_css_class("send-btn")
        self.send_btn.add_css_class("stop-btn")
        self.send_btn.set_tooltip_text("Stop")
        if not self._pulse_id:
            self._pulse_id = GLib.timeout_add(120, self._pulse)
        self.agent.start(text)

    # ---- header actions ------------------------------------------------
    def _on_toggle_sidebar(self, _btn) -> None:
        child = self.paned.get_start_child()
        if child:
            child.set_visible(not child.get_visible())

    def _on_new_chat(self, _btn) -> None:
        self._save_session()
        self.agent.reset()
        self.session_id = "s%d" % int(time.time())
        self._clear(self.transcript)
        self.feed_lbl.set_text("ready")
        self.add_note("new conversation", "dim")

    def _on_voice_toggle(self, btn: Gtk.ToggleButton) -> None:
        self.cfg["voice_enabled"] = btn.get_active()
        save_config(self.cfg)
        if not btn.get_active():
            self.tts.stop()
        self.toast("voice %s" % ("on" if btn.get_active() else "off"))

    def _on_mic(self, _btn) -> None:
        if not self.stt.available:
            self.toast(self.stt.why_unavailable())
            return
        if not self._recording:
            if self.stt.start():
                self._recording = True
                self.mic_btn.add_css_class("mic-live")
                self.feed_lbl.set_text("listening... click the mic to stop")
            return
        self._recording = False
        self.mic_btn.remove_css_class("mic-live")
        self.feed_lbl.set_text("transcribing")

        def work() -> None:
            text = self.stt.stop_and_transcribe()
            GLib.idle_add(self._mic_done, text)

        threading.Thread(target=work, daemon=True).start()

    def _mic_done(self, text: str) -> bool:
        if not text:
            self.feed_lbl.set_text("heard nothing")
            return False
        self.feed_lbl.set_text("ready")
        self._send(text)
        return False

    # ---- background chores ---------------------------------------------
    def _startup_probe(self) -> None:
        model = str(self.cfg.get("model"))
        GLib.idle_add(self._ui_step, "bringing the engine up")
        ok, msg = self.supervisor.ensure_running(
            lambda s: GLib.idle_add(self._ui_step, s))
        GLib.idle_add(self.render_engine)
        if not ok:
            GLib.idle_add(self.add_note, "[engine] %s" % msg, "err")
            if self.supervisor.state == "missing":
                GLib.idle_add(self.add_note, install_hint("ollama"), "dim")
            GLib.idle_add(self._ui_step, "engine down")
        else:
            names = Ollama(self.cfg).models()
            if names and model not in names:
                GLib.idle_add(
                    self.add_note,
                    "%s is not pulled yet - open Models and grab it, or run: "
                    "ollama pull %s" % (model, model), "err")
            GLib.idle_add(self._ui_step, "ready")
        GLib.idle_add(self._set_subtitle)
        self._async_weather()
        self._async_news("")
        GLib.idle_add(self.add_note,
                      "George online. Local model, no keys, no telemetry. "
                      "Ask for the news, the weather, this box, or the web.",
                      "dim")

    def render_engine(self) -> bool:
        self._clear(self.engine_body)
        sup = self.supervisor
        self.engine_body.append(_kv_row("ollama", sup.status_line()))
        self.engine_body.append(_kv_row("model", str(self.cfg.get("model"))))
        self.engine_body.append(_kv_row("host", sup.client.base.replace(
            "http://", "")))
        self.engine_body.append(_kv_row("voice", self.tts.engine_name))
        return False

    def _set_subtitle(self) -> bool:
        self.subtitle.set_text("%s  .  voice: %s" %
                               (self.cfg.get("model"), self.tts.engine_name))
        return False

    def _async_news(self, topic: str) -> None:
        def work() -> None:
            items = fetch_news(self.cfg.get("feeds") or DEFAULT_FEEDS,
                               per_feed=6, topic=topic)
            GLib.idle_add(self.render_news, items)
        threading.Thread(target=work, daemon=True).start()

    def _async_weather(self) -> None:
        def work() -> None:
            w = weather(str(self.cfg.get("location", "")))
            GLib.idle_add(self.render_weather, w)
        threading.Thread(target=work, daemon=True).start()

    def _save_session(self) -> None:
        if not self.agent.history:
            return
        first = next((m["content"] for m in self.agent.history
                      if m["role"] == "user"), "chat")
        self.chats.save(self.session_id, first[:60], self.agent.history)
        self.refresh_chats()

    # ---- dialogs --------------------------------------------------------
    def _open_about(self) -> None:
        self.add_note(
            "%s %s - local desktop AI, brother to Basilisk. Model %s on %s. "
            "No API keys, no cloud calls, no offensive tooling."
            % (APP_NAME, VERSION, self.cfg.get("model"),
               self.cfg.get("ollama_url")), "dim")

    def _open_history(self) -> None:
        self.chats.purge()
        rows = self.chats.listing()
        if not rows:
            self.toast("no saved chats")
            return
        win = Adw.Window(transient_for=self, modal=True)
        win.set_title("Recent chats")
        win.set_default_size(520, 520)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.append(Adw.HeaderBar())
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.set_margin_top(10)
        inner.set_margin_bottom(10)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        for sid, title, ts in rows:
            btn = Gtk.Button()
            btn.add_css_class("flat")
            inner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            inner_box.append(_label(title, "bubble-text"))
            inner_box.append(_label(time.strftime("%d %b %H:%M",
                                                  time.localtime(ts)), "dim"))
            btn.set_child(inner_box)
            btn.connect("clicked",
                        lambda *_a, s=sid, w=win: (self._load_session(s),
                                                   w.close()))
            inner.append(btn)
        sw.set_child(inner)
        box.append(sw)
        win.set_content(box)
        win.present()

    def _load_session(self, sid: str) -> None:
        sess = self.chats.get(sid)
        if not sess:
            return
        self._save_session()
        self.session_id = sid
        self.agent.history = list(sess.get("messages") or [])
        self._clear(self.transcript)
        for m in self.agent.history:
            if m.get("role") == "user":
                if m.get("content", "").startswith("OBSERVATION"):
                    continue
                self.add_user_bubble(m.get("content", ""))
            elif m.get("role") == "assistant":
                txt = strip_action_json(m.get("content", ""))
                if txt:
                    self.add_ai_bubble(txt)
        self.toast("loaded")

    # ---- model manager ---------------------------------------------------
    def _open_models(self) -> None:
        win = Adw.Window(transient_for=self, modal=True)
        win.set_title("Models")
        win.set_default_size(720, 680)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        hb = Adw.HeaderBar()
        hb.set_title_widget(_label("MODELS", "brand", wrap=False))
        outer.append(hb)

        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        col.set_margin_top(12)
        col.set_margin_bottom(12)
        col.set_margin_start(14)
        col.set_margin_end(14)
        sw.set_child(col)
        outer.append(sw)

        foot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        foot.add_css_class("feed-strip")
        status = _label("", "feed-text")
        bar = Gtk.ProgressBar()
        bar.set_visible(False)
        foot.append(status)
        foot.append(bar)
        outer.append(foot)
        win.set_content(outer)

        state = {"busy": False, "stop": threading.Event()}
        installed_card, installed_body = _card("ON THIS BOX")
        col.append(installed_card)
        custom_card, custom_body = _card("PULL ANYTHING")
        col.append(custom_card)
        avail_card, avail_body = _card("SUGGESTED")
        col.append(avail_card)

        def refresh() -> bool:
            self._clear(installed_body)
            rows = self.models.installed()
            if not rows:
                installed_body.append(_label(
                    "nothing pulled yet, or the engine is down", "dim"))
            active = str(self.cfg.get("model"))
            for m in rows:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row.add_css_class("news-row")
                name = _label(m["name"], "hud-val", wrap=False)
                name.set_hexpand(True)
                row.append(name)
                row.append(_label(m["size"], "dim", wrap=False))
                if m["name"] == active:
                    row.append(_label("active", "ok", wrap=False))
                else:
                    use = Gtk.Button(label="Use")
                    use.add_css_class("flat")
                    use.connect("clicked",
                                lambda *_a, n=m["name"]: pick(n))
                    row.append(use)
                rm = _icon_button("user-trash-symbolic", "Delete this model")
                rm.connect("clicked", lambda *_a, n=m["name"]: remove(n))
                row.append(rm)
                installed_body.append(row)
            return False

        def pick(name: str) -> None:
            self.cfg["model"] = name
            save_config(self.cfg)
            self._set_subtitle()
            self.render_engine()
            refresh()
            self.toast("model set to %s" % name)

        def remove(name: str) -> None:
            if state["busy"]:
                return
            if name == str(self.cfg.get("model")):
                status.set_text("that is the active model - switch first")
                return

            def work() -> None:
                ok, msg = self.models.delete(name)
                GLib.idle_add(status.set_text, msg)
                GLib.idle_add(refresh)
            threading.Thread(target=work, daemon=True).start()

        def pull(name: str) -> None:
            name = name.strip()
            if not name or state["busy"]:
                return
            state["busy"] = True
            state["stop"].clear()
            bar.set_visible(True)
            bar.set_fraction(0.0)
            status.set_text("pulling %s ..." % name)

            def progress(msg: str, frac: float) -> None:
                GLib.idle_add(status.set_text, "%s  %s" % (name, msg))
                GLib.idle_add(bar.set_fraction, frac)

            def work() -> None:
                ok, msg = self.models.pull(name, progress, state["stop"])
                GLib.idle_add(status.set_text, msg)
                GLib.idle_add(bar.set_visible, False)
                GLib.idle_add(refresh)
                state["busy"] = False
                if ok:
                    GLib.idle_add(self.toast, msg)
            threading.Thread(target=work, daemon=True).start()

        entry = Gtk.Entry()
        entry.set_placeholder_text("any ollama tag, e.g. qwen2.5:14b")
        entry.set_hexpand(True)
        go = Gtk.Button(label="Pull")
        go.add_css_class("suggested-action")
        go.connect("clicked", lambda *_a: pull(entry.get_text()))
        entry.connect("activate", lambda *_a: pull(entry.get_text()))
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        line.append(entry)
        line.append(go)
        custom_body.append(line)
        custom_body.append(_label(
            "anything on ollama.com/library works - George only needs a chat "
            "model", "dim"))

        for name, size, blurb in CURATED_MODELS:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("news-row")
            txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            txt.set_hexpand(True)
            txt.append(_label(name, "hud-val", wrap=False))
            txt.append(_label(blurb, "dim", wrap=False))
            row.append(txt)
            row.append(_label(size, "dim", wrap=False))
            btn = Gtk.Button(label="Pull")
            btn.add_css_class("flat")
            btn.connect("clicked", lambda *_a, n=name: pull(n))
            row.append(btn)
            avail_body.append(row)

        def on_close(*_a) -> bool:
            state["stop"].set()
            return False
        win.connect("close-request", on_close)
        refresh()
        win.present()

    # ---- settings --------------------------------------------------------
    def _open_settings(self) -> None:
        win = Adw.PreferencesWindow(transient_for=self, modal=True)
        win.set_title("George settings")
        win.set_default_size(660, 720)
        entries: Dict[str, Gtk.Widget] = {}

        def row(group, title: str, subtitle: str,
                widget: Gtk.Widget) -> None:
            r = Adw.ActionRow(title=title)
            if subtitle:
                r.set_subtitle(subtitle)
            widget.set_valign(Gtk.Align.CENTER)
            r.add_suffix(widget)
            r.set_activatable_widget(widget)
            group.add(r)

        def entry_for(key: str, width: int = 22) -> Gtk.Entry:
            e = Gtk.Entry()
            e.set_text(str(self.cfg.get(key, "")))
            e.set_width_chars(width)
            entries[key] = e
            return e

        def switch_for(key: str) -> Gtk.Switch:
            s = Gtk.Switch()
            s.set_active(bool(self.cfg.get(key)))
            entries[key] = s
            return s

        def spin_for(key: str, lo: float, hi: float, step: float,
                     digits: int = 0) -> Gtk.SpinButton:
            sp = Gtk.SpinButton.new_with_range(lo, hi, step)
            sp.set_digits(digits)
            sp.set_value(float(self.cfg.get(key, lo)))
            entries[key] = sp
            return sp

        # --- model page
        page = Adw.PreferencesPage(title="Model", icon_name="applications-science-symbolic")
        grp = Adw.PreferencesGroup(title="Local Ollama",
                                   description="No API key exists anywhere in "
                                               "this app. If ollama is down, "
                                               "George is down.")
        row(grp, "Server", "default http://localhost:11434",
            entry_for("ollama_url", 28))

        names = Ollama(self.cfg).models()
        cur = str(self.cfg.get("model"))
        if cur not in names:
            names.insert(0, cur)
        model_combo = Adw.ComboRow(title="Model")
        model_combo.set_model(Gtk.StringList.new(names))
        model_combo.set_selected(names.index(cur))
        entries["model"] = model_combo
        grp.add(model_combo)

        row(grp, "Temperature", "0.6 keeps deepseek-r1 from wandering",
            spin_for("temperature", 0.0, 2.0, 0.1, 2))
        row(grp, "Context window", "tokens", spin_for("num_ctx", 2048, 65536, 1024))
        row(grp, "Tool steps per turn", "hard ceiling on the loop",
            spin_for("max_steps", 1, 40, 1))
        page.add(grp)
        win.add(page)

        # --- voice page
        page = Adw.PreferencesPage(title="Voice", icon_name="audio-speakers-symbolic")
        grp = Adw.PreferencesGroup(title="Speech out",
                                   description="Engine in use: %s"
                                               % self.tts.engine_name)
        row(grp, "Read replies aloud", "", switch_for("voice_enabled"))
        engines = ["auto", "piper", "espeak", "none"]
        eng = Adw.ComboRow(title="Engine")
        eng.set_model(Gtk.StringList.new(engines))
        pref = str(self.cfg.get("voice_engine", "auto"))
        eng.set_selected(engines.index(pref) if pref in engines else 0)
        entries["voice_engine"] = eng
        grp.add(eng)
        row(grp, "Speed", "1.0 is natural", spin_for("voice_speed", 0.5, 2.0,
                                                     0.1, 1))
        row(grp, "Piper model", "path to a .onnx voice (blank = autodetect)",
            entry_for("piper_model", 30))
        page.add(grp)

        grp = Adw.PreferencesGroup(
            title="Speech in",
            description=(self.stt.why_unavailable() or
                         "recorder + %s, all local" % self.stt.engine))
        row(grp, "Push to talk enabled", "", switch_for("stt_enabled"))
        page.add(grp)
        win.add(page)

        # --- behaviour page
        page = Adw.PreferencesPage(title="Behaviour", icon_name="emblem-system-symbolic")
        grp = Adw.PreferencesGroup(
            title="Commands",
            description="Destructive commands are refused structurally and "
                        "that is not switchable.")
        row(grp, "Run commands without asking",
            "off = every non-read-only command needs one click",
            switch_for("auto_run_commands"))
        row(grp, "File sandbox root", "reads and writes stay under this",
            entry_for("sandbox_root", 26))
        page.add(grp)
        grp = Adw.PreferencesGroup(title="About you")
        row(grp, "Call you", "", entry_for("user_name", 18))
        row(grp, "Location", "for weather, e.g. Galway", entry_for("location", 18))
        row(grp, "Browser", "blank = xdg-open", entry_for("browser", 18))
        page.add(grp)
        win.add(page)

        # --- interface page
        page = Adw.PreferencesPage(title="Interface", icon_name="preferences-desktop-symbolic")
        grp = Adw.PreferencesGroup(title="Display")
        row(grp, "Font scale", "", spin_for("font_scale", 0.8, 1.8, 0.05, 2))
        row(grp, "Messages kept on screen", "older rows are dropped to save RAM",
            spin_for("transcript_live_rows", 10, 300, 10))
        row(grp, "Delete chats after (hours)", "0 = keep forever",
            spin_for("chat_retention_hours", 0, 720, 1))
        page.add(grp)

        grp = Adw.PreferencesGroup(title="News feeds",
                                   description="One per line:  Name | URL")
        feeds_view = Gtk.TextView()
        feeds_view.set_monospace(True)
        feeds_view.set_wrap_mode(Gtk.WrapMode.NONE)
        feeds_view.get_buffer().set_text(
            "\n".join("%s | %s" % (f[0], f[1])
                      for f in (self.cfg.get("feeds") or DEFAULT_FEEDS)
                      if len(f) >= 2))
        holder = Gtk.ScrolledWindow()
        holder.set_min_content_height(180)
        holder.set_child(feeds_view)
        holder.add_css_class("input-frame")
        grp.add(holder)
        row(grp, "Headlines shown", "", spin_for("news_count", 3, 40, 1))
        page.add(grp)
        win.add(page)

        def apply_and_close(*_a) -> bool:
            for key, widget in entries.items():
                if isinstance(widget, Gtk.Entry):
                    self.cfg[key] = widget.get_text().strip()
                elif isinstance(widget, Gtk.Switch):
                    self.cfg[key] = widget.get_active()
                elif isinstance(widget, Gtk.SpinButton):
                    val = widget.get_value()
                    self.cfg[key] = val if isinstance(DEFAULTS[key], float) \
                        else int(val)
                elif isinstance(widget, Adw.ComboRow):
                    item = widget.get_selected_item()
                    if item is not None:
                        self.cfg[key] = item.get_string()
            buf = feeds_view.get_buffer()
            raw = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            feeds = []
            for line in raw.splitlines():
                if "|" in line:
                    name, url = line.split("|", 1)
                    if url.strip():
                        feeds.append([name.strip() or "feed", url.strip()])
            if feeds:
                self.cfg["feeds"] = feeds
            save_config(self.cfg)
            self.tts.reconfigure()
            self.reload_css()
            self._set_subtitle()
            self._async_news("")
            self._async_weather()
            self.toast("settings saved")
            return False

        win.connect("close-request", apply_and_close)
        win.present()


# =====================================================================
# APPLICATION
# =====================================================================

class GeorgeApp(Adw.Application):
    """Owns the ollama daemon's lifetime.  It comes up with the app and
    goes down with it -- but only if we were the ones who started it."""

    def __init__(self) -> None:
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.cfg = load_config()
        self.supervisor = OllamaSupervisor(self.cfg)
        self.win: Optional[GeorgeWindow] = None
        self._shut = False

    def do_command_line(self, cmdline) -> int:
        self.activate()
        args = [a for a in cmdline.get_arguments()[1:] if not a.startswith("-")]
        if args and self.win:
            GLib.timeout_add(900, lambda: self.win._send(" ".join(args)))
        return 0

    def do_activate(self) -> None:
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK)
        if self.win is None:
            self.win = GeorgeWindow(self, self.cfg, self.supervisor)
            self.win.connect("close-request", self._on_close)
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig,
                                         self._on_signal)
                except Exception:
                    signal.signal(sig, lambda *_a: self._on_signal())
        self.win.present()

    def _on_signal(self) -> bool:
        log("caught a signal, shutting down")
        self.teardown()
        self.quit()
        return False

    def _on_close(self, _win) -> bool:
        self.teardown()
        return False                    # let the window actually close

    def teardown(self) -> None:
        if self._shut:
            return
        self._shut = True
        try:
            if self.win is not None:
                self.win.tts.stop()
                self.win._save_session()
                self.win.agent.stop()
        except Exception as exc:
            log("teardown (ui): %s" % exc)
        self.supervisor.shutdown()

    def do_shutdown(self) -> None:
        self.teardown()
        Adw.Application.do_shutdown(self)


def main() -> int:
    if "--version" in sys.argv:
        sys.stdout.write("%s %s\n" % (APP_NAME, VERSION))
        return 0
    if "--help" in sys.argv or "-h" in sys.argv:
        sys.stdout.write(
            "george [--version] [prompt ...]\n"
            "\n"
            "  Local desktop AI. Starts ollama on launch and stops it on\n"
            "  exit (only if it started it). No API keys, ever.\n"
            "\n"
            "  Any trailing words are sent as your first message:\n"
            "    george what is on the news\n"
            "\n"
            "  Config: %s\n"
            "  Data:   %s\n" % (CONFIG_PATH, os.path.dirname(NOTES_PATH)))
        return 0
    log("starting %s %s" % (APP_NAME, VERSION))
    return GeorgeApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
