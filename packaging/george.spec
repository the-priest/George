# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Windows build. Run it from an MSYS2 UCRT64
shell, from the repo root:

    pyinstaller packaging/george.spec --noconfirm

Why the explicit lists below instead of trusting the gi hooks:

GTK does not link most of what it needs. It loads typelibs by name at
run time, loads gdk-pixbuf loaders as plugins, reads its settings out of
a compiled GSettings schema, and looks icons up in a theme directory.
None of that is visible to a dependency walker, so a bundle built purely
from the import graph starts up and then dies with "Settings schema
org.gtk.Settings.FileChooser is not installed" or renders every button
as a missing-image box.

Listing the DLLs by name is enough for the rest: PyInstaller resolves
each one's own dependencies recursively, so this stays a minimal closure
rather than shipping the whole of MSYS2.
"""

import glob
import os
import sys

PREFIX = os.environ.get("MINGW_PREFIX", "C:/msys64/ucrt64")
ROOT = os.path.abspath(os.path.join(os.getcwd()))

if not os.path.isdir(PREFIX):
    raise SystemExit("MINGW_PREFIX %r does not exist -- run this from an "
                     "MSYS2 UCRT64 shell" % PREFIX)


def prefix(*parts):
    return os.path.join(PREFIX, *parts)


# ---------------------------------------------------------------------
# Binaries GTK loads by name rather than by linking
# ---------------------------------------------------------------------
RUNTIME_DLLS = [
    "libgtk-4-1.dll",
    "libadwaita-1-0.dll",
    "libgirepository-1.0-1.dll",
    "libgio-2.0-0.dll",
    "libglib-2.0-0.dll",
    "libgobject-2.0-0.dll",
    "libgdk_pixbuf-2.0-0.dll",
    "libgraphene-1.0-0.dll",
    "libpango-1.0-0.dll",
    "libpangocairo-1.0-0.dll",
    "libpangowin32-1.0-0.dll",
    "libcairo-2.dll",
    "libcairo-gobject-2.dll",
    "librsvg-2-2.dll",           # renders george.svg and symbolic icons
    "libepoxy-0.dll",            # GL loader: GSK's renderer needs it
    "libharfbuzz-0.dll",
    "libfribidi-0.dll",
]

binaries = []
missing = []
for name in RUNTIME_DLLS:
    path = prefix("bin", name)
    if os.path.exists(path):
        binaries.append((path, "."))
    else:
        missing.append(name)
if missing:
    # Not fatal: package names drift between MSYS2 revisions and the
    # dependency walker picks most of these up anyway. Say so loudly
    # rather than producing a bundle that fails on someone else's box.
    sys.stderr.write("spec: not found in %s/bin: %s\n"
                     % (PREFIX, ", ".join(missing)))

# gdk-pixbuf loaders are plugins -- without them, and without the cache
# that indexes them, GTK cannot decode a PNG or an SVG.
for path in glob.glob(prefix("lib", "gdk-pixbuf-2.0", "2.10.0", "loaders",
                             "*.dll")):
    binaries.append((path, "lib/gdk-pixbuf-2.0/2.10.0/loaders"))

# ---------------------------------------------------------------------
# Data GTK reads off disk
# ---------------------------------------------------------------------
datas = []

for path in glob.glob(prefix("lib", "girepository-1.0", "*.typelib")):
    datas.append((path, "lib/girepository-1.0"))

cache = prefix("lib", "gdk-pixbuf-2.0", "2.10.0", "loaders.cache")
if os.path.exists(cache):
    datas.append((cache, "lib/gdk-pixbuf-2.0/2.10.0"))

schemas = prefix("share", "glib-2.0", "schemas", "gschemas.compiled")
if os.path.exists(schemas):
    datas.append((schemas, "share/glib-2.0/schemas"))
else:
    raise SystemExit("gschemas.compiled is missing -- run "
                     "glib-compile-schemas %s" % prefix("share", "glib-2.0",
                                                        "schemas"))

# Icon themes. Adwaita is where every symbolic icon in the UI comes from.
for theme in ("Adwaita", "hicolor"):
    root = prefix("share", "icons", theme)
    for folder, _dirs, files in os.walk(root):
        for name in files:
            src = os.path.join(folder, name)
            rel = os.path.relpath(folder, prefix("share"))
            datas.append((src, os.path.join("share", rel)))

# George's own files
datas.append((os.path.join(ROOT, "george.svg"), "."))
datas.append((os.path.join(ROOT, "packaging", "george-safe.cmd"), "."))
for extra in ("README.md", "CHANGELOG.md"):
    if os.path.exists(os.path.join(ROOT, extra)):
        datas.append((os.path.join(ROOT, extra), "."))

hiddenimports = [
    "gi", "gi._gi", "gi.repository.Gtk", "gi.repository.Adw",
    "gi.repository.Gdk", "gi.repository.Gio", "gi.repository.GLib",
    "gi.repository.Pango", "gi.repository.GdkPixbuf",
    "gi.repository.GObject", "gi.repository.Graphene",
    "cairo",
    # George's own modules are imported by name from george.py, but list
    # them so a typo in an import shows up at build time, not at launch.
    "george_core", "george_platform", "george_theme", "george_tools",
    "george_hud", "george_voice", "george_vision", "george_sound",
    "winsound",
]

a = Analysis(
    [os.path.join(ROOT, "george.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[os.path.join(ROOT, "packaging", "runtime_hook_gtk.py")],
    excludes=["tkinter", "test", "unittest", "pydoc_data", "lib2to3"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="George",
    debug=False,
    strip=False,
    upx=False,                   # UPX corrupts GTK DLLs often enough to matter
    console=False,               # windowed: --version attaches to the parent
    icon=os.path.join(ROOT, "packaging", "george.ico"),
    version=os.path.join(ROOT, "packaging", "version_info.txt")
    if os.path.exists(os.path.join(ROOT, "packaging", "version_info.txt"))
    else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="George",
)
