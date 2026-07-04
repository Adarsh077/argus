# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Argus on Windows. UNTESTED — authored on Linux
# without access to a Windows machine or PyInstaller/Windows toolchain;
# see packaging/windows/README.md for exact build steps and what to
# verify.
#
# --- onedir, not onefile (why) -----------------------------------------
# Argus bundles a real templates/ directory (Jinja2 HTML templates for the
# dashboard, see src/argus/dashboard/templates/) as PyInstaller `datas`.
# onefile mode re-extracts the *entire* bundle (interpreter, all wheels,
# all data files) to a fresh temp directory on every single launch, which
# is slow, and — more importantly for Argus specifically — is a bad match
# for how this app runs: `windows_main.py` starts a long-running daemon
# thread immediately, so the process is not a short "do a thing and exit"
# CLI invocation where onefile's startup tax is easy to hide. onedir keeps
# a stable directory on disk that is fast to start and whose data files
# (this template directory in particular) are simple, already-unpacked
# real files at a fixed relative location next to the exe — no runtime
# extraction step to get wrong.

import os

block_cipher = None

# This spec lives at packaging/windows/argus.spec; the actual package
# source is at ../../src/argus relative to this file.
_here = os.path.dirname(os.path.abspath(SPEC))
_repo_root = os.path.abspath(os.path.join(_here, "..", ".."))
_src = os.path.join(_repo_root, "src")

a = Analysis(
    [os.path.join(_src, "argus", "windows_main.py")],
    pathex=[_src],
    binaries=[],
    datas=[
        # Dashboard Jinja2 templates — the one non-.py data directory the
        # app actually needs at runtime. Destination path
        # "argus/dashboard/templates" mirrors the package layout so
        # src/argus/dashboard/app.py's TEMPLATES_DIR resolution (which
        # checks sys._MEIPASS first when frozen, see that file) finds it
        # at <dist>/argus/argus/dashboard/templates in onedir mode.
        (
            os.path.join(_src, "argus", "dashboard", "templates"),
            os.path.join("argus", "dashboard", "templates"),
        ),
    ],
    hiddenimports=[
        # uvicorn/fastapi/starlette pull in some backends dynamically
        # (import-by-string) that PyInstaller's static analysis can miss.
        # The dashboard is pinned to loop="asyncio" + http="h11" + ws="none"
        # (see daemon.py _dashboard_loop) precisely so we depend only on the
        # pure-Python h11/asyncio implementations that reliably bundle, not
        # the optional C extensions (uvloop/httptools/websockets) that "auto"
        # prefers and PyInstaller routinely misses.
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "h11",
        "pystray._win32",
        "win32timezone",  # pulled in transitively by pywin32 on some setups
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Argus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # no console window — this is a tray + background-daemon app
    icon=None,  # TODO: add an .ico if/when Argus gets a real icon asset
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="argus",  # -> dist/argus/  (onedir output directory)
)
