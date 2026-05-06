#!/usr/bin/env python3
"""
TENEBRA LAUNCHER - per VEIN
GUI moderna in stile Tenebra. Auto-detect Steam, sync mod, avvio gioco.
"""

import os
import re
import sys
import json
import shutil
import platform
import zipfile
import threading
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

import webview

# ============================================================
# CONFIGURAZIONE
# ============================================================
DATA_URL = "https://raw.githubusercontent.com/Whis1/Server-Tenebra/main/data.json"
LAUNCHER_VERSION = "1.4.4"
GAME_NAME = "VEIN"
GAME_FOLDER_NAME = "Vein"  # nome cartella sotto steamapps/common
# ============================================================

IS_WINDOWS = platform.system() == "Windows"
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home() / ".config")) / "Tenebra" if IS_WINDOWS else Path.home() / ".config" / "Tenebra"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "launcher.log"


def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ============================================================
# STEAM AUTO-DETECT
# ============================================================

def get_steam_install() -> Path | None:
    """Cerca l'installazione di Steam (registro + percorsi standard)."""
    if IS_WINDOWS:
        try:
            import winreg
            for hive, key_path in [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            ]:
                try:
                    with winreg.OpenKey(hive, key_path) as k:
                        val, _ = winreg.QueryValueEx(k, "InstallPath" if hive == winreg.HKEY_LOCAL_MACHINE else "SteamPath")
                        p = Path(val)
                        if p.exists():
                            return p
                except (FileNotFoundError, OSError):
                    continue
        except ImportError:
            pass
        # Fallback path
        for p in [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam",
        ]:
            if p.exists():
                return p
    else:
        for p in [Path.home() / ".steam" / "steam", Path.home() / ".local" / "share" / "Steam"]:
            if p.exists():
                return p
    return None


def get_steam_libraries(steam_path: Path) -> list[Path]:
    """Restituisce tutte le cartelle steamapps di Steam (libreria principale + secondarie)."""
    libs = []
    main = steam_path / "steamapps"
    if main.exists():
        libs.append(main)
    vdf = steam_path / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        try:
            content = vdf.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'"path"\s+"([^"]+)"', content):
                raw = m.group(1).replace("\\\\", "\\")
                p = Path(raw) / "steamapps"
                if p.exists() and p not in libs:
                    libs.append(p)
        except Exception as e:
            log(f"[WARN] Parse libraryfolders.vdf: {e}")
    return libs


def detect_game_install() -> dict | None:
    """Auto-detect dell'installazione di VEIN su Steam.
    Restituisce dict con 'root' (Vein outer), 'mod_root' (Vein inner), 'exe' (Vein.exe da lanciare).
    """
    steam = get_steam_install()
    if not steam:
        log("Steam non trovato")
        return None
    log(f"Steam: {steam}")

    for lib in get_steam_libraries(steam):
        common = lib / "common"
        if not common.exists():
            continue
        # Cerca cartella case-insensitive
        candidates = [d for d in common.iterdir() if d.is_dir() and d.name.lower() == GAME_FOLDER_NAME.lower()]
        for game_root in candidates:
            log(f"Trovata cartella: {game_root}")
            # Eseguibile principale: <root>\Vein.exe (wrapper Steam) o nome cartella
            exe_candidates = [
                game_root / f"{GAME_FOLDER_NAME}.exe",
                game_root / f"{game_root.name}.exe",
            ]
            # Se esiste sotto-cartella interna UE (es. Vein\Vein), il mod_root è quella
            inner = next((d for d in game_root.iterdir() if d.is_dir() and d.name.lower() == GAME_FOLDER_NAME.lower()), None)
            mod_root = inner if inner else game_root

            # Cerca anche binaries shipping nell'inner come fallback exe
            if mod_root != game_root:
                ue_bin = mod_root / "Binaries" / "Win64" if IS_WINDOWS else mod_root / "Binaries" / "Linux"
                if ue_bin.is_dir():
                    if IS_WINDOWS:
                        exe_candidates += list(ue_bin.glob("*-Win64-Shipping.exe"))
                    else:
                        exe_candidates += list(ue_bin.glob("*-Linux-Shipping"))

            exe = next((e for e in exe_candidates if e.exists() and e.is_file()), None)
            if exe:
                return {"root": str(game_root), "mod_root": str(mod_root), "exe": str(exe)}
            log(f"Nessun eseguibile trovato in {game_root}")
    return None


def fetch_data():
    try:
        url = DATA_URL + ("&" if "?" in DATA_URL else "?") + f"t={int(datetime.now().timestamp())}"
        req = urllib.request.Request(url, headers={"User-Agent": f"TenebraLauncher/{LAUNCHER_VERSION}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"[ERRORE] Lettura dati: {e}")
        return None


def resolve_mod_path(mod_root: Path, mod: dict) -> Path:
    raw = mod.get("path_windows") if IS_WINDOWS else (mod.get("path_linux") or mod.get("path_windows"))
    if not raw:
        return mod_root
    raw = raw.replace("\\", "/").strip("/")
    parts = Path(raw).parts
    # Se il primo componente coincide col nome della cartella mod_root, lo togliamo
    if parts and parts[0].lower() == mod_root.name.lower():
        parts = parts[1:]
    return mod_root.joinpath(*parts) if parts else mod_root


def find_game_executable(game_root: Path):
    """Fallback se non abbiamo già un exe in config: cerca in vari path."""
    if not game_root.exists():
        return None
    bad = ("uninstall", "redist", "crashreport", "epicgames", "ueprereq", "directx")

    def good(p: Path) -> bool:
        return p.exists() and p.is_file() and not any(k in p.name.lower() for k in bad)

    candidates = []
    if IS_WINDOWS:
        # Priorità: <root>/Vein.exe (Steam wrapper)
        candidates += [game_root / f"{GAME_FOLDER_NAME}.exe", game_root / f"{game_root.name}.exe"]
        # Inner UE folder
        inner = next((d for d in game_root.iterdir() if d.is_dir() and d.name.lower() == GAME_FOLDER_NAME.lower()), None) if game_root.is_dir() else None
        if inner:
            ue_bin = inner / "Binaries" / "Win64"
            if ue_bin.is_dir():
                candidates += list(ue_bin.glob("*-Win64-Shipping.exe"))
        # Generic exe in root
        if game_root.is_dir():
            candidates += list(game_root.glob("*.exe"))
    else:
        candidates += [game_root / GAME_FOLDER_NAME, game_root / game_root.name]
        inner = next((d for d in game_root.iterdir() if d.is_dir() and d.name.lower() == GAME_FOLDER_NAME.lower()), None) if game_root.is_dir() else None
        if inner:
            ue_bin = inner / "Binaries" / "Linux"
            if ue_bin.is_dir():
                candidates += list(ue_bin.glob("*-Linux-Shipping"))

    return next((c for c in candidates if good(c)), None)


# ============================================================
# API esposta al frontend
# ============================================================

class Api:
    def __init__(self):
        self.cfg = load_config()
        self.data = None
        self.window = None

    def set_window(self, w):
        self.window = w

    def js(self, code: str):
        if self.window:
            try:
                self.window.evaluate_js(code)
            except Exception:
                pass

    def get_state(self):
        return {
            "version": LAUNCHER_VERSION,
            "game_root": self.cfg.get("game_root", ""),
            "mod_root": self.cfg.get("mod_root", ""),
            "exe_path": self.cfg.get("exe_path", ""),
            "installed_mods": self.cfg.get("installed_mods", {}),
            "is_windows": IS_WINDOWS,
            "game_name": GAME_NAME,
        }

    def auto_detect(self):
        """Cerca VEIN in tutte le librerie Steam."""
        result = detect_game_install()
        if result:
            # se cambia game_root, reset mod installate
            if result["mod_root"] != self.cfg.get("mod_root"):
                self.cfg["installed_mods"] = {}
            self.cfg["game_root"] = result["root"]
            self.cfg["mod_root"] = result["mod_root"]
            self.cfg["exe_path"] = result["exe"]
            save_config(self.cfg)
            log(f"Detect OK: root={result['root']} exe={result['exe']}")
        return result

    def manual_pick(self):
        """Apre dialog cartella. L'utente sceglie la cartella radice del gioco (quella con Vein.exe)."""
        if not self.window:
            return None
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=False)
        if not result:
            return None
        chosen = Path(result[0]).resolve()
        if not chosen.is_dir():
            return None
        # Risali se l'utente ha scelto la sotto-cartella interna
        game_root = chosen
        if chosen.name.lower() == GAME_FOLDER_NAME.lower() and chosen.parent.name.lower() == GAME_FOLDER_NAME.lower():
            game_root = chosen.parent
        # mod_root: inner se esiste, altrimenti chosen
        inner = next((d for d in game_root.iterdir() if d.is_dir() and d.name.lower() == GAME_FOLDER_NAME.lower()), None)
        mod_root = inner if inner else game_root
        exe = find_game_executable(game_root)
        info = {"root": str(game_root), "mod_root": str(mod_root), "exe": str(exe) if exe else ""}

        if mod_root and str(mod_root) != self.cfg.get("mod_root"):
            self.cfg["installed_mods"] = {}
        self.cfg["game_root"] = info["root"]
        self.cfg["mod_root"] = info["mod_root"]
        self.cfg["exe_path"] = info["exe"]
        save_config(self.cfg)
        return info

    def reset_game(self):
        """Cancella la configurazione del gioco (torna alla libreria)."""
        for k in ["game_root", "mod_root", "exe_path", "installed_mods"]:
            self.cfg.pop(k, None)
        save_config(self.cfg)
        return True

    def fetch(self):
        self.data = fetch_data()
        return self.data

    def install_all(self):
        if not self.data:
            self.js("setStatus('error', 'Dati non disponibili')")
            return False
        if not self.cfg.get("mod_root"):
            self.js("setStatus('error', 'Gioco non rilevato')")
            return False
        threading.Thread(target=self._install_all_thread, daemon=True).start()
        return True

    def _install_all_thread(self):
        success = False
        error_msg = ""
        try:
            mod_root = Path(self.cfg["mod_root"])
            mods = [m for m in (self.data.get("mods") or []) if m.get("status") == "active"]
            installed = self.cfg.get("installed_mods", {})

            to_do = [m for m in mods if m.get("id") and (m["id"] not in installed or installed[m["id"]] != m.get("version"))]

            if not to_do:
                self.js("setStatus('ok', 'Mod già aggiornate')")
                success = True
                return

            total = len(to_do)
            for i, mod in enumerate(to_do, 1):
                name = mod.get("name", "?")
                self.js(f"setSyncStep('Sincronizzazione mod ({i}/{total})', '{self._js_escape(name)}')")
                ok = self._install_single(mod, mod_root)
                if ok:
                    self.cfg.setdefault("installed_mods", {})[mod["id"]] = mod.get("version")
                    save_config(self.cfg)
                else:
                    error_msg = f"Errore installando {name}"
                    self.js(f"setStatus('error', '{self._js_escape(error_msg)}')")
                    return

            self.js("setStatus('ok', 'Mod sincronizzate')")
            self.js("setProgress(0)")
            success = True
        except Exception as e:
            error_msg = str(e)
            log(f"[ERRORE] _install_all_thread: {e}")
        finally:
            self.js(f"syncComplete({str(success).lower()}, '{self._js_escape(error_msg)}')")

    def _install_single(self, mod, mod_root):
        url = mod.get("url")
        if not url:
            return False
        dest_dir = resolve_mod_path(mod_root, mod)
        tmp_file = CONFIG_DIR / "tmp" / f"{mod['id']}.tmp"
        tmp_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            def hook(count, block_size, total_size):
                if total_size > 0:
                    pct = min(int(count * block_size * 100 / total_size), 100)
                    self.js(f"setProgress({pct})")

            urllib.request.urlretrieve(url, tmp_file, hook)
            log(f"Scaricato {mod['name']} da {url}")

            dest_dir.mkdir(parents=True, exist_ok=True)
            if url.lower().split("?")[0].endswith(".zip"):
                try:
                    with zipfile.ZipFile(tmp_file, "r") as z:
                        z.extractall(dest_dir)
                except zipfile.BadZipFile:
                    shutil.copy2(tmp_file, dest_dir / Path(url.split("?")[0]).name)
            else:
                fname = Path(url.split("?")[0]).name
                shutil.copy2(tmp_file, dest_dir / fname)

            try:
                tmp_file.unlink(missing_ok=True)
            except Exception:
                pass
            return True
        except Exception as e:
            log(f"[ERRORE] install {mod.get('name')}: {e}")
            return False

    def launch_game(self):
        exe_path = self.cfg.get("exe_path")
        if not exe_path or not Path(exe_path).exists():
            # tenta di rilevarlo di nuovo
            game_root = self.cfg.get("game_root")
            if game_root:
                found = find_game_executable(Path(game_root))
                if found:
                    self.cfg["exe_path"] = str(found)
                    save_config(self.cfg)
                    exe_path = str(found)
        if not exe_path or not Path(exe_path).exists():
            self.js("setStatus('error', 'Eseguibile VEIN non trovato')")
            return False

        exe = Path(exe_path)
        log(f"Avvio gioco: {exe}")
        try:
            if IS_WINDOWS:
                # Popen evita ShellExecute (no UAC se non richiesto)
                subprocess.Popen([str(exe)], cwd=str(exe.parent),
                                 creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                subprocess.Popen([str(exe)], cwd=str(exe.parent), start_new_session=True)
            self.js(f"setStatus('ok', 'Avvio: {self._js_escape(exe.name)}')")
            return True
        except Exception as e:
            # Fallback su startfile
            try:
                if IS_WINDOWS:
                    os.startfile(str(exe))
                    self.js(f"setStatus('ok', 'Avvio: {self._js_escape(exe.name)}')")
                    return True
            except Exception as e2:
                log(f"[ERRORE] avvio fallback: {e2}")
            log(f"[ERRORE] avvio: {e}")
            self.js(f"setStatus('error', 'Errore avvio: {self._js_escape(str(e))}')")
            return False

    def open_log(self):
        try:
            if IS_WINDOWS:
                os.startfile(str(LOG_FILE))
            else:
                os.system(f'xdg-open "{LOG_FILE}"')
        except Exception:
            pass

    def check_update(self):
        """Confronta la versione locale con quella in data.json.
        Ritorna {'available': bool, 'remote_version': str, 'notes': str, 'url': str} o None.
        """
        if not self.data:
            return None
        l = self.data.get("launcher") or {}
        remote_v = (l.get("version") or "").strip()
        url = (l.get("exe_url_windows") if IS_WINDOWS else l.get("exe_url_linux")) or ""
        if not remote_v or not url:
            return {"available": False, "remote_version": remote_v, "notes": l.get("notes", ""), "url": url}
        try:
            cur = tuple(int(x) for x in LAUNCHER_VERSION.split("."))
            rem = tuple(int(x) for x in remote_v.split("."))
            available = rem > cur
        except Exception:
            available = remote_v != LAUNCHER_VERSION
        return {"available": available, "remote_version": remote_v, "notes": l.get("notes", ""), "url": url}

    def perform_update(self):
        """Scarica nuovo .exe, fa swap e riavvia."""
        info = self.check_update()
        if not info or not info.get("url"):
            return False
        threading.Thread(target=self._update_thread, args=(info["url"],), daemon=True).start()
        return True

    def _update_thread(self, url):
        try:
            self.js("setStatus('working', 'Download aggiornamento...')")
            current_exe = Path(sys.executable)
            update_dir = CONFIG_DIR / "update"
            update_dir.mkdir(parents=True, exist_ok=True)
            new_exe = update_dir / "TenebraLauncher.new.exe"

            def hook(count, block_size, total_size):
                if total_size > 0:
                    pct = min(int(count * block_size * 100 / total_size), 100)
                    self.js(f"setProgress({pct})")

            urllib.request.urlretrieve(url, new_exe, hook)
            log(f"Aggiornamento scaricato in {new_exe}")

            if not IS_WINDOWS:
                # Linux: copia diretta
                try:
                    shutil.copy2(new_exe, current_exe)
                    new_exe.unlink(missing_ok=True)
                    self.js("setStatus('ok', 'Aggiornato. Riavvia il launcher.')")
                except Exception as e:
                    log(f"[ERRORE] Update Linux: {e}")
                    self.js(f"setStatus('error', 'Errore: {self._js_escape(str(e))}')")
                return

            # Windows: bat robusto con logging, retry, copy come fallback
            bat_file = update_dir / "updater.bat"
            log_file = update_dir / "update.log"
            bat_content = (
                "@echo off\r\n"
                "setlocal enableextensions\r\n"
                f'set "LOG={log_file}"\r\n'
                f'set "NEW={new_exe}"\r\n'
                f'set "CUR={current_exe}"\r\n'
                '> "%LOG%" echo [%date% %time%] === Update started ===\r\n'
                '>> "%LOG%" echo NEW: %NEW%\r\n'
                '>> "%LOG%" echo CUR: %CUR%\r\n'
                '\r\n'
                'set /a WAITS=0\r\n'
                ':wait\r\n'
                'tasklist /FI "IMAGENAME eq TenebraLauncher.exe" /NH 2>NUL | find /I "TenebraLauncher.exe" >NUL\r\n'
                'if not errorlevel 1 (\r\n'
                '    set /a WAITS+=1\r\n'
                '    if %WAITS% gtr 30 goto fail_timeout\r\n'
                '    timeout /t 1 /nobreak > nul\r\n'
                '    goto wait\r\n'
                ')\r\n'
                '>> "%LOG%" echo [%date% %time%] Old launcher exited (waited %WAITS%s)\r\n'
                'timeout /t 3 /nobreak > nul\r\n'
                '\r\n'
                'set /a TRIES=0\r\n'
                ':swap\r\n'
                'set /a TRIES+=1\r\n'
                '>> "%LOG%" echo [%date% %time%] Copy attempt %TRIES%\r\n'
                'copy /y /b "%NEW%" "%CUR%" >> "%LOG%" 2>&1\r\n'
                'if not errorlevel 1 goto swap_ok\r\n'
                'if %TRIES% lss 20 (\r\n'
                '    timeout /t 2 /nobreak > nul\r\n'
                '    goto swap\r\n'
                ')\r\n'
                'goto fail_swap\r\n'
                '\r\n'
                ':swap_ok\r\n'
                '>> "%LOG%" echo [%date% %time%] Swap OK after %TRIES% attempts\r\n'
                'del "%NEW%" >nul 2>&1\r\n'
                '>> "%LOG%" echo [%date% %time%] Starting new launcher\r\n'
                'start "" "%CUR%"\r\n'
                '>> "%LOG%" echo [%date% %time%] === Done ===\r\n'
                'exit /b 0\r\n'
                '\r\n'
                ':fail_timeout\r\n'
                '>> "%LOG%" echo [%date% %time%] TIMEOUT: old launcher still running, aborting\r\n'
                'start "" "%CUR%"\r\n'
                'exit /b 1\r\n'
                '\r\n'
                ':fail_swap\r\n'
                '>> "%LOG%" echo [%date% %time%] FAILED to copy after %TRIES% attempts, starting old\r\n'
                'start "" "%CUR%"\r\n'
                'exit /b 2\r\n'
            )
            bat_file.write_text(bat_content, encoding="ascii", errors="replace")
            log(f"Updater bat: {bat_file}")

            time_to_quit = 1.0
            # Lancia updater detached, senza finestra console
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                ["cmd", "/c", str(bat_file)],
                creationflags=(subprocess.DETACHED_PROCESS
                               | subprocess.CREATE_NEW_PROCESS_GROUP
                               | CREATE_NO_WINDOW),
                close_fds=True,
            )
            # Chiudi la finestra (poi processo termina e bat fa swap)
            self.js(f"setTimeout(() => window.close && window.close(), {int(time_to_quit*1000)})")
            # In caso pywebview non chiuda, esci hard
            threading.Timer(time_to_quit + 1.5, lambda: os._exit(0)).start()
        except Exception as e:
            log(f"[ERRORE] Update: {e}")
            self.js(f"setStatus('error', 'Errore aggiornamento: {self._js_escape(str(e))}')")

    def open_folder(self, kind):
        try:
            target = self.cfg.get("game_root") if kind == "game" else self.cfg.get("mod_root")
            if target and Path(target).exists():
                if IS_WINDOWS:
                    os.startfile(target)
                else:
                    os.system(f'xdg-open "{target}"')
        except Exception:
            pass

    def _js_escape(self, s):
        return str(s).replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ").replace("\r", "")


# ============================================================
# UI HTML
# ============================================================

HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8" />
<title>Tenebra Launcher</title>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;700;900&family=Crimson+Pro:wght@300;400&display=swap" rel="stylesheet" />
<style>
  :root {
    --bg: #070a0e; --bg2: #0d1017; --surface: #111520; --surface2: #161b25;
    --border: #1e2535; --accent: #c8302a; --accent2: #e8472f;
    --gold: #b8972a; --text: #d4cfc8; --text-dim: #6b6860; --text-head: #ede8e0;
    --green: #4aaa74; --green-bg: rgba(74,170,116,.12);
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; overflow: hidden; }
  body {
    background: var(--bg); color: var(--text);
    font-family: 'Crimson Pro', Georgia, serif; font-size: 15px;
    user-select: none; -webkit-user-select: none;
  }
  body::before {
    content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 9999;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.04'/%3E%3C/svg%3E");
    opacity: .35;
  }

  .app { display: flex; flex-direction: column; height: 100vh; }

  /* HEADER */
  header {
    height: 64px; padding: 0 28px;
    display: flex; align-items: center; justify-content: space-between;
    background: linear-gradient(180deg, var(--surface) 0%, transparent 100%);
    border-bottom: 1px solid rgba(200,48,42,.2);
    flex-shrink: 0; -webkit-app-region: drag;
  }
  header > * { -webkit-app-region: no-drag; }
  .header-left { display: flex; align-items: center; gap: 24px; }
  .logo {
    font-family: 'Cinzel', serif; font-size: 22px; font-weight: 900;
    letter-spacing: .25em; color: var(--text-head); cursor: default;
  }
  .logo span { color: var(--accent); }
  .nav-tabs { display: flex; gap: 4px; }
  .nav-tab {
    font-family: 'Cinzel', serif; font-size: 10px; letter-spacing: .25em;
    text-transform: uppercase; color: var(--text-dim);
    padding: 8px 16px; cursor: pointer; transition: color .15s;
    border-bottom: 2px solid transparent;
  }
  .nav-tab:hover { color: var(--text); }
  .nav-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .nav-tab:disabled, .nav-tab[disabled="true"] { opacity: .3; pointer-events: none; }
  .header-right { display: flex; align-items: center; gap: 18px; font-size: 12px; }
  .ver { font-family: 'Cinzel', serif; font-size: 10px; letter-spacing: .25em; color: var(--text-dim); }
  .conn-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--green); margin-right: 6px; box-shadow: 0 0 8px var(--green); }
  .conn-dot.off { background: var(--accent); box-shadow: 0 0 8px var(--accent); }
  .conn-text { font-family: 'Cinzel', serif; font-size: 10px; letter-spacing: .2em; color: var(--text-dim); text-transform: uppercase; }

  /* PAGES */
  .page { flex: 1; display: none; overflow: hidden; }
  .page.active { display: flex; }

  /* === LIBRARY === */
  #page-library {
    flex-direction: column; padding: 48px 64px; overflow-y: auto;
    background:
      radial-gradient(ellipse 60% 50% at 30% 20%, rgba(200,48,42,.08) 0%, transparent 70%),
      radial-gradient(ellipse 50% 40% at 80% 80%, rgba(184,151,42,.05) 0%, transparent 70%),
      var(--bg);
  }
  .lib-header { margin-bottom: 36px; }
  .lib-eyebrow { font-family: 'Cinzel', serif; font-size: 10px; letter-spacing: .4em; color: var(--accent); text-transform: uppercase; margin-bottom: 8px; }
  .lib-title { font-family: 'Cinzel', serif; font-size: 32px; font-weight: 700; letter-spacing: .08em; color: var(--text-head); margin-bottom: 6px; }
  .lib-sub { color: var(--text-dim); font-style: italic; font-size: 15px; }

  .lib-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 20px; max-width: 1400px;
  }

  .game-tile {
    aspect-ratio: 3 / 4;
    background: var(--surface);
    border: 1px solid var(--border);
    cursor: pointer;
    position: relative; overflow: hidden;
    transition: transform .25s ease, border-color .25s, box-shadow .25s;
    display: flex; flex-direction: column;
  }
  .game-tile:hover {
    transform: translateY(-4px) scale(1.01);
    border-color: var(--accent);
    box-shadow: 0 12px 40px rgba(200,48,42,.25);
  }
  .game-tile-art {
    flex: 1;
    background:
      radial-gradient(ellipse 70% 50% at 50% 30%, rgba(200,48,42,.4) 0%, transparent 60%),
      radial-gradient(ellipse 80% 60% at 50% 100%, rgba(8,10,14,1) 0%, rgba(8,10,14,.7) 60%),
      linear-gradient(180deg, #2a1014 0%, #0a0508 100%);
    position: relative; display: flex; align-items: center; justify-content: center;
  }
  .game-tile-art::before {
    content: ''; position: absolute; inset: 0;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 400 600' xmlns='http://www.w3.org/2000/svg'%3E%3Cdefs%3E%3Cfilter id='f'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.02' numOctaves='3'/%3E%3CfeColorMatrix values='0 0 0 0 0.78  0 0 0 0 0.18  0 0 0 0 0.16  0 0 0 0.4 0'/%3E%3C/filter%3E%3C/defs%3E%3Crect width='400' height='600' filter='url(%23f)' opacity='.3'/%3E%3C/svg%3E");
    mix-blend-mode: screen; opacity: .5;
  }
  .game-tile-name {
    font-family: 'Cinzel', serif; font-size: 56px; font-weight: 900;
    letter-spacing: .15em; color: var(--text-head);
    text-shadow: 0 0 30px rgba(200,48,42,.6), 0 0 60px rgba(0,0,0,.8);
    z-index: 1; transition: transform .3s;
  }
  .game-tile:hover .game-tile-name { transform: scale(1.05); }
  .game-tile-overlay {
    position: absolute; inset: 0; background: rgba(7,10,14,.85);
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 14px; opacity: 0; transition: opacity .25s; z-index: 2;
  }
  .game-tile:hover .game-tile-overlay { opacity: 1; }
  .game-tile-play {
    background: var(--accent); color: #fff;
    font-family: 'Cinzel', serif; font-size: 12px; letter-spacing: .3em;
    text-transform: uppercase; padding: 14px 32px; border: none;
    box-shadow: 0 0 30px rgba(200,48,42,.5);
  }
  .game-tile-foot {
    padding: 14px 16px;
    background: var(--bg2); border-top: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }
  .game-tile-info {
    font-family: 'Cinzel', serif; font-size: 11px; letter-spacing: .15em;
    color: var(--text-head); text-transform: uppercase;
  }
  .game-tile-meta {
    font-size: 11px; color: var(--text-dim); font-family: 'Cinzel', serif; letter-spacing: .15em;
  }

  .lib-actions { margin-top: 32px; display: flex; gap: 12px; flex-wrap: wrap; }
  .lib-actions button {
    background: transparent; color: var(--text-dim); border: 1px solid var(--border);
    font-family: 'Cinzel', serif; font-size: 10px; letter-spacing: .2em;
    text-transform: uppercase; padding: 10px 20px; cursor: pointer;
    transition: border-color .15s, color .15s;
  }
  .lib-actions button:hover { border-color: var(--accent); color: var(--accent); }

  /* === GAME PAGE === */
  #page-game { flex-direction: column; }
  .game-main { flex: 1; display: flex; overflow: hidden; }

  /* CONTENT (a tutta larghezza, niente sidebar) */
  .content {
    flex: 1; padding: 36px 56px; overflow-y: auto;
    background: radial-gradient(ellipse 70% 40% at 50% 0%, rgba(200,48,42,.06) 0%, transparent 70%), var(--bg);
  }
  .content-actions {
    display: flex; gap: 10px; align-items: center;
  }
  .btn-icon {
    background: transparent; color: var(--text-dim);
    border: 1px solid var(--border);
    font-family: 'Cinzel', serif; font-size: 10px; letter-spacing: .2em;
    text-transform: uppercase; padding: 9px 16px; cursor: pointer;
    transition: all .15s;
  }
  .btn-icon:hover { border-color: var(--accent); color: var(--accent); }
  .content-header {
    display: flex; align-items: baseline; justify-content: space-between;
    margin-bottom: 24px; padding-bottom: 18px; border-bottom: 1px solid var(--border);
  }
  .content-title { font-family: 'Cinzel', serif; font-size: 24px; font-weight: 700; color: var(--text-head); letter-spacing: .08em; }
  .content-sub { font-size: 13px; color: var(--text-dim); font-style: italic; margin-top: 4px; }
  .btn-sync {
    background: rgba(200,48,42,.12); color: var(--accent);
    border: 1px solid rgba(200,48,42,.4);
    font-family: 'Cinzel', serif; font-size: 10px; letter-spacing: .2em;
    text-transform: uppercase; padding: 9px 20px; cursor: pointer;
    transition: all .15s;
  }
  .btn-sync:hover:not(:disabled) { background: rgba(200,48,42,.25); }
  .btn-sync:disabled { opacity: .4; cursor: not-allowed; }

  .mods-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
  .mod-card {
    background: var(--surface); border: 1px solid var(--border); padding: 22px;
    transition: border-color .2s, box-shadow .2s;
    display: flex; flex-direction: column; gap: 10px;
  }
  .mod-card:hover { border-color: var(--gold); }
  .mod-card.installed { border-color: rgba(74,170,116,.4); }
  .mod-card.update { border-color: rgba(184,151,42,.6); box-shadow: 0 0 0 1px rgba(184,151,42,.2); }
  .mod-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
  .mod-name { font-family: 'Cinzel', serif; font-size: 15px; color: var(--text-head); letter-spacing: .04em; }
  .mod-version { font-family: 'Cinzel', serif; font-size: 9px; letter-spacing: .15em; color: var(--gold); padding: 2px 8px; border: 1px solid rgba(184,151,42,.3); white-space: nowrap; }
  .mod-desc { font-size: 13px; color: var(--text-dim); line-height: 1.55; flex: 1; min-height: 40px; }
  .mod-meta { display: flex; gap: 12px; align-items: center; padding-top: 10px; border-top: 1px solid var(--border); font-size: 11px; }
  .mod-size { color: var(--text-dim); font-family: 'Cinzel', serif; letter-spacing: .1em; }
  .mod-status { margin-left: auto; font-family: 'Cinzel', serif; font-size: 9px; letter-spacing: .2em; text-transform: uppercase; padding: 3px 8px; }
  .status-installed { color: var(--green); background: var(--green-bg); border: 1px solid rgba(74,170,116,.3); }
  .status-update { color: var(--gold); background: rgba(184,151,42,.1); border: 1px solid rgba(184,151,42,.3); }
  .status-new { color: var(--accent); background: rgba(200,48,42,.1); border: 1px solid rgba(200,48,42,.3); }

  .empty-state { text-align: center; padding: 60px 20px; color: var(--text-dim); font-style: italic; }
  .empty-state-icon { font-size: 36px; margin-bottom: 14px; opacity: .4; }

  /* PLAY BAR */
  footer.play-bar {
    height: 92px; padding: 16px 32px; flex-shrink: 0;
    background: linear-gradient(0deg, var(--surface) 0%, var(--bg2) 100%);
    border-top: 1px solid var(--border);
    display: flex; align-items: center; gap: 24px;
  }
  .status-area { flex: 1; }
  .status-text { font-size: 13px; color: var(--text-dim); margin-bottom: 6px; min-height: 16px; }
  .status-text.ok { color: var(--green); }
  .status-text.error { color: var(--accent); }
  .status-text.working { color: var(--gold); }
  .progress-bar { height: 4px; background: var(--border); position: relative; overflow: hidden; }
  .progress-fill { height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent2)); width: 0%; transition: width .2s; }

  .btn-play {
    background: var(--accent); color: #fff;
    font-family: 'Cinzel', serif; font-size: 14px; font-weight: 700;
    letter-spacing: .3em; text-transform: uppercase;
    padding: 18px 48px; border: none; cursor: pointer;
    transition: all .2s; box-shadow: 0 0 30px rgba(200,48,42,.4);
    white-space: nowrap;
  }
  .btn-play:hover:not(:disabled) {
    background: var(--accent2); box-shadow: 0 0 50px rgba(200,48,42,.6);
    transform: translateY(-1px);
  }
  .btn-play:disabled { opacity: .35; cursor: not-allowed; box-shadow: none; }

  /* LAUNCH MODAL */
  .modal {
    position: fixed; inset: 0; background: rgba(7,10,14,.92);
    display: none; align-items: center; justify-content: center;
    z-index: 100; padding: 40px; backdrop-filter: blur(4px);
  }
  .modal.show { display: flex; }
  .modal-box {
    background: var(--surface); border: 1px solid var(--border);
    max-width: 520px; width: 100%; padding: 40px;
  }
  .modal h3 {
    font-family: 'Cinzel', serif; font-size: 22px; letter-spacing: .15em;
    color: var(--text-head); margin-bottom: 28px; text-align: center;
    font-weight: 700;
  }
  .modal h3 span { color: var(--accent); }
  .modal p { color: var(--text); margin-bottom: 14px; line-height: 1.7; font-size: 14px; }
  .modal .small { color: var(--text-dim); font-size: 12px; font-style: italic; text-align: center; }

  .steps { display: flex; flex-direction: column; gap: 14px; margin-bottom: 28px; }
  .step {
    display: flex; align-items: center; gap: 14px;
    padding: 12px 14px; border: 1px solid var(--border); background: var(--bg2);
    transition: all .25s;
  }
  .step.active { border-color: var(--gold); background: rgba(184,151,42,.06); }
  .step.done { border-color: rgba(74,170,116,.4); background: rgba(74,170,116,.05); }
  .step.error { border-color: var(--accent); background: rgba(200,48,42,.06); }
  .step-icon {
    width: 26px; height: 26px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    border-radius: 50%; font-size: 14px;
    border: 1px solid var(--border); color: var(--text-dim);
  }
  .step.active .step-icon {
    border-color: var(--gold); color: var(--gold);
  }
  .step.active .step-icon::before {
    content: ''; width: 12px; height: 12px;
    border: 2px solid var(--gold); border-top-color: transparent;
    border-radius: 50%; animation: spin 1s linear infinite;
  }
  .step.done .step-icon { border-color: var(--green); color: var(--green); background: var(--green-bg); }
  .step.done .step-icon::before { content: '✓'; font-weight: 700; }
  .step.error .step-icon { border-color: var(--accent); color: var(--accent); background: rgba(200,48,42,.1); }
  .step.error .step-icon::before { content: '✕'; font-weight: 700; }
  .step-text {
    flex: 1; font-family: 'Cinzel', serif; font-size: 12px;
    letter-spacing: .15em; text-transform: uppercase; color: var(--text-dim);
  }
  .step.active .step-text, .step.done .step-text { color: var(--text-head); }
  .step.error .step-text { color: var(--accent); }
  .step-detail {
    margin-left: 40px; font-size: 12px; color: var(--text-dim);
    font-family: 'Crimson Pro', serif; font-style: italic;
    margin-top: 4px; min-height: 18px;
  }
  .step.active .step-detail { color: var(--gold); }

  .modal-progress {
    height: 4px; background: var(--border); position: relative;
    overflow: hidden; margin-bottom: 18px; display: none;
  }
  .modal-progress.show { display: block; }
  .modal-progress-fill {
    height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent2));
    width: 0%; transition: width .2s;
  }

  .modal-actions { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; }
  .modal-actions button {
    font-family: 'Cinzel', serif; font-size: 11px; letter-spacing: .2em;
    text-transform: uppercase; padding: 12px 24px; cursor: pointer;
    border: 1px solid var(--border); background: transparent; color: var(--text-dim);
    transition: all .15s;
  }
  .modal-actions button:hover { border-color: var(--accent); color: var(--accent); }
  .modal-actions button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  .modal-actions button.primary:hover { background: var(--accent2); border-color: var(--accent2); color: #fff; }

  @keyframes spin { to { transform: rotate(360deg); } }

  ::-webkit-scrollbar { width: 8px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); }
  ::-webkit-scrollbar-thumb:hover { background: var(--accent); }

  /* UPDATE BANNER */
  .update-banner {
    background: linear-gradient(90deg, rgba(184,151,42,.18), rgba(200,48,42,.15));
    border-bottom: 1px solid rgba(184,151,42,.4);
    padding: 12px 28px; display: none;
    align-items: center; justify-content: space-between; gap: 16px;
    flex-shrink: 0;
  }
  .update-banner.show { display: flex; }
  .update-info { flex: 1; min-width: 0; }
  .update-title {
    font-family: 'Cinzel', serif; font-size: 11px; letter-spacing: .25em;
    color: var(--gold); text-transform: uppercase; margin-bottom: 2px;
  }
  .update-desc { font-size: 13px; color: var(--text); }
  .update-actions { display: flex; gap: 10px; flex-shrink: 0; }
  .update-btn {
    font-family: 'Cinzel', serif; font-size: 10px; letter-spacing: .2em;
    text-transform: uppercase; padding: 8px 18px; border: 1px solid var(--border);
    background: transparent; color: var(--text-dim); cursor: pointer;
    transition: all .15s;
  }
  .update-btn:hover { border-color: var(--text-head); color: var(--text-head); }
  .update-btn.primary { background: var(--gold); color: #1a1208; border-color: var(--gold); }
  .update-btn.primary:hover { background: #d4ad33; color: #0d0904; }
</style>
</head>
<body>

<!-- LAUNCH MODAL -->
<div class="modal" id="modal-launch">
  <div class="modal-box">
    <h3 id="launch-title">Avvio <span>VEIN</span></h3>

    <div class="steps">
      <div class="step" id="step-detect">
        <div class="step-icon"></div>
        <div style="flex:1;">
          <div class="step-text">Rilevamento installazione</div>
          <div class="step-detail" id="step-detect-detail">In attesa...</div>
        </div>
      </div>
      <div class="step" id="step-sync">
        <div class="step-icon"></div>
        <div style="flex:1;">
          <div class="step-text">Sincronizzazione mod</div>
          <div class="step-detail" id="step-sync-detail">In attesa...</div>
        </div>
      </div>
      <div class="step" id="step-launch">
        <div class="step-icon"></div>
        <div style="flex:1;">
          <div class="step-text">Avvio gioco</div>
          <div class="step-detail" id="step-launch-detail">In attesa...</div>
        </div>
      </div>
    </div>

    <div class="modal-progress" id="modal-progress">
      <div class="modal-progress-fill" id="modal-progress-fill"></div>
    </div>

    <div class="modal-actions" id="launch-actions" style="display:none;">
      <button onclick="closeLaunchModal()" id="btn-close">Chiudi</button>
      <button onclick="manualPick()" id="btn-manual" style="display:none;">Seleziona cartella manualmente</button>
      <button onclick="retryLaunch()" id="btn-retry" class="primary" style="display:none;">Riprova</button>
    </div>
  </div>
</div>

<div class="app">

  <!-- UPDATE BANNER -->
  <div class="update-banner" id="update-banner">
    <div class="update-info">
      <div class="update-title">⬆ Aggiornamento disponibile</div>
      <div class="update-desc" id="update-desc">Una nuova versione del launcher è pronta.</div>
    </div>
    <div class="update-actions">
      <button class="update-btn" onclick="dismissUpdate()">Più tardi</button>
      <button class="update-btn primary" onclick="doUpdate()">⬇ Aggiorna ora</button>
    </div>
  </div>

  <header>
    <div class="header-left">
      <div class="logo"><span>T</span>ENEBRA</div>
    </div>
    <div class="header-right">
      <span class="ver" id="version-label">v—</span>
      <span><span class="conn-dot" id="conn-dot"></span><span class="conn-text" id="conn-text">…</span></span>
    </div>
  </header>

  <!-- LIBRARY (unica pagina) -->
  <div class="page active" id="page-library">
    <div class="lib-header">
      <div class="lib-eyebrow">La tua libreria</div>
      <div class="lib-title">Tenebra Library</div>
      <div class="lib-sub">Clicca su un gioco per giocare con le mod sincronizzate.</div>
    </div>

    <div class="lib-grid">
      <div class="game-tile" id="tile-vein" onclick="startLaunch()">
        <div class="game-tile-art">
          <div class="game-tile-name">VEIN</div>
        </div>
        <div class="game-tile-overlay">
          <button class="game-tile-play">▶ Avvia</button>
          <div style="font-size:11px;color:var(--text-dim);font-family:'Cinzel',serif;letter-spacing:.15em;">Mod + lancio in un click</div>
        </div>
        <div class="game-tile-foot">
          <span class="game-tile-info">VEIN</span>
          <span class="game-tile-meta" id="tile-status">Steam</span>
        </div>
      </div>
    </div>

    <div class="lib-actions">
      <button onclick="reDetect()" id="btn-redetect" style="display:none;">↺ Re-rileva installazione</button>
    </div>
  </div>
</div>

<script>
let api = null;
let state = null;
let data = null;

window.addEventListener('pywebviewready', async () => {
  api = window.pywebview.api;
  state = await api.get_state();
  document.getElementById('version-label').textContent = 'v' + state.version;

  // Connetti e carica dati
  data = await api.fetch();
  setConnection(!!data);

  // Controlla aggiornamenti launcher
  if (data) {
    const upd = await api.check_update();
    if (upd && upd.available) {
      const banner = document.getElementById('update-banner');
      const desc = document.getElementById('update-desc');
      desc.textContent = `Versione v${upd.remote_version} disponibile (sei alla v${state.version}).` + (upd.notes ? ' ' + upd.notes : '');
      banner.classList.add('show');
    }
  }

  if (state.game_root && state.exe_path) {
    showGameInstalled();
  }
});

function dismissUpdate() {
  document.getElementById('update-banner').classList.remove('show');
}

async function doUpdate() {
  document.getElementById('update-banner').classList.remove('show');
  setStep('detect', 'done', 'Saltato');
  setStep('sync', 'done', 'Saltato');
  setStep('launch', 'active', 'Aggiornamento launcher in corso...');
  showProgressBar(true);
  document.getElementById('launch-title').innerHTML = 'Aggiornamento <span>Tenebra</span>';
  document.getElementById('modal-launch').classList.add('show');
  await api.perform_update();
}

function setConnection(ok) {
  document.getElementById('conn-dot').classList.toggle('off', !ok);
  document.getElementById('conn-text').textContent = ok ? 'Online' : 'Offline';
}

function showGameInstalled() {
  document.getElementById('tile-status').textContent = '✓ PRONTO';
  document.getElementById('tile-status').style.color = 'var(--green)';
  const btn = document.getElementById('btn-redetect');
  if (btn) btn.style.display = 'inline-block';
}

async function reDetect() {
  await api.reset_game();
  state = await api.get_state();
  document.getElementById('tile-status').textContent = 'Steam';
  document.getElementById('tile-status').style.color = '';
  document.getElementById('btn-redetect').style.display = 'none';
  setTimeout(() => startLaunch(), 200);
}

function esc(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ============ MODAL HELPERS ============
function resetSteps() {
  ['detect','sync','launch'].forEach(id => {
    const el = document.getElementById('step-' + id);
    el.classList.remove('active','done','error');
    document.getElementById('step-' + id + '-detail').textContent = 'In attesa...';
  });
  setProgress(0);
  showProgressBar(false);
  document.getElementById('launch-actions').style.display = 'none';
  document.getElementById('btn-manual').style.display = 'none';
  document.getElementById('btn-retry').style.display = 'none';
}

function setStep(id, state, detail) {
  const el = document.getElementById('step-' + id);
  el.classList.remove('active','done','error');
  if (state) el.classList.add(state);
  if (detail !== undefined) {
    document.getElementById('step-' + id + '-detail').textContent = detail;
  }
}

function showProgressBar(show) {
  document.getElementById('modal-progress').classList.toggle('show', show);
}

function setProgress(pct) {
  document.getElementById('modal-progress-fill').style.width = pct + '%';
}

function setSyncStep(label, modName) {
  setStep('sync', 'active', `${label}: ${modName}`);
}

// no-op compat (chiamate da Python che non hanno più una destinazione UI)
function setStatus(){}

function showLaunchActions(opts) {
  document.getElementById('launch-actions').style.display = 'flex';
  document.getElementById('btn-manual').style.display = opts.manual ? 'inline-flex' : 'none';
  document.getElementById('btn-retry').style.display = opts.retry ? 'inline-flex' : 'none';
}

function closeLaunchModal() {
  document.getElementById('modal-launch').classList.remove('show');
}

// ============ MAIN LAUNCH FLOW ============
let _syncResolve = null;
window.syncComplete = (success, errorMsg) => {
  if (_syncResolve) {
    const r = _syncResolve;
    _syncResolve = null;
    r({success: success, error: errorMsg || ''});
  }
};

async function startLaunch() {
  resetSteps();
  document.getElementById('launch-title').innerHTML = 'Avvio <span>VEIN</span>';
  document.getElementById('modal-launch').classList.add('show');

  // STEP 1: Rilevamento
  state = await api.get_state();
  if (state.game_root && state.exe_path) {
    setStep('detect', 'done', state.game_root);
  } else {
    setStep('detect', 'active', 'Cerco VEIN nelle librerie Steam...');
    const result = await api.auto_detect();
    if (!result) {
      setStep('detect', 'error', 'VEIN non trovato nelle librerie Steam');
      showLaunchActions({manual: true, retry: false});
      return;
    }
    state = await api.get_state();
    setStep('detect', 'done', state.game_root);
    showGameInstalled();
  }

  // STEP 2: Sincronizzazione mod
  if (!data) {
    setStep('sync', 'error', 'Server Tenebra non raggiungibile');
    showLaunchActions({retry: true});
    return;
  }

  const mods = (data.mods || []).filter(m => m.status === 'active');
  const installed = state.installed_mods || {};
  const needs = mods.filter(m => !installed[m.id] || installed[m.id] !== m.version);

  if (mods.length === 0) {
    setStep('sync', 'done', 'Nessuna mod disponibile');
  } else if (needs.length === 0) {
    setStep('sync', 'done', `${mods.length} mod già aggiornate`);
  } else {
    setStep('sync', 'active', `${needs.length} mod da scaricare...`);
    showProgressBar(true);
    const syncResult = await new Promise(resolve => {
      _syncResolve = resolve;
      api.install_all();
    });
    showProgressBar(false);
    setProgress(0);
    if (!syncResult.success) {
      setStep('sync', 'error', syncResult.error || 'Errore durante sincronizzazione');
      showLaunchActions({retry: true});
      return;
    }
    state = await api.get_state();
    setStep('sync', 'done', `${needs.length} mod installate`);
  }

  // STEP 3: Avvio gioco
  setStep('launch', 'active', 'Lancio VEIN...');
  const ok = await api.launch_game();
  if (ok) {
    setStep('launch', 'done', 'Gioco avviato. Buona fortuna.');
    setTimeout(() => window.close && window.close(), 1800);
  } else {
    setStep('launch', 'error', 'Errore durante l\\'avvio del gioco');
    showLaunchActions({retry: true});
  }
}

async function manualPick() {
  closeLaunchModal();
  const result = await api.manual_pick();
  if (result && result.exe) {
    state = await api.get_state();
    showGameInstalled();
    setTimeout(() => startLaunch(), 200);
  } else if (result) {
    alert('Eseguibile non trovato in quella cartella. Riprova selezionando la cartella radice di VEIN.');
  }
}

function retryLaunch() {
  closeLaunchModal();
  setTimeout(() => startLaunch(), 100);
}
</script>
</body>
</html>
"""


def main():
    api = Api()
    window = webview.create_window(
        'Tenebra Launcher',
        html=HTML,
        js_api=api,
        width=1200,
        height=780,
        min_size=(1000, 640),
        background_color='#070a0e',
        text_select=False,
    )
    api.set_window(window)
    webview.start(debug=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"[CRASH] {e}")
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk(); root.withdraw()
            messagebox.showerror("Tenebra Launcher", f"Errore critico:\n\n{e}")
        except Exception:
            print(f"Errore critico: {e}")
            input("Premi INVIO per uscire...")
        sys.exit(1)
