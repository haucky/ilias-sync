#!/bin/bash
# Installiert ilias-sync nach ~/.local/share/ilias-sync und legt den Befehl ~/.local/bin/ilias-sync an.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)/ilias_sync.py"
DIR="$HOME/.local/share/ilias-sync"
BIN="$HOME/.local/bin"

command -v python3 >/dev/null || { echo "python3 fehlt – bitte 'xcode-select --install' ausführen."; exit 1; }

mkdir -p "$DIR" "$BIN"
cp "$SRC" "$DIR/ilias_sync.py"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip requests beautifulsoup4

cat > "$BIN/ilias-sync" <<EOF
#!/bin/bash
exec "$DIR/venv/bin/python" "$DIR/ilias_sync.py" "\$@"
EOF
chmod +x "$BIN/ilias-sync"

echo "✓ Installiert: $BIN/ilias-sync"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "  ~/.local/bin ist nicht im PATH. Einmalig ausführen:"
     echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc" ;;
esac
echo "Weiter mit:  ilias-sync setup"
