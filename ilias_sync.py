#!/usr/bin/env python3
"""
ilias-sync – One-Way-Sync aller Dateien aus deinen Kursen auf myILIAS (KH Freiburg)
in ein lokales Verzeichnis. Lädt nur neue oder geänderte Dateien herunter.

Befehle:
  ilias-sync setup                 Zugangsdaten + Zielordner einrichten (Passwort -> macOS-Schlüsselbund)
  ilias-sync courses               Gefundene Kurse/Gruppen auflisten
  ilias-sync sync [--dry-run]      Synchronisieren
  ilias-sync schedule --hours 2    Automatisch alle 2 Stunden synchronisieren (launchd)
  ilias-sync schedule --remove     Automatik wieder entfernen
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
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

BASE_URL = "https://myilias.kh-freiburg.de/"
CLIENT_ID = "kh-freiburg"
APP = "ilias-sync"
CONFIG_DIR = Path.home() / ".config" / APP
CONFIG_FILE = CONFIG_DIR / "config.json"
DEBUG_DIR = Path.home() / ".cache" / APP / "debug"
KEYCHAIN_SERVICE = "ilias-sync-kh-freiburg"
LAUNCHD_LABEL = "de.kh-freiburg.ilias-sync"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
LOG_FILE = Path.home() / "Library" / "Logs" / f"{APP}.log"
STATE_NAME = ".ilias-sync-state.json"

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
    return json.loads(CONFIG_FILE.read_text())


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def keychain_get(user: str) -> Optional[str]:
    env = os.environ.get("ILIAS_PASSWORD")
    if env:
        return env
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def keychain_set(user: str, password: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", user, "-w", password],
        check=True, capture_output=True,
    )


# --------------------------------------------------------------------------- Hilfsfunktionen

def safe_name(name: str, max_len: int = 150) -> str:
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[\/\\:\*\?"<>\|\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if not name:
        name = "unbenannt"
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


def notify(title: str, msg: str) -> None:
    msg = msg.replace('"', "'")
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
                   capture_output=True)


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
        self.s.headers["User-Agent"] = f"{APP}/1.0 (+macOS)"
        self.logged_in = False

    def _dump(self, name: str, html: str) -> None:
        if DEBUG:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            (DEBUG_DIR / f"{int(time.time()*1000)}_{safe_name(name, 60)}.html").write_text(html)

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
        self.state: Dict[str, dict] = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.visited: set = set()
        self.new: List[str] = []
        self.updated: List[str] = []
        self.errors: List[str] = []

    def save_state(self) -> None:
        if self.dry:
            return
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, ensure_ascii=False))
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
        log("Kein Passwort im Schlüsselbund gefunden. Bitte  ilias-sync setup  ausführen.")
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
    cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    print("Einrichtung für myILIAS (KH Freiburg)\n")
    user = input(f"Anmeldename [{cfg.get('username', '')}]: ").strip() or cfg.get("username", "")
    print("Anmeldungsart:  1) Studenten AD   2) Mitarbeiter AD   3) ILIAS-Datenbank")
    default_mode = {"student": "1", "mitarbeiter": "2", "ilias": "3"}.get(cfg.get("auth_mode", "student"), "1")
    choice = input(f"Auswahl [{default_mode}]: ").strip() or default_mode
    mode = {"1": "student", "2": "mitarbeiter", "3": "ilias"}.get(choice, "student")
    pw = getpass.getpass("Passwort (wird im macOS-Schlüsselbund gespeichert): ")
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

    keychain_set(user, pw)
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


def cmd_schedule(args) -> None:
    uid = os.getuid()
    if LAUNCHD_PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(LAUNCHD_PLIST)], capture_output=True)
    if args.remove:
        LAUNCHD_PLIST.unlink(missing_ok=True)
        print("Automatischer Sync entfernt.")
        return
    load_config()
    script = Path(__file__).resolve()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{script}</string>
    <string>sync</string>
    <string>--notify</string>
  </array>
  <key>StartInterval</key><integer>{int(args.hours * 3600)}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{LOG_FILE}</string>
  <key>StandardErrorPath</key><string>{LOG_FILE}</string>
</dict>
</plist>
"""
    LAUNCHD_PLIST.write_text(plist)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(LAUNCHD_PLIST)], capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(["launchctl", "load", str(LAUNCHD_PLIST)], check=False)
    print(f"✓ Sync läuft jetzt alle {args.hours:g} Stunde(n) automatisch (und sofort einmal).")
    print(f"  Log: {LOG_FILE}")
    print("  Hinweis: Beim ersten Lauf fragt macOS evtl., ob der Zugriff auf den Schlüsselbund "
          "erlaubt werden soll – „Immer erlauben“ wählen.")


def main() -> None:
    global VERBOSE, DEBUG
    p = argparse.ArgumentParser(prog=APP, description="One-Way-Sync deiner myILIAS-Kursdateien (KH Freiburg).")
    p.add_argument("-v", "--verbose", action="store_true", help="ausführliche Ausgabe")
    p.add_argument("--debug", action="store_true", help=f"abgerufene HTML-Seiten nach {DEBUG_DIR} speichern")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="Zugangsdaten und Zielordner einrichten").set_defaults(func=cmd_setup)
    sub.add_parser("courses", help="gefundene Kurse anzeigen").set_defaults(func=cmd_courses)
    s = sub.add_parser("sync", help="Dateien synchronisieren")
    s.add_argument("-n", "--dry-run", action="store_true", help="nur anzeigen, nichts herunterladen")
    s.add_argument("--notify", action="store_true", help="macOS-Mitteilung bei neuen Dateien")
    s.set_defaults(func=cmd_sync)
    sc = sub.add_parser("schedule", help="automatischen Sync per launchd einrichten")
    sc.add_argument("--hours", type=float, default=2, help="Intervall in Stunden (Standard: 2)")
    sc.add_argument("--remove", action="store_true", help="automatischen Sync entfernen")
    sc.set_defaults(func=cmd_schedule)
    args = p.parse_args()
    VERBOSE, DEBUG = args.verbose, args.debug
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        sys.exit(130)


if __name__ == "__main__":
    main()
