# ilias-sync

Lädt automatisch alle Dateien aus deinen Kursen auf **myILIAS (KH Freiburg)** in einen Ordner auf
deinem Rechner – und danach nur noch, was neu ist oder sich geändert hat.

Läuft unter **Windows**, **macOS** und **Linux**. Python wird nicht benötigt.

## Installation

### Windows

PowerShell öffnen (Startmenü → „PowerShell“) und einfügen:

```powershell
irm https://raw.githubusercontent.com/haucky/ilias-sync/main/install.ps1 | iex
```

Danach gibt es im Startmenü **ILIAS-Sync**. Beim ersten Start wird die Anmeldung eingerichtet,
danach synchronisiert ein Doppelklick einfach.

### macOS / Linux

Terminal öffnen und einfügen:

```bash
curl -fsSL https://raw.githubusercontent.com/haucky/ilias-sync/main/install.sh | bash
```

### Manuell herunterladen

Alternativ das passende Programm unter
[Releases](https://github.com/haucky/ilias-sync/releases/latest) herunterladen:

| System                         | Datei                         |
|--------------------------------|-------------------------------|
| Windows                        | `ilias-sync-windows-x64.exe`  |
| macOS mit Apple-Chip (M1–M4 …) | `ilias-sync-macos-arm64`      |
| macOS mit Intel-Chip           | `ilias-sync-macos-x64`        |
| Linux                          | `ilias-sync-linux-x64`        |

Die Programme sind (noch) nicht signiert, deshalb warnt das System beim manuellen Download:

- **Windows:** „Der Computer wurde durch Windows geschützt“ → *Weitere Informationen* → *Trotzdem ausführen*.
- **macOS:** Im Terminal einmalig
  `xattr -d com.apple.quarantine ~/Downloads/ilias-sync-macos-* && chmod +x ~/Downloads/ilias-sync-macos-*`
  ausführen.

Mit den Installationsbefehlen oben entfallen diese Warnungen.

## Benutzung

```
ilias-sync setup               # Anmeldename, Anmeldungsart („Studenten AD“), Passwort, Zielordner (Standard: ~/ILIAS)
ilias-sync sync                # synchronisieren
ilias-sync schedule --hours 2  # ab jetzt alle 2 h automatisch im Hintergrund, mit Mitteilung bei neuen Dateien
ilias-sync schedule --remove   # Automatik wieder abschalten
ilias-sync courses             # gefundene Kurse anzeigen
ilias-sync sync --dry-run      # nur anzeigen, was heruntergeladen würde
```

`ilias-sync` ohne Befehl richtet beim ersten Mal ein und synchronisiert danach.

- Das Passwort liegt sicher im **macOS-Schlüsselbund** bzw. in der
  **Windows-Anmeldeinformationsverwaltung** (Linux: Secret Service / GNOME-Schlüsselbund).
  Alternativ kann es über die Umgebungsvariable `ILIAS_PASSWORD` übergeben werden.
- Die automatische Synchronisierung nutzt launchd (macOS), die Aufgabenplanung (Windows)
  bzw. einen systemd-Timer (Linux).
- Lokal bearbeitete Dateien werden nie stillschweigend überschrieben, sondern als
  „… (lokal DATUM)“ gesichert.
- Kurse lassen sich in der Konfigurationsdatei über `include`/`exclude` (reguläre Ausdrücke) filtern:
  - macOS/Linux: `~/.config/ilias-sync/config.json`
  - Windows: `%APPDATA%\ilias-sync\config.json`

## Für Entwickler

Aus dem Quellcode starten:

```bash
pip install -r requirements.txt
python ilias_sync.py --help
```

oder als Befehl installieren: `pipx install git+https://github.com/haucky/ilias-sync`.

### Neue Version veröffentlichen

1. `__version__` in `ilias_sync.py` erhöhen, z. B. auf `1.2.0`, und committen.
2. Tag setzen und pushen:
   ```bash
   git tag v1.2.0 && git push origin v1.2.0
   ```
3. GitHub Actions ([`.github/workflows/release.yml`](.github/workflows/release.yml)) baut die
   Programme für alle Systeme und legt automatisch eine Release mit den Downloads an.

Bei jedem Push und Pull Request werden die Programme ebenfalls gebaut und getestet; sie liegen
dann als Artefakte am jeweiligen Workflow-Lauf.
