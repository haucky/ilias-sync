#!/bin/bash
# ilias-sync für macOS und Linux installieren:
#   curl -fsSL https://raw.githubusercontent.com/haucky/ilias-sync/main/install.sh | bash
#
# Lädt das fertige Programm von der neuesten GitHub-Release nach ~/.local/bin/ilias-sync.
# Kein Python nötig. Mit  ILIAS_SYNC_FROM_SOURCE=1 bash install.sh  wird stattdessen aus dem
# Quellcode (Python 3 + venv) installiert.
set -euo pipefail

REPO="haucky/ilias-sync"
BIN="$HOME/.local/bin"
SHARE="$HOME/.local/share/ilias-sync"

install_from_source() {
  local here; here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
  [ -f "$here/ilias_sync.py" ] || { echo "ilias_sync.py nicht gefunden – im Repo-Ordner ausführen."; exit 1; }
  command -v python3 >/dev/null || { echo "python3 fehlt (macOS: 'xcode-select --install')."; exit 1; }
  mkdir -p "$SHARE" "$BIN"
  cp "$here/ilias_sync.py" "$SHARE/ilias_sync.py"
  python3 -m venv "$SHARE/venv"
  "$SHARE/venv/bin/pip" install -q --upgrade pip -r "$here/requirements.txt"
  rm -f "$BIN/ilias-sync"
  cat > "$BIN/ilias-sync" <<EOS
#!/bin/bash
exec "$SHARE/venv/bin/python" "$SHARE/ilias_sync.py" "\$@"
EOS
  chmod +x "$BIN/ilias-sync"
}

install_binary() {
  local os arch asset
  case "$(uname -s)" in
    Darwin) os=macos ;;
    Linux)  os=linux ;;
    *) echo "Nicht unterstütztes System: $(uname -s). Unter Windows bitte install.ps1 verwenden."; exit 1 ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) arch=arm64 ;;
    x86_64|amd64)  arch=x64 ;;
    *) echo "Nicht unterstützte Architektur: $(uname -m)"; exit 1 ;;
  esac
  asset="ilias-sync-$os-$arch"
  if [ "$os" = linux ] && [ "$arch" = arm64 ]; then
    echo "Für Linux/ARM gibt es kein fertiges Programm – bitte aus dem Quellcode installieren:"
    echo "  git clone https://github.com/$REPO && cd ilias-sync && ILIAS_SYNC_FROM_SOURCE=1 bash install.sh"
    exit 1
  fi
  echo "Lade $asset …"
  mkdir -p "$BIN"
  local tmp; tmp="$(mktemp "$BIN/.ilias-sync.XXXXXX")"
  if ! curl -fSL --progress-bar -o "$tmp" "https://github.com/$REPO/releases/latest/download/$asset"; then
    rm -f "$tmp"; echo "Download fehlgeschlagen."; exit 1
  fi
  chmod +x "$tmp"
  # curl setzt kein Quarantäne-Flag, aber sicher ist sicher (Gatekeeper)
  [ "$os" = macos ] && xattr -d com.apple.quarantine "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$BIN/ilias-sync"
}

if [ "${ILIAS_SYNC_FROM_SOURCE:-0}" = 1 ]; then install_from_source; else install_binary; fi

echo "✓ Installiert: $BIN/ilias-sync ($("$BIN/ilias-sync" --version))"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) rc="$HOME/.bashrc"; [ "$(basename "${SHELL:-}")" = zsh ] && rc="$HOME/.zshrc"
     if ! grep -qs '.local/bin' "$rc"; then
       echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
       echo "  ~/.local/bin wurde zum PATH in $rc hinzugefügt – neues Terminal-Fenster öffnen."
     fi ;;
esac
echo "Weiter mit:  ilias-sync setup"
