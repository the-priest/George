# -*- coding: ascii -*-
"""
Runtime hook: point GTK at the copies inside the bundle.

This runs before any import in george.py. Every path GTK resolves from
the environment has to be redirected here, because inside a PyInstaller
bundle nothing is where GTK was compiled to expect it. Getting one of
these wrong does not produce an import error -- it produces a window
with no icons, or a hard abort about a missing settings schema, which is
a much worse thing to debug.
"""

import os
import sys

BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _set(name, value):
    if os.path.exists(value):
        os.environ[name] = value


_set("GI_TYPELIB_PATH", os.path.join(BASE, "lib", "girepository-1.0"))
_set("GSETTINGS_SCHEMA_DIR", os.path.join(BASE, "share", "glib-2.0",
                                          "schemas"))
_set("GDK_PIXBUF_MODULE_FILE",
     os.path.join(BASE, "lib", "gdk-pixbuf-2.0", "2.10.0", "loaders.cache"))
_set("GDK_PIXBUF_MODULEDIR",
     os.path.join(BASE, "lib", "gdk-pixbuf-2.0", "2.10.0", "loaders"))

# XDG_DATA_DIRS is how GTK finds the icon themes. Prepend rather than
# replace: a user with icon themes installed system-wide should keep
# them.
share = os.path.join(BASE, "share")
if os.path.isdir(share):
    existing = os.environ.get("XDG_DATA_DIRS", "")
    os.environ["XDG_DATA_DIRS"] = (share + os.pathsep + existing
                                   if existing else share)

# GTK4's default renderer wants working OpenGL. On a laptop with only
# integrated graphics and a stock Windows driver that is a coin flip, and
# when it loses the window comes up black. This is opt-in rather than
# forced because the cairo renderer is noticeably slower.
def _wants_safe_graphics():
    if os.environ.get("GEORGE_SAFE_GRAPHICS"):
        return True
    # Read the config directly rather than importing george_core: this
    # hook runs before GTK is even on the path, and importing the core
    # here would fix the renderer choice after GTK had already made it.
    try:
        import json
        appdata = os.environ.get("APPDATA", "")
        with open(os.path.join(appdata, "George", "config.json"),
                  encoding="utf-8") as fh:
            return bool(json.load(fh).get("safe_graphics"))
    except Exception:
        return False


if _wants_safe_graphics():
    os.environ["GSK_RENDERER"] = "cairo"

# Windows has no /tmp; make sure the temp dir George writes screenshots
# and TTS wavs into actually exists before anything asks for it.
for var in ("TEMP", "TMP"):
    path = os.environ.get(var, "")
    if path and not os.path.isdir(path):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
