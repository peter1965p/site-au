#!/usr/bin/env bash
# Legt Menue- und Taskleisteneintrag fuer die GUI an (nur fuer den
# aktuellen Benutzer, keine Root-Rechte noetig).
set -euo pipefail

PROJEKT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$PROJEKT/venv/bin/python"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

if [[ ! -x "$PYTHON" ]]; then
    echo "Kein venv unter $PYTHON - erst 'python3 -m venv venv' und die Pakete installieren." >&2
    exit 1
fi

mkdir -p "$APPS" "$ICONS"
install -m 644 "$PROJEKT/assets/site-audit.svg" "$ICONS/site-audit.svg"

sed -e "s|@EXEC@|$PYTHON $PROJEKT/gui.py|" \
    -e "s|@PROJEKT@|$PROJEKT|" \
    "$PROJEKT/desktop/site-audit.desktop.in" > "$APPS/site-audit.desktop"
chmod 644 "$APPS/site-audit.desktop"

# Caches auffrischen, damit der Eintrag sofort auftaucht
command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPS" || true
command -v gtk-update-icon-cache >/dev/null && \
    gtk-update-icon-cache -qtf "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true

echo "Installiert:"
echo "  $APPS/site-audit.desktop"
echo "  $ICONS/site-audit.svg"
echo
echo "Zu finden im Anwendungsmenue unter \"site-audit\"."
echo "Deinstallieren: rm \"$APPS/site-audit.desktop\" \"$ICONS/site-audit.svg\""
