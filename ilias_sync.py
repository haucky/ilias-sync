#!/usr/bin/env python3
"""
ilias-sync – One-Way-Sync aller Dateien aus deinen Kursen auf myILIAS (KH Freiburg)
in ein lokales Verzeichnis. Lädt nur neue oder geänderte Dateien herunter.

Läuft auf macOS, Windows und Linux.

Befehle:
  ilias-sync                       Ohne Befehl: Einrichtung (falls nötig), dann Sync
  ilias-sync setup                 Zugangsdaten + Zielordner einrichten (Passwort -> Schlüsselbund
                                   bzw. Windows-Anmeldeinformationsverwaltung)
  ilias-sync courses               Gefundene Kurse/Gruppen auflisten
  ilias-sync sync [--dry-run]      Synchronisieren
  ilias-sync schedule --hours 2    Automatisch alle 2 Stunden synchronisieren
                                   (macOS: launchd, Windows: Aufgabenplanung, Linux: systemd-Timer)
  ilias-sync schedule --remove     Automatik wieder entfernen
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote, urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Fehlende Pakete. Bitte installieren:  pip3 install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)

# --------------------------------------------------------------------------- Konstanten

__version__ = "1.1.0"

BASE_URL = "https://myilias.kh-freiburg.de/"
CLIENT_ID = "kh-freiburg"
APP = "ilias-sync"
STATE_NAME = ".ilias-sync-state.json"

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
FROZEN = getattr(sys, "frozen", False)  # als PyInstaller-Binary gestartet

if IS_WINDOWS:
    _roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    _local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    CONFIG_DIR = _roaming / APP
    DEBUG_DIR = _local / APP / "debug"
    LOG_FILE = _local / APP / f"{APP}.log"
elif IS_MACOS:
    CONFIG_DIR = Path.home() / ".config" / APP
    DEBUG_DIR = Path.home() / ".cache" / APP / "debug"
    LOG_FILE = Path.home() / "Library" / "Logs" / f"{APP}.log"
else:
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP
    DEBUG_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / APP / "debug"
    LOG_FILE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / APP / f"{APP}.log"
CONFIG_FILE = CONFIG_DIR / "config.json"

KEYCHAIN_SERVICE = "ilias-sync-kh-freiburg"
LAUNCHD_LABEL = "de.kh-freiburg.ilias-sync"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
WIN_TASK_NAME = "ilias-sync"
SYSTEMD_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
PW_STORE = ("in der Windows-Anmeldeinformationsverwaltung" if IS_WINDOWS
            else "im macOS-Schlüsselbund" if IS_MACOS else "im System-Schlüsselbund")

AUTH_MODES = {
    "student": "Studenten AD",
    "mitarbeiter": "Mitarbeiter AD",
    "ilias": "ILIAS-Datenbank",
}

# Objekttypen, in die rekursiv hineingeschaut wird
CONTAINER_TYPES = {"fold", "grp", "sess"}
MAX_DEPTH = 20

VERBOSE = False
DEBUG = False


def log(msg: str) -> None:
    print(msg, flush=True)


def vlog(msg: str) -> None:
    if VERBOSE:
        print("  · " + msg, flush=True)


# --------------------------------------------------------------------------- Config / Schlüsselbund

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log("Noch nicht eingerichtet. Bitte zuerst ausführen:  ilias-sync setup")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# Unter macOS wird das Passwort über /usr/bin/security abgelegt: Der Schlüsselbund-Eintrag vertraut dann
# diesem Systemprogramm statt dem (unsignierten, bei jedem Update wechselnden) Binary – keine Rückfragen.
# Windows/Linux nutzen das keyring-Paket (Anmeldeinformationsverwaltung bzw. Secret Service).

def keychain_get(user: str) -> Optional[str]:
    env = os.environ.get("ILIAS_PASSWORD")
    if env:
        return env
    if IS_MACOS:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"],
            capture_output=True, text=True,
        )
        return r.stdout.rstrip("\n") if r.returncode == 0 else None
    try:
        import keyring
        return keyring.get_password(KEYCHAIN_SERVICE, user)
    except Exception as e:  # noqa
        vlog(f"Schlüsselbund nicht verfügbar: {e}")
        return None


def keychain_set(user: str, password: str) -> None:
    if IS_MACOS:
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", user, "-w", password],
            check=True, capture_output=True,
        )
        return
    import keyring
    keyring.set_password(KEYCHAIN_SERVICE, user, password)


# --------------------------------------------------------------------------- Hilfsfunktionen

def safe_name(name: str, max_len: int = 150) -> str:
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[\/\\:\*\?"<>\|\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if not name:
        name = "unbenannt"
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)(\..*)?", name):  # unter Windows reserviert
        name = "_" + name
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            name = stem[: max_len - len(ext) - 1].rstrip() + "." + ext
        else:
            name = name[:max_len].rstrip()
    return name


def ref_from_href(href: str) -> Optional[int]:
    for pat in (r"[?&]ref_id=(\d+)", r"goto\.php/[a-z]+/(\d+)", r"[?&]target=[a-z]+_(\d+)"):
        m = re.search(pat, href)
        if m:
            return int(m.group(1))
    return None


def type_from_href(href: str) -> Optional[str]:
    m = re.search(r"goto\.php/([a-z]+)/\d+", href) or re.search(r"[?&]target=([a-z]+)_\d+", href)
    if m:
        return m.group(1)
    if "cmd=sendfile" in href:
        return "file"
    return None


def type_from_icon(el) -> Optional[str]:
    if el is None:
        return None
    for img in el.find_all("img"):
        m = re.search(r"icon_([a-z]+)\.svg", img.get("src", ""))
        if m and m.group(1) not in ("custom", "checked", "alert"):
            return m.group(1)
    return None


def filename_from_cd(cd: Optional[str]) -> Optional[str]:
    if not cd:
        return None
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", cd)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd) or re.search(r"filename\s*=\s*([^;]+)", cd)
    if m:
        raw = m.group(1).strip()
        try:  # requests dekodiert Header als latin-1
            return raw.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return raw
    return None


def file_signature(props: str) -> str:
    """Aus den Eigenschaften (Größe, Version, Datum) einen stabilen Änderungs-Fingerabdruck bauen."""
    parts = []
    parts += re.findall(r"\d+(?:[.,]\d+)?\s*(?:Bytes|KB|MB|GB|kB)", props)
    parts += re.findall(r"Version:?\s*\d+", props, flags=re.I)
    parts += re.findall(r"\d{1,2}\.\s*\w+\.?\s*\d{4}(?:,?\s*\d{1,2}:\d{2})?", props)
    parts += re.findall(r"\d{1,2}\.\d{1,2}\.\d{2,4}(?:,?\s*\d{1,2}:\d{2})?", props)
    parts += re.findall(r"(?:Heute|Gestern|Today|Yesterday),?\s*\d{1,2}:\d{2}", props)
    return " | ".join(parts) if parts else props


def ext_from_props(props: str) -> str:
    first = props.split("|")[0].strip().lower()
    return "." + first if re.fullmatch(r"[a-z0-9]{1,6}", first) else ""


def is_login_page(html: str) -> bool:
    return bool(re.search(r'type="password"', html)) and ("login" in html.lower() or "ilstartupgui" in html.lower())


_WIN_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:ILIAS_NOTIFY_TITLE)) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:ILIAS_NOTIFY_MSG)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
    [Windows.UI.Notifications.ToastNotification]::new($t))
"""


def notify(title: str, msg: str) -> None:
    try:
        if IS_MACOS:
            msg = msg.replace('"', "'")
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
                           capture_output=True)
        elif IS_WINDOWS:
            env = dict(os.environ, ILIAS_NOTIFY_TITLE=title, ILIAS_NOTIFY_MSG=msg)
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WIN_TOAST],
                           env=env, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, msg], capture_output=True)
    except Exception as e:  # noqa – eine fehlende Mitteilung soll den Sync nie abbrechen
        vlog(f"Mitteilung fehlgeschlagen: {e}")


# --------------------------------------------------------------------------- Parsing

@dataclass
class Item:
    type: str
    ref_id: int
    title: str
    url: str
    props: str


def parse_items(html: str, page_url: str) -> List[Item]:
    """Listeneinträge (Dateien, Ordner, Kurse ...) aus einer ILIAS-Seite extrahieren."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#ilContentContainer") or soup.find("main") or soup
    items: Dict[int, Item] = {}

    # Klassische Container-Listen (Kurs-, Ordner-, Gruppenansicht)
    for li in root.select("div.il_ContainerListItem"):
        a = li.select_one("a.il_ContainerItemTitle") or li.select_one(".il_ContainerItemTitle a")
        if not a or not a.get("href"):
            continue
        href = urljoin(page_url, a["href"])
        ref = ref_from_href(href)
        if ref is None:
            continue
        outer = li.find_parent(class_=re.compile(r"ilContainerListItemOuter")) or li.parent
        typ = type_from_icon(outer) or type_from_href(href)
        props = " | ".join(p.get_text(" ", strip=True) for p in li.select(".il_ItemProperty"))
        if typ and ref not in items:
            items[ref] = Item(typ, ref, a.get_text(" ", strip=True), href, props)

    # Neuere UI-Komponenten (Dashboard / Mitgliedschaften in ILIAS 8/9)
    for it in root.select("div.il-item, div.il-std-item"):
        a = it.select_one(".il-item-title a")
        if not a or not a.get("href"):
            continue
        href = urljoin(page_url, a["href"])
        ref = ref_from_href(href)
        if ref is None or ref in items:
            continue
        typ = type_from_href(href) or type_from_icon(it)
        props = " | ".join(p.get_text(" ", strip=True) for p in it.select(".il-item-property-value"))
        if typ:
            items[ref] = Item(typ, ref, a.get_text(" ", strip=True), href, props)

    return list(items.values())


# --------------------------------------------------------------------------- ILIAS-Client

class LoginError(Exception):
    pass


class Ilias:
    def __init__(self, username: str, password: str, auth_mode: str):
        self.username = username
        self.password = password
        self.auth_mode = auth_mode
        self.s = requests.Session()
        self.s.headers["User-Agent"] = f"{APP}/{__version__} ({platform.system()})"
        self.logged_in = False

    def _dump(self, name: str, html: str) -> None:
        if DEBUG:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            (DEBUG_DIR / f"{int(time.time()*1000)}_{safe_name(name, 60)}.html").write_text(html, encoding="utf-8")

    def login(self) -> None:
        r = self.s.get(urljoin(BASE_URL, f"login.php?client_id={CLIENT_ID}&cmd=force_login&lang=de"), timeout=30)
        self._dump("login_page", r.text)
        soup = BeautifulSoup(r.text, "html.parser")
        pw = soup.find("input", attrs={"type": "password"})
        if not pw:
            raise LoginError("Login-Formular nicht gefunden (Seitenstruktur geändert?)")
        form = pw.find_parent("form")
        action = urljoin(r.url, form.get("action") or r.url)

        data: Dict[str, str] = {}
        radios: Dict[str, list] = {}
        user_set = False
        for inp in form.find_all(["input", "select", "textarea"]):
            name = inp.get("name")
            if not name:
                continue
            t = (inp.get("type") or "text").lower()
            if t == "radio":
                radios.setdefault(name, []).append(inp)
            elif t in ("submit", "button", "image", "reset"):
                continue
            elif t == "checkbox":
                if inp.has_attr("checked"):
                    data[name] = inp.get("value", "on")
            elif t == "password":
                data[name] = self.password
            elif t in ("text", "email") and not user_set:
                data[name] = self.username
                user_set = True
            elif inp.name == "select":
                opt = inp.find("option", selected=True) or inp.find("option")
                data[name] = opt.get("value", "") if opt else ""
            else:
                data[name] = inp.get("value", "")

        # Anmeldungsart (Studenten AD / Mitarbeiter AD / ILIAS-Datenbank) wählen
        wanted = AUTH_MODES.get(self.auth_mode, self.auth_mode).lower()
        for name, opts in radios.items():
            chosen = None
            for o in opts:
                label = ""
                if o.get("id"):
                    lab = soup.find("label", attrs={"for": o["id"]})
                    label = lab.get_text(" ", strip=True) if lab else ""
                label = label or (o.parent.get_text(" ", strip=True) if o.parent else "")
                hay = label.lower()
                if wanted in hay or (self.auth_mode == "ilias" and "datenbank" in hay):
                    chosen = o
                    break
            chosen = chosen or next((o for o in opts if o.has_attr("checked")), opts[0])
            data[name] = chosen.get("value", "")
            vlog(f"Anmeldungsart: {chosen.get('value')}")

        btn = form.find(["button", "input"], attrs={"type": "submit"})
        if btn is not None and btn.get("name"):
            data[btn["name"]] = btn.get("value", "")

        r = self.s.post(action, data=data, timeout=30)
        self._dump("after_login", r.text)
        if is_login_page(r.text) and "logout" not in r.text.lower():
            raise LoginError("Anmeldung fehlgeschlagen – Benutzername, Passwort oder Anmeldungsart prüfen "
                             "(ilias-sync setup).")
        self.logged_in = True
        vlog("Angemeldet.")

    def get(self, url: str) -> requests.Response:
        if not self.logged_in:
            self.login()
        url = urljoin(BASE_URL, url)
        for attempt in range(3):
            try:
                r = self.s.get(url, timeout=60)
            except requests.RequestException as e:
                if attempt == 2:
                    raise
                vlog(f"Netzwerkfehler ({e}), neuer Versuch …")
                time.sleep(3)
                continue
            if "text/html" in r.headers.get("Content-Type", "") and is_login_page(r.text) \
                    and "logout" not in r.text.lower():
                vlog("Sitzung abgelaufen – melde neu an.")
                self.login()
                continue
            self._dump(url.split("/")[-1], r.text if "text/html" in r.headers.get("Content-Type", "") else "")
            return r
        return r

    @staticmethod
    def container_url(ref_id: int) -> str:
        return f"ilias.php?baseClass=ilrepositorygui&ref_id={ref_id}"

    def courses(self) -> List[Item]:
        pages = [
            "ilias.php?baseClass=ilmembershipoverviewgui",
            "ilias.php?baseClass=ilDashboardGUI&cmd=jumpToMemberships",
            "ilias.php?baseClass=ilDashboardGUI&cmd=show",
        ]
        found: Dict[int, Item] = {}
        for p in pages:
            r = self.get(p)
            for it in parse_items(r.text, r.url):
                if it.type in ("crs", "grp"):
                    found.setdefault(it.ref_id, it)
            if found:
                break
        return sorted(found.values(), key=lambda i: i.title.lower())

    def children(self, ref_id: int) -> List[Item]:
        r = self.get(self.container_url(ref_id))
        return parse_items(r.text, r.url)


# --------------------------------------------------------------------------- Sync

class Syncer:
    def __init__(self, ilias: Ilias, root: Path, dry_run: bool):
        self.il = ilias
        self.root = root
        self.dry = dry_run
        self.state_file = root / STATE_NAME
        self.state: Dict[str, dict] = json.loads(self.state_file.read_text(encoding="utf-8")) \
            if self.state_file.exists() else {}
        self.visited: set = set()
        self.new: List[str] = []
        self.updated: List[str] = []
        self.errors: List[str] = []

    def save_state(self) -> None:
        if self.dry:
            return
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def owner_of(self, rel: str) -> Optional[str]:
        for ref, st in self.state.items():
            if st.get("path") == rel:
                return ref
        return None

    def sync_course(self, course: Item) -> None:
        log(f"▸ {course.title}")
        self.walk(course.ref_id, self.root / safe_name(course.title), 0)

    def walk(self, ref_id: int, local_dir: Path, depth: int) -> None:
        if ref_id in self.visited or depth > MAX_DEPTH:
            return
        self.visited.add(ref_id)
        try:
            children = self.il.children(ref_id)
        except Exception as e:  # noqa
            self.errors.append(f"{local_dir}: {e}")
            return
        used_names: Dict[str, int] = {}
        for it in children:
            if it.type == "file":
                self.sync_file(it, local_dir)
            elif it.type in CONTAINER_TYPES:
                name = safe_name(it.title)
                if used_names.get(name, it.ref_id) != it.ref_id:
                    name = f"{name} ({it.ref_id})"
                used_names[name] = it.ref_id
                vlog(f"→ {it.type}: {it.title}")
                self.walk(it.ref_id, local_dir / name, depth + 1)

    def sync_file(self, it: Item, local_dir: Path) -> None:
        key = str(it.ref_id)
        sig = file_signature(it.props)
        st = self.state.get(key)
        if st and st.get("sig") == sig and (self.root / st["path"]).exists():
            vlog(f"unverändert: {st['path']}")
            return
        if self.dry:
            label = "geändert" if st else "neu"
            log(f"  [{label}] {(local_dir / safe_name(it.title)).relative_to(self.root)}")
            return

        url = it.url if ("download" in it.url or "sendfile" in it.url) \
            else urljoin(BASE_URL, f"goto.php?target=file_{it.ref_id}_download&client_id={CLIENT_ID}")
        try:
            r = self.il.s.get(url, stream=True, timeout=120)
            if "text/html" in r.headers.get("Content-Type", "") and not r.headers.get("Content-Disposition"):
                r.close()
                self.il.login()
                r = self.il.s.get(url, stream=True, timeout=120)
                if "text/html" in r.headers.get("Content-Type", "") and not r.headers.get("Content-Disposition"):
                    raise RuntimeError("Download liefert eine Webseite statt einer Datei")
            r.raise_for_status()

            fname = filename_from_cd(r.headers.get("Content-Disposition")) or (it.title + ext_from_props(it.props))
            fname = safe_name(fname)
            target = local_dir / fname
            rel = str(target.relative_to(self.root))
            owner = self.owner_of(rel)
            if owner and owner != key:  # gleicher Name, anderes ILIAS-Objekt
                stem, ext = os.path.splitext(fname)
                target = local_dir / f"{stem} ({it.ref_id}){ext}"
                rel = str(target.relative_to(self.root))

            # Lokal bearbeitete Version nicht stillschweigend überschreiben
            if st:
                old = self.root / st["path"]
                if old.exists():
                    s = old.stat()
                    if int(s.st_mtime) != st.get("mtime") or s.st_size != st.get("size"):
                        backup = old.with_name(f"{old.stem} (lokal {time.strftime('%Y-%m-%d %H%M')}){old.suffix}")
                        old.rename(backup)
                        log(f"  ! lokale Änderung gesichert: {backup.name}")
                    elif old != target:
                        old.unlink()  # in ILIAS umbenannt/verschoben

            local_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=local_dir, prefix=".ilias-dl-")
            with os.fdopen(fd, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
            os.replace(tmp, target)
            s = target.stat()
            self.state[key] = {"path": rel, "sig": sig, "mtime": int(s.st_mtime), "size": s.st_size,
                               "title": it.title}
            self.save_state()
            (self.updated if st else self.new).append(rel)
            log(f"  {'↻' if st else '+'} {rel}")
        except Exception as e:  # noqa
            self.errors.append(f"{it.title}: {e}")
            log(f"  ✗ {it.title}: {e}")


# --------------------------------------------------------------------------- Befehle

def make_client(cfg: dict) -> Ilias:
    pw = keychain_get(cfg["username"])
    if not pw:
        log(f"Kein Passwort {PW_STORE} gefunden. Bitte  ilias-sync setup  ausführen "
            "(oder Umgebungsvariable ILIAS_PASSWORD setzen).")
        sys.exit(1)
    return Ilias(cfg["username"], pw, cfg.get("auth_mode", "student"))


def filter_courses(cfg: dict, courses: List[Item]) -> List[Item]:
    inc = [re.compile(p, re.I) for p in cfg.get("include", [])]
    exc = [re.compile(p, re.I) for p in cfg.get("exclude", [])]
    out = []
    for c in courses:
        if inc and not any(p.search(c.title) for p in inc):
            continue
        if any(p.search(c.title) for p in exc):
            continue
        out.append(c)
    return out


def cmd_setup(_args) -> None:
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    print("Einrichtung für myILIAS (KH Freiburg)\n")
    user = input(f"Anmeldename [{cfg.get('username', '')}]: ").strip() or cfg.get("username", "")
    print("Anmeldungsart:  1) Studenten AD   2) Mitarbeiter AD   3) ILIAS-Datenbank")
    default_mode = {"student": "1", "mitarbeiter": "2", "ilias": "3"}.get(cfg.get("auth_mode", "student"), "1")
    choice = input(f"Auswahl [{default_mode}]: ").strip() or default_mode
    mode = {"1": "student", "2": "mitarbeiter", "3": "ilias"}.get(choice, "student")
    pw = getpass.getpass(f"Passwort (wird {PW_STORE} gespeichert): ")
    default_dir = cfg.get("target_dir", str(Path.home() / "ILIAS"))
    target = input(f"Zielordner [{default_dir}]: ").strip() or default_dir
    target = str(Path(target).expanduser().resolve())

    print("\nTeste Anmeldung …")
    il = Ilias(user, pw, mode)
    try:
        il.login()
        courses = il.courses()
    except LoginError as e:
        print(f"✗ {e}")
        sys.exit(1)
    print(f"✓ Angemeldet – {len(courses)} Kurse/Gruppen gefunden.")

    try:
        keychain_set(user, pw)
    except Exception as e:  # noqa
        print(f"✗ Passwort konnte nicht gespeichert werden ({e}).\n"
              "  Alternativ das Passwort über die Umgebungsvariable ILIAS_PASSWORD übergeben.")
    cfg.update({"username": user, "auth_mode": mode, "target_dir": target})
    cfg.setdefault("include", [])
    cfg.setdefault("exclude", [])
    save_config(cfg)
    Path(target).mkdir(parents=True, exist_ok=True)
    print(f"\nGespeichert in {CONFIG_FILE}\nJetzt synchronisieren mit:  ilias-sync sync")


def cmd_courses(_args) -> None:
    cfg = load_config()
    il = make_client(cfg)
    courses = il.courses()
    active = {c.ref_id for c in filter_courses(cfg, courses)}
    for c in courses:
        mark = "✓" if c.ref_id in active else "–"
        print(f"{mark} [{c.type} {c.ref_id}] {c.title}")
    if not courses:
        print("Keine Kurse gefunden. Mit --debug ausführen und die HTML-Dateien prüfen.")


def cmd_sync(args) -> None:
    cfg = load_config()
    root = Path(cfg["target_dir"]).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    il = make_client(cfg)
    started = time.time()
    log(time.strftime("%Y-%m-%d %H:%M:%S") + f" – Sync nach {root}" + (" (Probelauf)" if args.dry_run else ""))
    try:
        courses = filter_courses(cfg, il.courses())
    except LoginError as e:
        log(f"✗ {e}")
        if args.notify:
            notify("ILIAS-Sync", "Anmeldung fehlgeschlagen")
        sys.exit(1)
    if not courses:
        log("Keine Kurse gefunden.")
        return
    syncer = Syncer(il, root, args.dry_run)
    for c in courses:
        syncer.sync_course(c)

    n, u, e = len(syncer.new), len(syncer.updated), len(syncer.errors)
    log(f"Fertig in {time.time() - started:.0f}s: {n} neu, {u} aktualisiert, {e} Fehler.")
    for err in syncer.errors:
        log(f"  Fehler: {err}")
    if args.notify and (n or u):
        names = [Path(p).name for p in (syncer.new + syncer.updated)][:3]
        notify("ILIAS-Sync", f"{n} neue, {u} aktualisierte Dateien: " + ", ".join(names))


def self_command() -> List[str]:
    """Befehl, mit dem sich ilias-sync selbst aufruft (Binary oder Python-Skript)."""
    if FROZEN:
        return [str(Path(sys.executable).resolve())]
    return [str(Path(sys.executable).resolve()), str(Path(__file__).resolve())]


def schedule_macos(args) -> None:
    uid = os.getuid()
    if LAUNCHD_PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(LAUNCHD_PLIST)], capture_output=True)
    if args.remove:
        LAUNCHD_PLIST.unlink(missing_ok=True)
        print("Automatischer Sync entfernt.")
        return
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    prog = "\n".join(f"    <string>{a}</string>" for a in self_command() + ["sync", "--notify"])
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{prog}
  </array>
  <key>StartInterval</key><integer>{int(args.hours * 3600)}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{LOG_FILE}</string>
  <key>StandardErrorPath</key><string>{LOG_FILE}</string>
</dict>
</plist>
"""
    LAUNCHD_PLIST.write_text(plist, encoding="utf-8")
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(LAUNCHD_PLIST)], capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(["launchctl", "load", str(LAUNCHD_PLIST)], check=False)
    print(f"✓ Sync läuft jetzt alle {args.hours:g} Stunde(n) automatisch (und sofort einmal).")
    print(f"  Log: {LOG_FILE}")
    print("  Hinweis: Beim ersten Lauf fragt macOS evtl., ob der Zugriff auf den Schlüsselbund "
          "erlaubt werden soll – „Immer erlauben“ wählen.")


def schedule_windows(args) -> None:
    subprocess.run(["schtasks", "/Delete", "/TN", WIN_TASK_NAME, "/F"], capture_output=True)
    if args.remove:
        print("Automatischer Sync entfernt.")
        return
    # conhost --headless startet das Konsolenprogramm ohne aufpoppendes Fenster
    conhost = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "conhost.exe"
    cmd = subprocess.list2cmdline(self_command() + ["--log-file", str(LOG_FILE), "sync", "--notify"])
    tr = f'"{conhost}" --headless {cmd}' if conhost.exists() else cmd
    minutes = max(1, round(args.hours * 60))
    sched = ["/SC", "MINUTE", "/MO", str(minutes)] if minutes < 1440 \
        else ["/SC", "DAILY", "/MO", str(max(1, round(args.hours / 24)))]
    r = subprocess.run(["schtasks", "/Create", "/TN", WIN_TASK_NAME, "/TR", tr, *sched, "/F"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"✗ Aufgabe konnte nicht angelegt werden: {(r.stderr or r.stdout).strip()}")
        sys.exit(1)
    subprocess.run(["schtasks", "/Run", "/TN", WIN_TASK_NAME], capture_output=True)
    print(f"✓ Sync läuft jetzt alle {args.hours:g} Stunde(n) automatisch (und sofort einmal).")
    print(f"  Aufgabe „{WIN_TASK_NAME}“ in der Windows-Aufgabenplanung.  Log: {LOG_FILE}")


def schedule_linux(args) -> None:
    service = SYSTEMD_DIR / f"{APP}.service"
    timer = SYSTEMD_DIR / f"{APP}.timer"
    if not shutil.which("systemctl"):
        print("systemd nicht gefunden. Alternativ per crontab -e eintragen, z. B.:")
        print(f"  0 */2 * * * {subprocess.list2cmdline(self_command())} sync --notify >> {LOG_FILE} 2>&1")
        sys.exit(1)
    subprocess.run(["systemctl", "--user", "disable", "--now", timer.name], capture_output=True)
    if args.remove:
        service.unlink(missing_ok=True)
        timer.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        print("Automatischer Sync entfernt.")
        return
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    exec_start = subprocess.list2cmdline(self_command() + ["--log-file", str(LOG_FILE), "sync", "--notify"])
    service.write_text(f"[Unit]\nDescription=ilias-sync\n\n[Service]\nType=oneshot\nExecStart={exec_start}\n",
                       encoding="utf-8")
    timer.write_text(f"[Unit]\nDescription=ilias-sync Timer\n\n[Timer]\nOnBootSec=2min\nOnActiveSec=10s\n"
                     f"OnUnitActiveSec={int(args.hours * 3600)}s\n\n"
                     f"[Install]\nWantedBy=timers.target\n", encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    r = subprocess.run(["systemctl", "--user", "enable", "--now", timer.name], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"✗ Timer konnte nicht aktiviert werden: {r.stderr.strip()}")
        sys.exit(1)
    print(f"✓ Sync läuft jetzt alle {args.hours:g} Stunde(n) automatisch (und sofort einmal).")
    print(f"  Log: {LOG_FILE}")


def cmd_schedule(args) -> None:
    if not args.remove:
        load_config()
    if IS_MACOS:
        schedule_macos(args)
    elif IS_WINDOWS:
        schedule_windows(args)
    else:
        schedule_linux(args)


def ask_yes(question: str, default: bool = True) -> bool:
    ans = input(f"{question} [{'J/n' if default else 'j/N'}]: ").strip().lower()
    return default if not ans else ans in ("j", "ja", "y", "yes")


def cmd_default(_args) -> None:
    """Ohne Befehl gestartet (z. B. per Doppelklick): beim ersten Mal einrichten, sonst einfach syncen."""
    sync_args = argparse.Namespace(dry_run=False, notify=False)
    if not CONFIG_FILE.exists():
        cmd_setup(_args)
        print()
        if ask_yes("Automatisch alle 2 Stunden im Hintergrund synchronisieren?"):
            cmd_schedule(argparse.Namespace(hours=2.0, remove=False))
            return  # der Hintergrund-Job synchronisiert sofort einmal
        if not ask_yes("Jetzt synchronisieren?"):
            return
    cmd_sync(sync_args)


def setup_console(log_file: Optional[str]) -> None:
    if log_file:  # Hintergrundläufe (Windows/Linux) schreiben direkt in eine Logdatei
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        f = open(log_file, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
        return
    for stream in (sys.stdout, sys.stderr):  # Windows-Konsolen sind oft nicht UTF-8 (✓, ▸, Umlaute)
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def selftest() -> None:
    """Für die Build-Pipeline: prüft, ob alle Abhängigkeiten im Binary stecken."""
    print(f"{APP} {__version__} auf {platform.system()} {platform.machine()}, Python {platform.python_version()}")
    print(f"requests {requests.__version__}, Konfiguration: {CONFIG_FILE}")
    if not IS_MACOS:
        import keyring
        backend = keyring.get_keyring()
        print(f"keyring-Backend: {type(backend).__module__}.{type(backend).__name__}")
        if IS_WINDOWS and "Windows" not in type(backend).__name__:
            sys.exit("Windows-Anmeldeinformationsverwaltung nicht verfügbar")


def main() -> None:
    global VERBOSE, DEBUG
    p = argparse.ArgumentParser(prog=APP, description="One-Way-Sync deiner myILIAS-Kursdateien (KH Freiburg).")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="ausführliche Ausgabe")
    p.add_argument("--debug", action="store_true", help=f"abgerufene HTML-Seiten nach {DEBUG_DIR} speichern")
    p.add_argument("--log-file", help=argparse.SUPPRESS)
    p.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_default)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="Zugangsdaten und Zielordner einrichten").set_defaults(func=cmd_setup)
    sub.add_parser("courses", help="gefundene Kurse anzeigen").set_defaults(func=cmd_courses)
    s = sub.add_parser("sync", help="Dateien synchronisieren")
    s.add_argument("-n", "--dry-run", action="store_true", help="nur anzeigen, nichts herunterladen")
    s.add_argument("--notify", action="store_true", help="Systemmitteilung bei neuen Dateien")
    s.set_defaults(func=cmd_sync)
    sc = sub.add_parser("schedule", help="automatischen Sync im Hintergrund einrichten")
    sc.add_argument("--hours", type=float, default=2, help="Intervall in Stunden (Standard: 2)")
    sc.add_argument("--remove", action="store_true", help="automatischen Sync entfernen")
    sc.set_defaults(func=cmd_schedule)
    args = p.parse_args()
    VERBOSE, DEBUG = args.verbose, args.debug
    setup_console(args.log_file)
    if args.selftest:
        return selftest()
    # Unter Windows schließt sich das Fenster nach einem Doppelklick sofort – Ausgabe stehen lassen.
    pause = IS_WINDOWS and args.cmd is None and not args.log_file
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        sys.exit(130)
    finally:
        if pause:
            try:
                input("\nEnter drücken zum Schließen …")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    main()
