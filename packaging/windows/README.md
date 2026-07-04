# Argus on Windows — installer + Task Scheduler (Final Packaging)

**Status: UNTESTED, end to end.** Everything in this directory — the
PyInstaller spec, the Inno Setup script, the combined
`argus.windows_main` entrypoint it builds, and the steps below — was
written without access to a Windows machine or the PyInstaller/Inno Setup
toolchains, and none of it has actually been run. Treat it as a
best-effort recipe to verify and fix on a real Windows box, not as a
tested artifact.

## Building the installer (on Windows)

1. Install [`uv`](https://docs.astral.sh/uv/) and sync deps, then add
   PyInstaller (a Windows/build-only tool, so it is deliberately not a
   `pyproject.toml` dependency):

   ```powershell
   uv sync
   uv pip install pyinstaller
   ```

2. Build the onedir bundle from the repo root:

   ```powershell
   uv run pyinstaller packaging\windows\argus.spec
   ```

   Output lands at `packaging\windows\dist\argus\` (an *onedir* build —
   see the spec file's top-of-file comment for why onedir was chosen over
   onefile: onefile's per-launch extraction-to-temp is slow and fragile
   for an app bundling many small Jinja2 template files, and a poor fit
   for a process that immediately spawns a long-running daemon thread).
   `dist\argus\Argus.exe` is the entry point; running it starts the
   daemon (with the dashboard served in-process, same as `argus run` on
   Linux) on a background thread and then runs the tray icon on the main
   thread — see `src/argus/windows_main.py`.

3. Compile the installer with **Inno Setup**'s command-line compiler
   (install Inno Setup from https://jrsoftware.org/isinfo.php first):

   ```powershell
   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\windows\argus.iss
   ```

   Output: `packaging\windows\Output\argus-setup-0.1.0.exe` (Inno's
   default `OutputDir` when unset).

## What the installer does

- Installs the onedir bundle under `%LOCALAPPDATA%\Programs\Argus`
  (`PrivilegesRequired=lowest` — per-user install, no admin prompt, matches
  the "just double-click, no manual steps" install experience from the
  spec).
- Registers the **Argus** Task Scheduler task (logon trigger,
  restart-on-crash) by generating a copy of `Argus.xml` with the real
  installed `Argus.exe` path substituted in (Inno's `[Code]` section,
  see `argus.iss`'s `CreateArgusTaskXml` — this mirrors what
  `src/argus/service.py`'s `install_windows()` does in Python, kept
  separate deliberately so the shipped installer doesn't have to invoke
  the just-installed app once merely to get it to self-register) and
  importing it via `schtasks.exe /Create ... /XML ...` from an Inno
  `[Run]` entry.
- Offers a "Launch Argus" checkbox after install (`[Run]`,
  `postinstall skipifsilent`).
- On uninstall, ends and deletes the scheduled task (`[UninstallRun]`).

## Known gotchas to verify on real Windows (not yet verified here)

- **UTF-16 vs UTF-8 for `Argus.xml`.** `schtasks /Create /XML` officially
  wants UTF-16; the Inno Pascal Script in `argus.iss` currently writes
  the substituted XML with `SaveStringToFile`, which is 8-bit. This is
  flagged explicitly in a comment in `argus.iss` — `schtasks` often
  tolerates a UTF-8-declared-as-UTF-16 file in practice, but this needs
  an actual run to confirm; if it fails, switch to writing a UTF-16LE
  file with a BOM (Inno has no built-in helper for this — either shell
  out to PowerShell via `Exec()`, or use `SaveStringToUTF8File` +
  `WideCharToMultiByte`-equivalent trick, or just skip the XML
  substitution entirely and register the task with a plain `schtasks
  /Create /SC ONLOGON /TR ...` one-liner instead, at the cost of losing
  the XML's `RestartOnFailure` setting — see the fallback one-liner
  documented below).
- **Jinja2 template bundling.** This was checked and fixed as part of
  this packaging work — see "Templates-bundling fix" below. Re-verify by
  actually opening the dashboard (`http://127.0.0.1:8477`) from the
  built, installed `.exe` and confirming pages render (not a 500 from a
  missing `TemplateNotFound`).
- **`console=False` in `argus.spec`.** No console window is intended
  (this is a tray-icon background app) — confirm logging isn't silently
  swallowed; if diagnosing a real Windows issue, temporarily flip
  `console=True` and rebuild to see stdout/stderr.
- **PyInstaller hidden imports.** The `hiddenimports` list in
  `argus.spec` is a best guess at what uvicorn/fastapi/pystray need
  dynamically; if the built `.exe` errors on startup with an
  `ImportError`/`ModuleNotFoundError`, add the missing module there and
  rebuild.
- **pygobject / dbus-next.** These are Linux-only
  (`sys_platform == 'linux'` markers in `pyproject.toml`), so they are
  not expected to be pulled in by PyInstaller's Windows build at all —
  confirm the built `dist\argus\` tree doesn't contain them (it
  shouldn't, since `uv sync`/`pip install` on Windows never installs
  them in the first place).

### Templates-bundling fix (already made, part of this packaging pass)

Before this packaging pass, `src/argus/dashboard/app.py` resolved its
Jinja2 templates directory as `Path(__file__).parent / "templates"`. This
is generally fine even inside a PyInstaller-frozen app's `sys._MEIPASS`
extraction dir, but is not guaranteed across PyInstaller versions/modes,
so it was made explicit and defensive: the module now checks
`sys._MEIPASS` first (set only inside a frozen app) and only falls back
to `Path(__file__).parent / "templates"` for normal dev/`uv`/`pip`
installs. See the `TEMPLATES_DIR` assignment near the top of
`src/argus/dashboard/app.py`.

## Manual Task Scheduler fallback / what `argus service install` does

`argus service install` (from `src/argus/service.py`) is a separate,
programmatic path for registering the same kind of task — usable for a
manual/dev install (`uv run argus service install`) without building or
running the `.exe` installer at all:

- Trigger: **at logon** (current user).
- Action: runs the installed `argus` entry point in the background
  (`pythonw.exe -m argus.cli run` — `pythonw`, not `python`, so no console
  window pops up).
- Restart-on-crash: Task Scheduler's own `RestartOnFailure` (interval 1
  minute, up to 999 restarts) restarts the task if the process exits.
- Runs in the interactive logon session (not `RunOnlyIfLoggedOn`-less
  service mode), because screen/window/camera capture needs an
  interactive desktop.

```powershell
schtasks /Create /TN "Argus" /XML "Argus.xml" /F
```

`argus service install` resolves the current Python/argus install location
and writes a copy of `Argus.xml` with `<Command>`/`<Arguments>` pointed at
that interpreter before importing it, then runs the above.

Equivalent one-liner without the XML file (also usable by hand):

```powershell
schtasks /Create /TN "Argus" /SC ONLOGON /RL LIMITED ^
  /TR "\"C:\path\to\pythonw.exe\" -m argus.cli run" /F
```

(`RestartOnFailure`/restart-count is not exposed via plain `schtasks`
flags — only via XML — hence `Argus.xml` is the source of truth; the
one-liner above is a fallback that gets you logon-start but not the
scheduler-level auto-restart.)

## Uninstall

```powershell
schtasks /End /TN "Argus"
schtasks /Delete /TN "Argus" /F
```

## Status

```powershell
schtasks /Query /TN "Argus" /V /FO LIST
```
