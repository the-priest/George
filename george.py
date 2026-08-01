#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
george.py -- the GTK4 / libadwaita shell.

Basilisk's brother: same window shape, same live action feed, same voice
stack, same blunt tone.  No security tooling anywhere in it.  The brain
is a local Ollama model and there is no API key in this program.

The look lives in george_theme.py and the moving instruments live in
george_hud.py, so this file is only wiring: widgets, callbacks and the
rules about which thread is allowed to touch what.

Threading rule, unchanged and load-bearing: workers never touch a
widget.  Everything crossing back into the UI goes through idle(), which
also swallows exceptions -- an exception raised inside a GLib idle
callback kills the callback silently and leaves the window half updated,
which is a miserable thing to debug.
"""

from __future__ import annotations

import os
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
    OllamaSupervisor, clipboard_write, fetch_news, install_hint,
    install_crash_handlers, load_config, log, open_in_browser, reasoning_of,
    save_config, system_status, take_screenshot, weather,
)
import george_platform as osx
from george_theme import FALLBACK_CSS, build_css, palette, rgb
from george_tools import Agent, strip_action_json
from george_voice import SpeechToText, TextToSpeech
from george_vision import VISION_MODELS, Eyes, Watcher
from george_sound import Sounds
import george_hud as hud


# =====================================================================
# THREAD PLUMBING
# =====================================================================

def idle(fn: Callable, *args: Any) -> None:
    """Run fn on the GTK thread.  Never raises into the main loop."""
    def wrapper() -> bool:
        try:
            fn(*args)
        except Exception as exc:
            log("ui callback %s failed: %s" % (getattr(fn, "__name__", fn),
                                               exc))
        return False
    GLib.idle_add(wrapper)


def guard(fn: Callable) -> Callable:
    """Decorator for direct GTK callbacks (clicks, timeouts)."""
    def wrapped(*args: Any, **kw: Any) -> Any:
        try:
            return fn(*args, **kw)
        except Exception as exc:
            log("handler %s failed: %s" % (getattr(fn, "__name__", fn), exc))
            return None
    wrapped.__name__ = getattr(fn, "__name__", "wrapped")
    return wrapped


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


def _box(horizontal: bool = False, spacing: int = 0) -> Gtk.Box:
    return Gtk.Box(orientation=(Gtk.Orientation.HORIZONTAL if horizontal
                                else Gtk.Orientation.VERTICAL),
                   spacing=spacing)


def _margins(widget: Gtk.Widget, top: int = 0, bottom: int = 0,
             start: int = 0, end: int = 0) -> Gtk.Widget:
    widget.set_margin_top(top)
    widget.set_margin_bottom(bottom)
    widget.set_margin_start(start)
    widget.set_margin_end(end)
    return widget


def _card(title: str, action: Optional[Gtk.Widget] = None,
          css: str = "hud-card") -> Tuple[Gtk.Box, Gtk.Box]:
    """A titled panel.  Returns (card, body) -- callers fill the body."""
    outer = _box(spacing=8)
    outer.add_css_class(css)
    head = _box(horizontal=True, spacing=6)
    lbl = _label(title, "hud-title", wrap=False)
    lbl.set_hexpand(True)
    head.append(lbl)
    if action is not None:
        head.append(action)
    outer.append(head)
    body = _box(spacing=5)
    outer.append(body)
    return outer, body


def _kv_row(key: str, value: str) -> Gtk.Box:
    row = _box(horizontal=True, spacing=8)
    k = _label(key, "hud-key", wrap=False)
    k.set_hexpand(True)
    k.set_ellipsize(Pango.EllipsizeMode.END)
    v = _label(value, "hud-val", wrap=False, xalign=1.0)
    v.set_ellipsize(Pango.EllipsizeMode.END)
    row.append(k)
    row.append(v)
    return row


def _clear(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


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
            try:
                win = Adw.Window(transient_for=self.parent, modal=True)
                win.set_default_size(540, -1)
                win.set_title(title)
                box = _margins(_box(spacing=14), 4, 18, 18, 18)
                hb = Adw.HeaderBar()
                hb.set_show_end_title_buttons(False)
                hb.set_title_widget(_label(title, "brand", wrap=False))
                box.append(hb)
                box.append(_label(body, "mono", selectable=True))
                btns = _box(horizontal=True, spacing=8)
                btns.set_halign(Gtk.Align.END)
                no = Gtk.Button(label="No")
                yes = Gtk.Button(label="Do it")
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
                win.connect("close-request",
                            lambda *_a: (done.set(), False)[1])
                win.present()
            except Exception as exc:
                log("confirm dialog failed: %s" % exc)
                done.set()
            return False

        GLib.idle_add(build)
        if not done.wait(timeout=300):
            return False
        return result["ok"]


# =====================================================================
# AI BUBBLE
#
# Streams as plain text (cheap, 8 repaints a second), then re-renders
# once as rich blocks when the turn ends.
# =====================================================================

class AiBubble:

    def __init__(self, win: "GeorgeWindow", text: str = "") -> None:
        self.win = win
        self.text = text
        self.box = _box(spacing=8)
        self.box.add_css_class("bubble-ai")

        head = _box(horizontal=True, spacing=8)
        av = _label("G", "avatar", wrap=False, xalign=0.5)
        av.set_size_request(26, 26)
        av.set_valign(Gtk.Align.START)
        head.append(av)
        name = _label(APP_NAME.upper(), "hud-title", wrap=False)
        name.set_hexpand(True)
        head.append(name)
        head.append(_label(time.strftime("%H:%M"), "stamp", wrap=False))
        self.box.append(head)

        self.content = _box(spacing=8)
        self.box.append(self.content)

        self.stream_label = _label(text, "bubble-text", selectable=True)
        self.stream_label.set_max_width_chars(84)
        self.content.append(self.stream_label)

        tools = _box(horizontal=True, spacing=2)
        tools.set_halign(Gtk.Align.END)
        play = _icon_button("media-playback-start-symbolic", "Read this aloud")
        play.connect("clicked", lambda *_a: self.win.tts.speak(self.text))
        copy = _icon_button("edit-copy-symbolic", "Copy this reply")
        copy.connect("clicked", lambda *_a: self.win.copy(self.text))
        tools.append(play)
        tools.append(copy)
        self.box.append(tools)

    def set_streaming_text(self, text: str) -> None:
        self.text = text
        if self.stream_label is not None:
            self.stream_label.set_text(text)

    def finalise(self, text: str) -> None:
        """Swap the plain streaming label for rendered blocks."""
        self.text = text
        try:
            _clear(self.content)
            self.stream_label = None
            colours = palette(self.win.cfg)
            for kind, lang, body in hud.split_code_blocks(text):
                if kind == "code":
                    self.content.append(
                        hud.code_card(lang, body, self.win.copy))
                    continue
                lbl = _label("", "bubble-text", selectable=True)
                lbl.set_max_width_chars(84)
                hud.safe_markup(lbl, body, colours["accent_light"],
                                colours["faint"])
                lbl.connect("activate-link", self.win.on_link)
                self.content.append(lbl)
        except Exception as exc:
            log("rich render failed, falling back to plain: %s" % exc)
            _clear(self.content)
            self.stream_label = _label(text, "bubble-text", selectable=True)
            self.stream_label.set_max_width_chars(84)
            self.content.append(self.stream_label)


# =====================================================================
# MAIN WINDOW
# =====================================================================

SUGGESTIONS = [
    ("What is on the news?", "news-symbolic"),
    ("How is this box doing?", "computer-symbolic"),
    ("Weather for today", "weather-clear-symbolic"),
    ("Open Hacker News on my screen", "web-browser-symbolic"),
]


class GeorgeWindow(Adw.ApplicationWindow):

    def __init__(self, app: Adw.Application, cfg: Dict[str, Any],
                 supervisor: OllamaSupervisor) -> None:
        super().__init__(application=app, title=APP_NAME)
        self.cfg = cfg
        self.supervisor = supervisor
        self.models = ModelManager(cfg)
        self.set_default_size(1320, 880)
        self.set_icon_name(APP_ID)

        self.memory = MemoryStore()
        self.chats = ChatStore(cfg)
        self.tts = TextToSpeech(cfg)
        self.stt = SpeechToText(cfg)
        self.agent = Agent(cfg, self.memory, self.tts)
        self.confirmer = Confirmer(self)
        self.sfx = Sounds(cfg)
        self.sfx.prebuild()
        self.eyes = Eyes(cfg)
        self.watcher = Watcher(cfg, self.eyes, self._grab_for_watch,
                               lambda text: idle(self._watch_said, text))

        self.session_id = "s%d" % int(time.time())
        self._live_raw = ""
        self._live_bubble: Optional[AiBubble] = None
        self._pulse_id = 0
        self._stick_bottom = True
        self._recording = False
        self._hero: Optional[Gtk.Widget] = None
        self._engine_ok = False

        self.css_provider: Optional[Gtk.CssProvider] = None
        self._install_css()
        self._build_ui()
        self._wire_agent()
        self._wire_keys()

        GLib.timeout_add_seconds(3, self._refresh_vitals)
        self._refresh_vitals()
        threading.Thread(target=self._startup_probe, daemon=True,
                         name="george-startup").start()

    # =================================================================
    # THEME
    # =================================================================
    def _install_css(self) -> None:
        provider = Gtk.CssProvider()
        try:
            provider.connect("parsing-error", self._on_css_error)
        except Exception:
            pass                      # older GTK: signal is not there
        try:
            provider.load_from_data(build_css(self.cfg))
        except Exception as exc:
            log("theme build failed (%s); using the fallback sheet" % exc)
            try:
                provider.load_from_data(FALLBACK_CSS)
            except Exception:
                return
        # If a bundled george.png exists next to the script, add a background
        # CSS rule that uses it as a large centered background for the chat.
        # Do not set `opacity` on the container (it affects children). Instead
        # overlay a dark translucent gradient above the image so text stays
        # readable while the art shows through.
        try:
            bg = os.path.join(os.path.dirname(__file__), "george.png")
            if os.path.isfile(bg):
                bg_url = "file://" + os.path.abspath(bg)
                extra = ('\n.chat-bg { background-image: linear-gradient(rgba(5,7,10,0.86), '
                         'rgba(5,7,10,0.86)), url("%s"); '
                         'background-size: cover; background-position: center; '
                         'background-repeat: no-repeat; }\n' % bg_url)
                try:
                    provider.load_from_data(extra.encode("utf-8"))
                except Exception as _:
                    # Non-fatal: the main stylesheet already provides the UI
                    log("could not load background image css: %s" % bg)
        except Exception:
            pass
        display = Gdk.Display.get_default()
        if display is None:
            log("no display; skipping stylesheet")
            return
        if self.css_provider is not None:
            try:
                Gtk.StyleContext.remove_provider_for_display(
                    display, self.css_provider)
            except Exception:
                pass
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.css_provider = provider

    @staticmethod
    def _on_css_error(_provider: Any, section: Any, error: Any) -> None:
        # A rule GTK does not understand is dropped, not fatal.  Log it
        # so it shows up once rather than being invisible forever.
        try:
            log("css: %s at %s" % (error.message, section.to_string()))
        except Exception:
            log("css: %s" % error)

    def reload_css(self) -> None:
        self._install_css()
        self._apply_theme_to_hud()

    def _apply_theme_to_hud(self) -> None:
        """The cairo instruments do not read CSS -- hand them the new
        colours whenever the accent changes."""
        p = palette(self.cfg)
        accent, dim, bad = rgb(p["accent"]), rgb(p["faint"]), rgb(p["bad"])
        animate = bool(self.cfg.get("animations", True))
        for widget, attrs in (
                (getattr(self, "core", None), ("accent", "dim", "bad")),
                (getattr(self, "spark_cpu", None), ("accent", "dim")),
                (getattr(self, "spark_mem", None), ("accent", "dim")),
                (getattr(self, "dot", None), ("accent",))):
            if widget is None:
                continue
            if "accent" in attrs:
                widget.accent = accent
            if "dim" in attrs:
                widget.dim = dim
            if "bad" in attrs:
                widget.bad = bad
            widget.queue_draw()
        for gauge in getattr(self, "gauges", {}).values():
            gauge.accent = accent
            gauge.dim = dim
            gauge.queue_draw()
        if getattr(self, "core", None) is not None:
            self.core.set_animate(animate)
        if getattr(self, "dot", None) is not None:
            self.dot.set_animate(animate)

    # =================================================================
    # LAYOUT
    # =================================================================
    def _build_ui(self) -> None:
        self.toasts = Adw.ToastOverlay()
        root = _box()
        self.toasts.set_child(root)
        self.set_content(self.toasts)

        root.append(self._build_header())

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_position(340)
        self.paned.set_vexpand(True)
        self.paned.set_start_child(self._build_sidebar())
        self.paned.set_end_child(self._build_chat())
        self.paned.set_resize_start_child(False)
        self.paned.set_shrink_start_child(False)
        root.append(self.paned)

    # ---- header ------------------------------------------------------
    def _build_header(self) -> Adw.HeaderBar:
        hb = Adw.HeaderBar()

        title = _box(spacing=1)
        title.set_halign(Gtk.Align.CENTER)
        title.append(_label("G E O R G E", "brand", wrap=False, xalign=0.5))
        self.subtitle = _label("", "brand-sub", wrap=False, xalign=0.5)
        title.append(self.subtitle)
        hb.set_title_widget(title)

        toggle = _icon_button("sidebar-show-symbolic", "Toggle the HUD  (F9)")
        toggle.connect("clicked", self._on_toggle_sidebar)
        hb.pack_start(toggle)

        newchat = _icon_button("document-new-symbolic",
                               "New conversation  (Ctrl+N)")
        newchat.connect("clicked", self._on_new_chat)
        hb.pack_start(newchat)

        p = palette(self.cfg)
        pill = _box(horizontal=True, spacing=8)
        pill.add_css_class("status-pill")
        pill.set_valign(Gtk.Align.CENTER)
        self.dot = hud.StateDot(rgb(p["accent"]))
        self.dot.set_valign(Gtk.Align.CENTER)
        self.dot.set_colour(rgb(p["warn"]), pulsing=True)
        pill.append(self.dot)
        self.status_lbl = _label("STARTING", "status-text", wrap=False)
        pill.append(self.status_lbl)
        hb.pack_start(pill)

        self.watch_btn = Gtk.ToggleButton()
        self.watch_btn.set_icon_name("view-reveal-symbolic")
        self.watch_btn.add_css_class("flat")
        self.watch_btn.set_tooltip_text(
            "Let George watch your screen and chip in  (Ctrl+W)")
        self.watch_btn.connect("toggled", self._on_watch_toggle)
        hb.pack_end(self.watch_btn)

        self.mic_btn = _icon_button("audio-input-microphone-symbolic",
                                    "Push to talk  (Ctrl+M)")
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
        mb.set_tooltip_text("Menu")
        pop = Gtk.Popover()
        box = _margins(_box(spacing=2), 4, 4, 4, 4)

        def entry(label: str, accel: str, cb: Callable) -> None:
            b = Gtk.Button()
            b.add_css_class("flat")
            row = _box(horizontal=True, spacing=18)
            t = _label(label, "menu-item", wrap=False)
            t.set_hexpand(True)
            row.append(t)
            if accel:
                row.append(_label(accel, "menu-key", wrap=False))
            b.set_child(row)
            b.connect("clicked", lambda *_a: (pop.popdown(), cb()))
            box.append(b)

        entry("Models", "", self._open_models)
        entry("Settings", "Ctrl+,", self._open_settings)
        entry("Recent chats", "Ctrl+H", self._open_history)
        entry("Refresh news", "F5", lambda: self._async_news(""))
        entry("Open notes", "", lambda: open_in_browser("file://" + NOTES_PATH,
                                                        self.cfg))
        entry("Keyboard shortcuts", "", self._open_shortcuts)
        entry("About", "", self._open_about)
        pop.set_child(box)
        mb.set_popover(pop)
        return mb

    # ---- sidebar / HUD ----------------------------------------------
    def _build_sidebar(self) -> Gtk.Widget:
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add_css_class("sidebar-pane")
        col = _margins(_box(spacing=12), 14, 16, 12, 12)

        p = palette(self.cfg)
        accent, dim, bad = rgb(p["accent"]), rgb(p["faint"]), rgb(p["bad"])
        animate = bool(self.cfg.get("animations", True))

        # -- CORE: the one animated instrument, and the clock
        core_card, core_body = _card("CORE", css="core-card")
        self.core = hud.ReactorCore(accent, bad, dim, animate=animate)
        self.core.set_halign(Gtk.Align.CENTER)
        core_body.append(self.core)
        self.clock_lbl = _label("", "hud-big", wrap=False, xalign=0.5)
        self.date_lbl = _label("", "faint", wrap=False, xalign=0.5)
        core_body.append(self.clock_lbl)
        core_body.append(self.date_lbl)
        col.append(core_card)
        GLib.timeout_add_seconds(20, self._tick_clock)
        self._tick_clock()

        # -- SYSTEM: three gauges, two sparklines, the numbers underneath
        vit_card, self.vitals_body = _card("SYSTEM")
        gauge_row = _box(horizontal=True, spacing=8)
        gauge_row.set_halign(Gtk.Align.CENTER)
        self.gauges = {
            "cpu": hud.RingGauge("CPU", accent, dim),
            "mem": hud.RingGauge("RAM", accent, dim),
            "disk": hud.RingGauge("DISK", accent, dim),
        }
        for g in self.gauges.values():
            gauge_row.append(g)
        vit_card.insert_child_after(gauge_row, vit_card.get_first_child())

        self.spark_cpu = hud.Sparkline(accent, dim)
        spark_wrap = _box(spacing=2)
        spark_wrap.append(_label("cpu history", "faint", wrap=False))
        spark_wrap.append(self.spark_cpu)
        vit_card.append(spark_wrap)
        col.append(vit_card)

        # -- WEATHER
        wx_card, self.weather_body = _card("WEATHER")
        self.weather_body.append(_label("checking...", "dim"))
        col.append(wx_card)

        # -- ENGINE
        models_btn = _icon_button("emblem-system-symbolic", "Manage models")
        models_btn.connect("clicked", lambda *_a: self._open_models())
        eng_card, self.engine_body = _card("ENGINE", models_btn)
        self.engine_body.append(_label("starting...", "dim"))
        col.append(eng_card)

        # -- NEWS
        refresh = _icon_button("view-refresh-symbolic", "Refresh headlines")
        refresh.connect("clicked", lambda *_a: self._async_news(""))
        news_card, self.news_body = _card("NEWS", refresh)
        self.news_body.append(_label("loading feeds...", "dim"))
        col.append(news_card)

        # -- CHATS
        chat_card, self.chats_body = _card("CHATS")
        self.chats_hint = _label("", "faint")
        chat_card.append(self.chats_hint)
        col.append(chat_card)
        self.refresh_chats()

        sw.set_child(col)
        return sw

    # ---- chat pane ---------------------------------------------------
    def _build_chat(self) -> Gtk.Widget:
        col = _box()

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_vexpand(True)
        self.transcript = _margins(_box(spacing=12), 18, 10, 22, 22)
        # allow a large background image to be applied only to the transcript
        # area so the rest of the UI keeps full opacity and contrast.
        self.transcript.add_css_class("chat-bg")
        self.scroll.set_child(self.transcript)
        adj = self.scroll.get_vadjustment()
        adj.connect("changed", self._on_adj_changed)
        adj.connect("value-changed", self._on_adj_value)
        col.append(self.scroll)
        self._show_hero()

        strip = _margins(_box(horizontal=True, spacing=10), 4, 0, 22, 22)
        pill = _box(horizontal=True, spacing=8)
        pill.add_css_class("feed-pill")
        self.spinner = Gtk.Spinner()
        pill.append(self.spinner)
        self.feed_lbl = _label("ready", "feed-text", wrap=False)
        pill.append(self.feed_lbl)
        strip.append(pill)
        self.think_lbl = _label("", "think-text", wrap=False)
        self.think_lbl.set_hexpand(True)
        self.think_lbl.set_valign(Gtk.Align.CENTER)
        self.think_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        strip.append(self.think_lbl)
        col.append(strip)

        bar = _margins(_box(horizontal=True, spacing=10), 10, 16, 22, 22)
        frame = _box(horizontal=True, spacing=8)
        frame.add_css_class("composer")
        frame.set_hexpand(True)

        self.entry = Gtk.TextView()
        self.entry.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.entry.set_hexpand(True)
        self.entry.set_top_margin(8)
        self.entry.set_bottom_margin(8)
        entry_scroll = Gtk.ScrolledWindow()
        entry_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        entry_scroll.set_min_content_height(40)
        entry_scroll.set_max_content_height(190)
        entry_scroll.set_hexpand(True)
        entry_scroll.set_child(self.entry)
        frame.append(entry_scroll)

        self.send_btn = Gtk.Button()
        self.send_btn.set_icon_name("go-up-symbolic")
        self.send_btn.add_css_class("send-btn")
        self.send_btn.set_tooltip_text("Send  (Enter)")
        self.send_btn.set_valign(Gtk.Align.END)
        self.send_btn.connect("clicked", self._on_send_clicked)
        frame.append(self.send_btn)
        bar.append(frame)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_entry_key)
        self.entry.add_controller(keys)

        col.append(bar)
        return col

    # ---- empty state --------------------------------------------------
    def _show_hero(self) -> None:
        if self._hero is not None:
            return
        hero = _margins(_box(spacing=10), 60, 20, 0, 0)
        hero.set_valign(Gtk.Align.CENTER)
        hero.set_halign(Gtk.Align.CENTER)
        hero.append(_label("GEORGE", "hero-title", wrap=False, xalign=0.5))
        hero.append(_label(
            "Local brain, no keys, no cloud. Ask for the news, this box, "
            "the weather, or the web.", "hero-sub", xalign=0.5))
        chips = _margins(_box(spacing=8), 18, 0, 0, 0)
        chips.set_halign(Gtk.Align.CENTER)
        line = _box(horizontal=True, spacing=8)
        for i, (text, _icon) in enumerate(SUGGESTIONS):
            if i == 2:
                chips.append(line)
                line = _box(horizontal=True, spacing=8)
            btn = Gtk.Button()
            btn.add_css_class("chip")
            btn.set_child(_label(text, "", wrap=False))
            btn.connect("clicked", lambda *_a, t=text: self._send(t))
            line.append(btn)
        chips.append(line)
        hero.append(chips)
        self.transcript.append(hero)
        self._hero = hero

    def _drop_hero(self) -> None:
        if self._hero is not None:
            try:
                self.transcript.remove(self._hero)
            except Exception:
                pass
            self._hero = None

    # ---- scrolling ----------------------------------------------------
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
            if extra is self._hero:
                continue
            self.transcript.remove(extra)

    # ---- transcript rows ----------------------------------------------
    def _row(self, widget: Gtk.Widget, align: Gtk.Align) -> Gtk.Box:
        self._drop_hero()
        row = _box(horizontal=True)
        widget.set_halign(align)
        row.append(widget)
        self.transcript.append(row)
        self._trim_transcript()
        return row

    def add_user_bubble(self, text: str) -> None:
        box = _box(spacing=4)
        box.add_css_class("bubble-user")
        box.set_size_request(120, -1)
        lbl = _label(text, "bubble-text", selectable=True)
        lbl.set_max_width_chars(66)
        box.append(lbl)
        self._row(box, Gtk.Align.END)

    def add_ai_bubble(self, text: str = "") -> AiBubble:
        bubble = AiBubble(self, text)
        self._row(bubble.box, Gtk.Align.START)
        return bubble

    def add_tool_card(self, name: str, arg: str, result: str) -> None:
        box = _box(horizontal=True, spacing=10)
        box.add_css_class("tool-card")
        box.append(_label(name, "tool-name", wrap=False))
        mid = _label(arg[:110], "", wrap=False)
        mid.set_ellipsize(Pango.EllipsizeMode.END)
        mid.set_hexpand(True)
        box.append(mid)
        bad = any(w in result.upper() for w in ("REFUS", "FAIL", "DECLIN"))
        box.append(_label(result[:60], "err" if bad else "ok", wrap=False))
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
        pic.set_size_request(-1, 340)
        frame = _box(spacing=6)
        frame.add_css_class("bubble-ai")
        frame.append(pic)
        open_btn = Gtk.Button(label="Open it")
        open_btn.add_css_class("chip")
        open_btn.set_halign(Gtk.Align.START)
        open_btn.connect("clicked",
                         lambda *_a: open_in_browser("file://" + path,
                                                     self.cfg))
        frame.append(_label(path, "faint", wrap=False))
        frame.append(open_btn)
        self._row(frame, Gtk.Align.START)

    def copy(self, text: str) -> None:
        try:
            self.get_clipboard().set(text)
            self.toast("copied")
        except Exception:
            clipboard_write(text)
            self.toast("copied")

    def on_link(self, _label: Gtk.Label, uri: str) -> bool:
        open_in_browser(uri, self.cfg)
        return True

    def toast(self, text: str) -> None:
        try:
            self.toasts.add_toast(Adw.Toast.new(text))
        except Exception:
            pass

    # =================================================================
    # AGENT WIRING
    # =================================================================
    def _wire_agent(self) -> None:
        ag = self.agent
        ag.on_step = lambda s: idle(self._ui_step, s)
        ag.on_token = self._collect_token
        ag.on_tool = lambda n, a, r: idle(self.add_tool_card, n, a, r)
        ag.on_final = lambda s: idle(self._ui_final, s)
        ag.on_error = lambda s: idle(self._ui_error, s)
        ag.on_news = lambda i: idle(self.render_news, i)
        ag.on_weather = lambda w: idle(self.render_weather, w)
        ag.on_vitals = lambda v: idle(self.render_vitals, v)
        ag.on_image = lambda p: idle(self.add_image, p)
        ag.on_done = lambda: idle(self._ui_done)
        ag.ask_confirm = self.confirmer.ask
        self.tts.on_state = lambda s: idle(self._ui_voice_state, s)

    def _collect_token(self, piece: str) -> None:
        self._live_raw += piece

    @guard
    def _pulse(self) -> bool:
        """Repaint the streaming bubble ~8x a second instead of once per
        token -- a 7B model on a local GPU emits faster than GTK can
        usefully redraw, and this also proves the app is not stuck."""
        raw = self._live_raw
        if raw:
            visible = strip_action_json(raw)
            if self._live_bubble is None and visible:
                self._live_bubble = self.add_ai_bubble("")
            if self._live_bubble is not None:
                self._live_bubble.set_streaming_text(visible)
            if self.cfg.get("show_reasoning", True):
                tail = reasoning_of(raw).replace("\n", " ")
                self.think_lbl.set_text(tail[-160:] if tail else "")
        return True

    def _ui_step(self, text: str) -> None:
        self.feed_lbl.set_text(text)

    def _ui_final(self, text: str) -> None:
        if self._live_bubble is not None:
            self._live_bubble.finalise(text)
            self._live_bubble = None
        else:
            self.add_ai_bubble("").finalise(text)
        self._live_raw = ""
        self.think_lbl.set_text("")
        self.feed_lbl.set_text("done")
        self.sfx.play("reply")
        self._save_session()

    def _ui_error(self, text: str) -> None:
        self.sfx.play("error")
        self.add_note("[%s]" % text, "err")
        self.feed_lbl.set_text("error")
        self._set_state("down" if "ollama" in text.lower() else "idle")

    def _ui_done(self) -> None:
        if self._pulse_id:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = 0
        self._live_raw = ""
        self._live_bubble = None
        self.spinner.stop()
        self.send_btn.set_icon_name("go-up-symbolic")
        self.send_btn.remove_css_class("stop-btn")
        self.send_btn.add_css_class("send-btn")
        self.send_btn.set_tooltip_text("Send  (Enter)")
        if self.feed_lbl.get_text() not in ("error", "stopped"):
            self.feed_lbl.set_text("ready")
        self._set_state("idle" if self._engine_ok else "down")

    def _ui_voice_state(self, state: str) -> None:
        speaking = state == "speaking"
        self.voice_btn.set_icon_name("audio-volume-high-symbolic" if speaking
                                     else "audio-speakers-symbolic")
        if speaking:
            self._set_state("speaking")
        elif not self.agent.busy:
            self._set_state("idle" if self._engine_ok else "down")

    # ---- one place that owns "what is George doing right now" --------
    def _set_state(self, state: str) -> None:
        p = palette(self.cfg)
        if state == "idle" and self.watcher.running:
            state = "watching"
        labels = {"idle": "READY", "busy": "THINKING", "speaking": "SPEAKING",
                  "listening": "LISTENING", "down": "ENGINE DOWN",
                  "watching": "WATCHING"}
        colours = {"idle": rgb(p["ok"]), "busy": rgb(p["accent"]),
                   "speaking": rgb(p["accent"]), "listening": rgb(p["warn"]),
                   "down": rgb(p["bad"]), "watching": rgb(p["accent_light"])}
        self.core.set_state("listening" if state == "watching" else state)
        self.core.set_caption(labels.get(state, "READY"))
        self.status_lbl.set_text(labels.get(state, "READY"))
        self.dot.set_colour(colours.get(state, rgb(p["ok"])),
                            pulsing=state in ("busy", "listening", "speaking",
                                              "watching"))

    # =================================================================
    # SENDING
    # =================================================================
    def _on_entry_key(self, _ctrl, keyval, _code, state) -> bool:
        enter = keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if enter and not shift:
            self._send()
            return True
        return False

    def _wire_keys(self) -> None:
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_window_key)
        self.add_controller(keys)

    def _on_window_key(self, _c, keyval, _code, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_Escape and self.agent.busy:
            self.agent.stop()
            self.feed_lbl.set_text("stopped")
            return True
        if keyval == Gdk.KEY_F9:
            self._on_toggle_sidebar(None)
            return True
        if keyval == Gdk.KEY_F5:
            self._async_news("")
            return True
        if not ctrl:
            return False
        if keyval in (Gdk.KEY_k, Gdk.KEY_K):
            self.entry.grab_focus()
            return True
        if keyval in (Gdk.KEY_n, Gdk.KEY_N):
            self._on_new_chat(None)
            return True
        if keyval in (Gdk.KEY_m, Gdk.KEY_M):
            self._on_mic(None)
            return True
        if keyval in (Gdk.KEY_w, Gdk.KEY_W):
            self.watch_btn.set_active(not self.watch_btn.get_active())
            return True
        if keyval in (Gdk.KEY_h, Gdk.KEY_H):
            self._open_history()
            return True
        if keyval == Gdk.KEY_comma:
            self._open_settings()
            return True
        return False

    def _on_send_clicked(self, _btn) -> None:
        if self.agent.busy:
            self.agent.stop()
            self.sfx.play("stop")
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
        self.sfx.play("send")
        self.add_user_bubble(text)
        self._stick_bottom = True

        self.spinner.start()
        self.feed_lbl.set_text("thinking")
        self._set_state("busy")
        self.send_btn.set_icon_name("media-playback-stop-symbolic")
        self.send_btn.remove_css_class("send-btn")
        self.send_btn.add_css_class("stop-btn")
        self.send_btn.set_tooltip_text("Stop  (Esc)")
        if not self._pulse_id:
            self._pulse_id = GLib.timeout_add(120, self._pulse)
        self.agent.start(text)

    # =================================================================
    # HEADER ACTIONS
    # =================================================================
    def _on_toggle_sidebar(self, _btn) -> None:
        child = self.paned.get_start_child()
        if child:
            child.set_visible(not child.get_visible())

    def _on_new_chat(self, _btn) -> None:
        self._save_session()
        self.agent.reset()
        self.session_id = "s%d" % int(time.time())
        _clear(self.transcript)
        self._hero = None
        self._show_hero()
        self.feed_lbl.set_text("ready")

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
                self.sfx.play("listen")
                self.mic_btn.add_css_class("mic-live")
                self._set_state("listening")
                self.feed_lbl.set_text("listening... click the mic to stop")
            return
        self._recording = False
        self.mic_btn.remove_css_class("mic-live")
        self.feed_lbl.set_text("transcribing")

        def work() -> None:
            text = self.stt.stop_and_transcribe()
            idle(self._mic_done, text)

        threading.Thread(target=work, daemon=True, name="george-stt").start()

    def _mic_done(self, text: str) -> None:
        if not text:
            self.feed_lbl.set_text("heard nothing")
            self._set_state("idle" if self._engine_ok else "down")
            return
        self.feed_lbl.set_text("ready")
        self._send(text)

    # =================================================================
    # AMBIENT MODE
    #
    # Off unless he turns it on, and while it is on the header button
    # stays lit and the core says WATCHING. Nobody should ever have to
    # wonder whether this thing is looking.
    # =================================================================
    def _grab_for_watch(self) -> str:
        ok, path = take_screenshot()
        return path if ok else ""

    def _on_watch_toggle(self, btn: Gtk.ToggleButton) -> None:
        if btn.get_active():
            if not self.eyes.available():
                btn.set_active(False)
                self.toast("no vision model - pull one in Settings > Eyes")
                self._open_settings()
                return
            if not self.watcher.start():
                btn.set_active(False)
                self.toast("could not start watching")
                return
            self.watch_btn.add_css_class("mic-live")
            self.cfg["watch_enabled"] = True
            self.toast("watching your screen - %s mode"
                       % self.cfg.get("watch_mode", "advice"))
        else:
            self.watcher.stop()
            self.watch_btn.remove_css_class("mic-live")
            self.cfg["watch_enabled"] = False
            self.toast("stopped watching")
        save_config(self.cfg)
        self._set_state("idle" if self._engine_ok else "down")

    def _pull_model(self, name: str) -> None:
        """Pull from the Eyes page without making him find the Models
        window."""
        self.toast("pulling %s ..." % name)

        def work() -> None:
            try:
                _ok, msg = self.models.pull(name, lambda m, f: None,
                                            threading.Event())
            except Exception as exc:
                msg = "pull failed: %s" % exc
            idle(self.toast, msg)
        threading.Thread(target=work, daemon=True,
                         name="george-pull").start()

    def _sync_watcher(self) -> None:
        want = bool(self.cfg.get("watch_enabled"))
        if want and not self.watcher.running:
            if self.eyes.available() and self.watcher.start():
                self.watch_btn.set_active(True)
                self.watch_btn.add_css_class("mic-live")
            else:
                self.cfg["watch_enabled"] = False
        elif not want and self.watcher.running:
            self.watcher.stop()
            self.watch_btn.set_active(False)
            self.watch_btn.remove_css_class("mic-live")

    def _watch_said(self, text: str) -> None:
        """A remark from ambient mode. Visually distinct from an answer,
        because he did not ask for it."""
        self.sfx.play("notice")
        box = _box(spacing=6)
        box.add_css_class("bubble-ai")
        box.add_css_class("bubble-watch")
        head = _box(horizontal=True, spacing=8)
        av = _label("!", "avatar", wrap=False, xalign=0.5)
        av.set_size_request(26, 26)
        av.set_valign(Gtk.Align.START)
        head.append(av)
        tag = _label("GEORGE NOTICED", "hud-title", wrap=False)
        tag.set_hexpand(True)
        head.append(tag)
        head.append(_label(time.strftime("%H:%M"), "stamp", wrap=False))
        box.append(head)
        lbl = _label(text, "bubble-text", selectable=True)
        lbl.set_max_width_chars(80)
        box.append(lbl)
        self._row(box, Gtk.Align.START)
        if self.cfg.get("watch_speak", True) and \
                self.cfg.get("voice_enabled", True):
            self.tts.speak(text)

    # =================================================================
    # HUD REFRESH
    # =================================================================
    @guard
    def _tick_clock(self) -> bool:
        self.clock_lbl.set_text(time.strftime("%H:%M"))
        self.date_lbl.set_text(time.strftime("%a %d %b %Y").upper())
        return True

    @guard
    def _refresh_vitals(self) -> bool:
        st = system_status()
        self.render_vitals(st)
        return True

    def render_vitals(self, st: Dict[str, str]) -> None:
        def pct(key: str) -> float:
            try:
                return min(1.0, max(0.0, float(st.get(key, 0)) / 100.0))
            except (TypeError, ValueError):
                return 0.0

        cpu = pct("cpu_pct")
        self.gauges["cpu"].set_value(cpu)
        self.gauges["mem"].set_value(pct("mem_pct"))
        self.gauges["disk"].set_value(pct("disk_pct"))
        self.spark_cpu.push(cpu)
        if not self.agent.busy and not self.tts.speaking:
            self.core.set_load(cpu)
        else:
            self.core.set_load(max(cpu, 0.12))

        _clear(self.vitals_body)
        self.vitals_body.append(_kv_row("host", st.get("host", "?")))
        self.vitals_body.append(_kv_row("uptime", st.get("uptime", "?")))
        self.vitals_body.append(_kv_row("load", st.get("load", "?")))
        if st.get("memory"):
            self.vitals_body.append(_kv_row("memory", st["memory"]))
        if st.get("disk"):
            self.vitals_body.append(_kv_row("disk", st["disk"]))
        if st.get("swap"):
            self.vitals_body.append(_kv_row("swap", st["swap"]))
        if st.get("battery"):
            self.vitals_body.append(_kv_row("battery", st["battery"]))
        if st.get("temp"):
            self.vitals_body.append(_kv_row("cpu temp", st["temp"]))

    def render_weather(self, w: Dict[str, str]) -> None:
        _clear(self.weather_body)
        if w.get("error"):
            self.weather_body.append(_label(w["error"], "faint"))
            return
        top = _box(horizontal=True, spacing=8)
        temp = _label("%s" % w.get("temp_c", "?"), "hud-big", wrap=False)
        top.append(temp)
        unit = _box(spacing=0)
        unit.set_valign(Gtk.Align.CENTER)
        unit.append(_label("DEG C", "hud-unit", wrap=False))
        unit.append(_label(w.get("desc", ""), "dim"))
        top.append(unit)
        self.weather_body.append(top)
        self.weather_body.append(_kv_row("place", w.get("place", "?")))
        self.weather_body.append(_kv_row("feels", "%sC" % w.get("feels_c", "?")))
        self.weather_body.append(_kv_row("today", "%s - %sC"
                                         % (w.get("min_c", "?"),
                                            w.get("max_c", "?"))))
        self.weather_body.append(_kv_row("wind", "%s km/h"
                                         % w.get("wind_kph", "?")))

    def render_news(self, items: List[Dict[str, str]]) -> None:
        _clear(self.news_body)
        if not items:
            self.news_body.append(_label("no headlines", "faint"))
            return
        for it in items[:int(self.cfg.get("news_count", 12))]:
            url = it.get("url", "")
            btn = Gtk.Button()
            btn.add_css_class("row-btn")
            inner = _box(spacing=3)
            inner.append(_label(it.get("source", ""), "news-src", wrap=False))
            title = _label(it.get("title", ""), "news-title")
            title.set_max_width_chars(34)
            inner.append(title)
            btn.set_child(inner)
            if url:
                btn.set_tooltip_text("Open " + url)
                btn.connect("clicked",
                            lambda *_a, u=url: open_in_browser(u, self.cfg))
            self.news_body.append(btn)

    def render_engine(self) -> None:
        _clear(self.engine_body)
        sup = self.supervisor
        self.engine_body.append(_kv_row("ollama", sup.status_line()))
        self.engine_body.append(_kv_row("model", str(self.cfg.get("model"))))
        self.engine_body.append(_kv_row("host", sup.client.base.replace(
            "http://", "")))
        self.engine_body.append(_kv_row("voice", self.tts.engine_name))
        self.engine_body.append(_kv_row("persona",
                                        str(self.cfg.get("persona", "jarvis"))))

    def refresh_chats(self) -> None:
        self.chats.purge()
        rows = self.chats.listing()
        _clear(self.chats_body)
        hours = int(self.cfg.get("chat_retention_hours") or 0)
        self.chats_hint.set_text(
            "auto-deleted after %dh" % hours if hours else "kept until deleted")
        if not rows:
            self.chats_body.append(_label("nothing saved yet", "faint"))
            return
        for sid, title, ts in rows[:12]:
            row = _box(horizontal=True, spacing=2)
            open_btn = Gtk.Button()
            open_btn.add_css_class("row-btn")
            open_btn.set_hexpand(True)
            inner = _box(spacing=2)
            t = _label(title[:44], "news-title", wrap=False)
            t.set_ellipsize(Pango.EllipsizeMode.END)
            inner.append(t)
            inner.append(_label(time.strftime("%d %b %H:%M",
                                              time.localtime(ts)), "faint",
                                wrap=False))
            open_btn.set_child(inner)
            open_btn.connect("clicked",
                             lambda *_a, s=sid: self._load_session(s))
            row.append(open_btn)
            rm = _icon_button("user-trash-symbolic", "Delete this chat")
            rm.connect("clicked", lambda *_a, s=sid: self._delete_chat(s))
            row.append(rm)
            self.chats_body.append(row)

    def _delete_chat(self, sid: str) -> None:
        self.chats.delete(sid)
        if sid == self.session_id:
            self.agent.reset()
            _clear(self.transcript)
            self._hero = None
            self._show_hero()
            self.session_id = "s%d" % int(time.time())
        self.refresh_chats()
        self.toast("chat deleted")

    # =================================================================
    # BACKGROUND CHORES
    # =================================================================
    def _startup_probe(self) -> None:
        model = str(self.cfg.get("model"))
        idle(self._ui_step, "bringing the engine up")
        try:
            ok, msg = self.supervisor.ensure_running(
                lambda s: idle(self._ui_step, s))
        except Exception as exc:
            log("supervisor failed: %s" % exc)
            ok, msg = False, "could not start ollama: %s" % exc
        self._engine_ok = bool(ok)
        idle(self.render_engine)
        idle(self._set_state, "idle" if ok else "down")
        if not ok:
            idle(self.add_note, "[engine] %s" % msg, "err")
            if self.supervisor.state == "missing":
                idle(self.add_note, install_hint("ollama"), "dim")
            idle(self._ui_step, "engine down")
        else:
            names = Ollama(self.cfg).models()
            if names and model not in names:
                idle(self.add_note,
                     "%s is not pulled yet - open Models and grab it, or run: "
                     "ollama pull %s" % (model, model), "err")
            idle(self._ui_step, "ready")
        idle(self._set_subtitle)
        idle(self._sync_watcher)
        self._async_weather()
        self._async_news("")
        if self.cfg.get("greet_on_start", True):
            idle(self._greet)

    def _greet(self) -> None:
        name = (self.cfg.get("user_name") or "").strip()
        hour = int(time.strftime("%H"))
        part = ("Morning" if 5 <= hour < 12 else
                "Afternoon" if 12 <= hour < 18 else "Evening")
        line = "%s%s. %s." % (part, ", " + name if name else "",
                              "Everything is up" if self._engine_ok
                              else "The engine is down")
        self.feed_lbl.set_text(line.lower())

    def _set_subtitle(self) -> None:
        self.subtitle.set_text("%s  .  %s  .  voice %s"
                               % (self.cfg.get("model"),
                                  self.cfg.get("persona", "jarvis"),
                                  self.tts.engine_name))

    def _async_news(self, topic: str) -> None:
        def work() -> None:
            try:
                items = fetch_news(self.cfg.get("feeds") or DEFAULT_FEEDS,
                                   per_feed=6, topic=topic)
            except Exception as exc:
                log("news failed: %s" % exc)
                items = []
            idle(self.render_news, items)
        threading.Thread(target=work, daemon=True, name="george-news").start()

    def _async_weather(self) -> None:
        def work() -> None:
            try:
                w = weather(str(self.cfg.get("location", "")))
            except Exception as exc:
                w = {"error": "weather failed: %s" % exc}
            idle(self.render_weather, w)
        threading.Thread(target=work, daemon=True, name="george-wx").start()

    def _save_session(self) -> None:
        if not self.agent.history:
            return
        try:
            first = next((m["content"] for m in self.agent.history
                          if m["role"] == "user"), "chat")
            self.chats.save(self.session_id, first[:60], self.agent.history)
            self.refresh_chats()
        except Exception as exc:
            log("session save failed: %s" % exc)

    # =================================================================
    # DIALOGS
    # =================================================================
    def _dialog(self, title: str, width: int = 640,
                height: int = 640) -> Tuple[Adw.Window, Gtk.Box]:
        win = Adw.Window(transient_for=self, modal=True)
        win.set_title(title)
        win.set_default_size(width, height)
        outer = _box()
        outer.add_css_class("dialog-body")
        hb = Adw.HeaderBar()
        hb.set_title_widget(_label(title.upper(), "brand", wrap=False))
        outer.append(hb)
        win.set_content(outer)
        return win, outer

    def _open_about(self) -> None:
        win, outer = self._dialog("About", 560, 460)
        col = _margins(_box(spacing=10), 18, 18, 22, 22)
        col.append(_label("%s %s" % (APP_NAME, VERSION), "hero-title",
                          wrap=False))
        col.append(_label(
            "A local desktop AI, brother to Basilisk. Everything runs on this "
            "machine: the model, the memory, the voice. No API keys, no cloud "
            "calls, no telemetry, and no offensive security tooling.", "dim"))
        col.append(_kv_row("model", str(self.cfg.get("model"))))
        col.append(_kv_row("engine", str(self.cfg.get("ollama_url"))))
        col.append(_kv_row("ollama", Ollama(self.cfg).version()))
        col.append(_kv_row("voice", self.tts.engine_name))
        col.append(_kv_row("cairo HUD", "yes" if hud.HAVE_CAIRO
                           else "no (install python-cairo)"))
        col.append(_kv_row("config", CONFIG_PATH))
        col.append(_kv_row("data", os.path.dirname(NOTES_PATH)))
        outer.append(col)
        win.present()

    def _open_shortcuts(self) -> None:
        win, outer = self._dialog("Shortcuts", 460, 460)
        col = _margins(_box(spacing=6), 16, 16, 22, 22)
        for keys, what in (("Enter", "send"),
                           ("Shift+Enter", "newline"),
                           ("Esc", "stop the current turn"),
                           ("Ctrl+K", "jump to the box"),
                           ("Ctrl+N", "new conversation"),
                           ("Ctrl+M", "push to talk"),
                           ("Ctrl+H", "recent chats"),
                           ("Ctrl+,", "settings"),
                           ("F5", "refresh news"),
                           ("F9", "toggle the HUD")):
            col.append(_kv_row(what, keys))
        outer.append(col)
        win.present()

    def _open_history(self) -> None:
        self.chats.purge()
        rows = self.chats.listing()
        if not rows:
            self.toast("no saved chats")
            return
        win, outer = self._dialog("Recent chats", 560, 560)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        inner = _margins(_box(spacing=4), 12, 12, 14, 14)
        for sid, title, ts in rows:
            btn = Gtk.Button()
            btn.add_css_class("row-btn")
            box = _box(spacing=2)
            box.append(_label(title, "bubble-text"))
            box.append(_label(time.strftime("%d %b %H:%M",
                                            time.localtime(ts)), "faint"))
            btn.set_child(box)
            btn.connect("clicked",
                        lambda *_a, s=sid, w=win: (self._load_session(s),
                                                   w.close()))
            inner.append(btn)
        sw.set_child(inner)
        outer.append(sw)
        win.present()

    def _load_session(self, sid: str) -> None:
        sess = self.chats.get(sid)
        if not sess:
            return
        self._save_session()
        self.session_id = sid
        self.agent.history = list(sess.get("messages") or [])
        _clear(self.transcript)
        self._hero = None
        for m in self.agent.history:
            if m.get("role") == "user":
                if m.get("content", "").startswith("OBSERVATION"):
                    continue
                self.add_user_bubble(m.get("content", ""))
            elif m.get("role") == "assistant":
                txt = strip_action_json(m.get("content", ""))
                if txt:
                    self.add_ai_bubble("").finalise(txt)
        if self.transcript.get_first_child() is None:
            self._show_hero()
        self.toast("loaded")

    # ---- model manager ------------------------------------------------
    def _open_models(self) -> None:
        win, outer = self._dialog("Models", 740, 700)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        col = _margins(_box(spacing=12), 14, 14, 16, 16)
        sw.set_child(col)
        outer.append(sw)

        foot = _margins(_box(spacing=8), 8, 12, 16, 16)
        status = _label("", "feed-text")
        bar = Gtk.ProgressBar()
        bar.set_visible(False)
        foot.append(status)
        foot.append(bar)
        outer.append(foot)

        state = {"busy": False, "stop": threading.Event()}
        installed_card, installed_body = _card("ON THIS BOX")
        col.append(installed_card)
        custom_card, custom_body = _card("PULL ANYTHING")
        col.append(custom_card)
        avail_card, avail_body = _card("SUGGESTED")
        col.append(avail_card)

        @guard
        def refresh() -> bool:
            _clear(installed_body)
            rows = self.models.installed()
            if not rows:
                installed_body.append(_label(
                    "nothing pulled yet, or the engine is down", "faint"))
            active = str(self.cfg.get("model"))
            for m in rows:
                row = _box(horizontal=True, spacing=8)
                row.add_css_class("row-btn")
                name = _label(m["name"], "hud-val", wrap=False)
                name.set_hexpand(True)
                row.append(name)
                row.append(_label(m["size"], "faint", wrap=False))
                if m["name"] == active:
                    row.append(_label("active", "ok", wrap=False))
                else:
                    use = Gtk.Button(label="Use")
                    use.add_css_class("chip")
                    use.connect("clicked", lambda *_a, n=m["name"]: pick(n))
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
                idle(status.set_text, msg)
                idle(refresh)
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
                idle(status.set_text, "%s  %s" % (name, msg))
                idle(bar.set_fraction, frac)

            def work() -> None:
                try:
                    ok, msg = self.models.pull(name, progress, state["stop"])
                except Exception as exc:
                    ok, msg = False, "pull failed: %s" % exc
                idle(status.set_text, msg)
                idle(bar.set_visible, False)
                idle(refresh)
                state["busy"] = False
                if ok:
                    idle(self.toast, msg)
            threading.Thread(target=work, daemon=True).start()

        entry = Gtk.Entry()
        entry.set_placeholder_text("any ollama tag, e.g. qwen2.5:14b")
        entry.set_hexpand(True)
        go = Gtk.Button(label="Pull")
        go.add_css_class("suggested-action")
        go.connect("clicked", lambda *_a: pull(entry.get_text()))
        entry.connect("activate", lambda *_a: pull(entry.get_text()))
        line = _box(horizontal=True, spacing=8)
        line.append(entry)
        line.append(go)
        custom_body.append(line)
        custom_body.append(_label(
            "anything on ollama.com/library works - George only needs a chat "
            "model", "faint"))

        for name, size, blurb in CURATED_MODELS:
            row = _box(horizontal=True, spacing=8)
            row.add_css_class("row-btn")
            txt = _box(spacing=1)
            txt.set_hexpand(True)
            txt.append(_label(name, "hud-val", wrap=False))
            txt.append(_label(blurb, "faint", wrap=False))
            row.append(txt)
            row.append(_label(size, "faint", wrap=False))
            btn = Gtk.Button(label="Pull")
            btn.add_css_class("chip")
            btn.connect("clicked", lambda *_a, n=name: pull(n))
            row.append(btn)
            avail_body.append(row)

        def on_close(*_a) -> bool:
            state["stop"].set()
            return False
        win.connect("close-request", on_close)
        refresh()
        win.present()

    # ---- settings ------------------------------------------------------
    def _open_settings(self) -> None:
        win = Adw.PreferencesWindow(transient_for=self, modal=True)
        win.set_title("George settings")
        win.set_default_size(700, 760)
        entries: Dict[str, Gtk.Widget] = {}

        def row(group, title: str, subtitle: str, widget: Gtk.Widget) -> None:
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
            try:
                sp.set_value(float(self.cfg.get(key, lo)))
            except (TypeError, ValueError):
                sp.set_value(lo)
            entries[key] = sp
            return sp

        def combo_for(key: str, options: List[str], title: str) -> Adw.ComboRow:
            cr = Adw.ComboRow(title=title)
            cr.set_model(Gtk.StringList.new(options))
            cur = str(self.cfg.get(key, options[0]))
            cr.set_selected(options.index(cur) if cur in options else 0)
            entries[key] = cr
            return cr

        # --- model page
        page = Adw.PreferencesPage(title="Model",
                                   icon_name="applications-science-symbolic")
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
        grp.add(combo_for("model", names, "Model"))
        row(grp, "Temperature", "0.6 keeps deepseek-r1 from wandering",
            spin_for("temperature", 0.0, 2.0, 0.1, 2))
        row(grp, "Context window", "tokens",
            spin_for("num_ctx", 2048, 65536, 1024))
        row(grp, "Tool steps per turn", "hard ceiling on the loop",
            spin_for("max_steps", 1, 40, 1))
        row(grp, "Tool timeout", "seconds before a stuck tool is abandoned",
            spin_for("tool_timeout", 5, 900, 5))
        row(grp, "Fall back to an installed model",
            "if the chosen tag is not pulled",
            switch_for("auto_model_fallback"))
        grp.add(combo_for("thinking", ["off", "auto", "on"],
                          "Reasoning trace"))
        r = Adw.ActionRow(title="Why off is the default")
        r.set_subtitle("A reasoner thinks before every step, and a turn can "
                       "take fourteen. Off is much faster; on is better at "
                       "hard questions.")
        grp.add(r)
        page.add(grp)
        win.add(page)

        # --- voice page
        page = Adw.PreferencesPage(title="Voice",
                                   icon_name="audio-speakers-symbolic")
        grp = Adw.PreferencesGroup(title="Speech out",
                                   description="Engine in use: %s"
                                               % self.tts.engine_name)
        row(grp, "Read replies aloud", "", switch_for("voice_enabled"))
        grp.add(combo_for("voice_engine", ["auto", "piper", "espeak", "none"],
                          "Engine"))
        row(grp, "Speed", "1.0 is natural",
            spin_for("voice_speed", 0.5, 2.0, 0.1, 1))
        row(grp, "Pitch", "espeak only", spin_for("voice_pitch", 0, 99, 1))
        row(grp, "Preferred voice locale", "e.g. en_GB, en_US, en_IE",
            entry_for("piper_voice_pref", 12))
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
        page = Adw.PreferencesPage(title="Behaviour",
                                   icon_name="emblem-system-symbolic")
        grp = Adw.PreferencesGroup(
            title="Character",
            description="How he talks. The tools do not change.")
        grp.add(combo_for("persona", ["jarvis", "plain", "blunt"], "Persona"))
        row(grp, "Greet on start", "", switch_for("greet_on_start"))
        row(grp, "Show his reasoning", "the scrolling line above the box",
            switch_for("show_reasoning"))
        page.add(grp)
        grp = Adw.PreferencesGroup(
            title="Commands and files",
            description="Destructive commands are refused structurally and "
                        "that is not switchable.")
        row(grp, "Run commands without asking",
            "off = every non-read-only command needs one click",
            switch_for("auto_run_commands"))
        row(grp, "Write files without asking",
            "off = every file write needs one click",
            switch_for("allow_writes"))
        row(grp, "File sandbox root", "reads and writes stay under this",
            entry_for("sandbox_root", 26))
        page.add(grp)
        grp = Adw.PreferencesGroup(title="About you")
        row(grp, "Call you", "", entry_for("user_name", 18))
        row(grp, "Location", "for weather, e.g. Galway",
            entry_for("location", 18))
        row(grp, "Browser", "blank = xdg-open", entry_for("browser", 18))
        page.add(grp)
        win.add(page)

        # --- eyes page
        page = Adw.PreferencesPage(title="Eyes",
                                   icon_name="view-reveal-symbolic")
        installed_vision = self.eyes.installed()
        grp = Adw.PreferencesGroup(
            title="Screen vision",
            description=("Screenshots go to your local ollama and nowhere "
                         "else. Nothing is uploaded and nothing is kept - "
                         "the image is deleted the moment it is read."))
        opts = ["(auto)"] + installed_vision
        cur_v = str(self.cfg.get("vision_model", ""))
        cr = Adw.ComboRow(title="Vision model")
        cr.set_model(Gtk.StringList.new(opts))
        cr.set_selected(opts.index(cur_v) if cur_v in opts else 0)
        entries["vision_model"] = cr
        grp.add(cr)
        if not installed_vision:
            r = Adw.ActionRow(title="Nothing pulled yet")
            r.set_subtitle("Grab one below, then reopen this window")
            grp.add(r)
        page.add(grp)

        grp = Adw.PreferencesGroup(
            title="Pull a vision model",
            description="Smaller is faster. moondream is the laptop pick.")
        for name, size, blurb in VISION_MODELS:
            r = Adw.ActionRow(title=name)
            r.set_subtitle("%s  -  %s" % (size, blurb))
            if name in installed_vision:
                tick = Gtk.Label(label="installed")
                tick.add_css_class("ok")
                tick.set_valign(Gtk.Align.CENTER)
                r.add_suffix(tick)
            else:
                b = Gtk.Button(label="Pull")
                b.add_css_class("chip")
                b.set_valign(Gtk.Align.CENTER)
                b.connect("clicked", lambda *_a, n=name: self._pull_model(n))
                r.add_suffix(b)
            grp.add(r)
        page.add(grp)

        grp = Adw.PreferencesGroup(
            title="Ambient mode",
            description=("George looks at your screen every so often and "
                         "chips in. Off by default; while it is on the "
                         "header button stays lit and the core says "
                         "WATCHING."))
        row(grp, "Watch my screen", "same as the header button (Ctrl+W)",
            switch_for("watch_enabled"))
        grp.add(combo_for("watch_mode", ["advice", "banter", "quiet"],
                          "What he chips in with"))
        row(grp, "Say it out loud", "as well as showing it",
            switch_for("watch_speak"))
        row(grp, "Look every (seconds)", "",
            spin_for("watch_interval", 20, 3600, 10))
        row(grp, "Leave at least (seconds) between remarks", "",
            spin_for("watch_min_gap", 0, 7200, 30))
        row(grp, "Most remarks per hour", "",
            spin_for("watch_max_per_hour", 1, 120, 1))
        page.add(grp)
        win.add(page)

        # --- interface page
        page = Adw.PreferencesPage(title="Interface",
                                   icon_name="preferences-desktop-symbolic")
        grp = Adw.PreferencesGroup(title="Look")
        grp.add(combo_for("accent", ["cyan", "amber", "violet", "green",
                                     "red", "white"], "Accent"))
        grp.add(combo_for("ui_density", ["comfortable", "compact"], "Density"))
        row(grp, "Animate the HUD", "off saves a little power",
            switch_for("animations"))
        row(grp, "Interface sounds",
            "short blips on send, reply and errors" if self.sfx.available
            else "no audio player found (pw-play, paplay or aplay)",
            switch_for("sounds"))
        row(grp, "Font scale", "", spin_for("font_scale", 0.75, 2.0, 0.05, 2))
        row(grp, "Messages kept on screen",
            "older rows are dropped to save RAM",
            spin_for("transcript_live_rows", 10, 400, 10))
        page.add(grp)
        grp = Adw.PreferencesGroup(title="Chats")
        row(grp, "Delete chats after (hours)", "0 = keep forever",
            spin_for("chat_retention_hours", 0, 720, 1))
        row(grp, "Chats kept", "", spin_for("chat_max_sessions", 5, 500, 5))
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
        holder.set_min_content_height(170)
        holder.set_child(feeds_view)
        holder.add_css_class("composer")
        grp.add(holder)
        row(grp, "Headlines shown", "", spin_for("news_count", 3, 40, 1))
        page.add(grp)
        win.add(page)

        @guard
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
                        value = item.get_string()
                        self.cfg[key] = "" if value == "(auto)" else value
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
            self._sync_watcher()
            self.tts.reconfigure()
            self.reload_css()
            self._set_subtitle()
            self.render_engine()
            self._async_news("")
            self._async_weather()
            self.toast("settings saved")
            return False

        win.connect("close-request", apply_and_close)
        win.present()


# =====================================================================
# APPLICATION
# =====================================================================

def claim_identity() -> None:
    """Make the desktop see George instead of "python3".

    Three separate mechanisms have to agree or the window falls back to
    a generic icon:

      * Wayland matches the window's app_id -- which GTK takes from the
        Gio.Application id -- against a .desktop file of the same name.
      * X11 builds WM_CLASS from g_get_prgname(), which Python sets to
        the script name, so it has to be overridden before the display
        is opened.
      * The icon itself has to be findable by name in an icon theme.

    All three now use APP_ID, and the app directory is added to the icon
    search path so the icon also works when running from a checkout that
    was never installed.
    """
    try:
        GLib.set_prgname(APP_ID)
        GLib.set_application_name(APP_NAME)
    except Exception as exc:
        log("could not set prgname: %s" % exc)
    if osx.IS_WINDOWS:
        # Without an explicit AppUserModelID the taskbar groups George
        # under whatever launched it and shows that program's icon --
        # the Windows version of exactly the bug 2.1.0 fixed on Wayland.
        osx.win_set_app_id(APP_ID)


def register_icon() -> None:
    """Called once a display exists."""
    try:
        display = Gdk.Display.get_default()
        if display is None:
            return
        theme = Gtk.IconTheme.get_for_display(display)
        here = os.path.dirname(os.path.abspath(__file__))
        for path in (here, os.path.join(here, "icons")):
            if os.path.isdir(path):
                theme.add_search_path(path)
        # running from a checkout: the file is george.svg, but the theme
        # looks it up by APP_ID, so give it that name in a cache dir
        src = os.path.join(here, "george.svg")
        if os.path.exists(src) and not theme.has_icon(APP_ID):
            cache = os.path.join(GLib.get_user_cache_dir(), "george",
                                 "icons", "hicolor", "scalable", "apps")
            os.makedirs(cache, exist_ok=True)
            dest = os.path.join(cache, APP_ID + ".svg")
            if not os.path.exists(dest):
                import shutil
                shutil.copyfile(src, dest)
            theme.add_search_path(os.path.join(
                GLib.get_user_cache_dir(), "george", "icons"))
        Gtk.Window.set_default_icon_name(APP_ID)
    except Exception as exc:
        log("icon registration failed: %s" % exc)


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
        register_icon()
        if self.win is None:
            self.win = GeorgeWindow(self, self.cfg, self.supervisor)
            self.win.connect("close-request", self._on_close)
            # GLib.unix_signal_add does not exist on Windows, and
            # SIGTERM is not a thing there either -- SIGBREAK is the
            # nearest equivalent. Ask for whatever this OS actually has.
            wanted = [signal.SIGINT]
            for name in ("SIGTERM", "SIGBREAK"):
                sig = getattr(signal, name, None)
                if sig is not None:
                    wanted.append(sig)
            for sig in wanted:
                try:
                    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig,
                                         self._on_signal)
                except Exception:
                    try:
                        signal.signal(sig, lambda *_a: self._on_signal())
                    except (ValueError, OSError, AttributeError) as exc:
                        log("no handler for %s: %s" % (sig, exc))
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
                self.win.watcher.stop()
                self.win.tts.stop()
                self.win._save_session()
                self.win.agent.stop()
        except Exception as exc:
            log("teardown (ui): %s" % exc)
        self.supervisor.shutdown()

    def do_shutdown(self) -> None:
        self.teardown()
        Adw.Application.do_shutdown(self)


def _say(text: str) -> None:
    """A windowed Windows build has no stdout at all -- sys.stdout is
    None and writing to it raises. Fall back to a message box so
    `George.exe --version` still answers instead of dying."""
    if sys.stdout is not None:
        try:
            sys.stdout.write(text)
            return
        except Exception:
            pass
    if not osx.IS_WINDOWS:
        return
    # Launched from a terminal there IS a console -- the windowed build
    # just is not attached to it. Attach to the parent and print like a
    # normal program; only fall back to a dialog when double-clicked.
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):
            with open("CONOUT$", "w", encoding="utf-8") as con:
                con.write(text)
            return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x40)
    except Exception:
        pass


def main() -> int:
    if "--version" in sys.argv:
        _say("%s %s\n" % (APP_NAME, VERSION))
        return 0
    if "--help" in sys.argv or "-h" in sys.argv:
        _say(
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
    install_crash_handlers()
    claim_identity()
    log("starting %s %s on %s" % (APP_NAME, VERSION, osx.describe()))
    return GeorgeApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
