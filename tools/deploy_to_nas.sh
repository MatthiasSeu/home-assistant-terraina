#!/usr/bin/env bash
# deploy_to_nas.sh
# Lokales Script: packt terraina_community_community und überträgt es auf das NAS.
# Danach: auf NAS einloggen, sudo -i, dann nas_install.sh ausführen.
#
# Verwendung:
#   bash tools/deploy_to_nas.sh
#
# Variablen unten anpassen!

set -euo pipefail

# ── KONFIGURATION ─────────────────────────────────────────────────
NAS_USER="nasadmin"       # z.B. matthias
NAS_IP="nas.uwn.at"            # z.B. 192.168.1.10
NAS_UPLOAD_DIR="/tmp"           # Ziel auf dem NAS (muss für LOGIN_USER schreibbar sein)
# ──────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPONENT_SRC="$(dirname "$SCRIPT_DIR")/custom_components/terraina_community"
TARBALL="/tmp/terraina_community.tar.gz"
NAS_SCRIPT_SRC="$SCRIPT_DIR/nas_install.sh"

echo "============================================================"
echo "  TERRAINA — Deploy to NAS"
echo "============================================================"
echo "  Quelle : $COMPONENT_SRC"
echo "  NAS    : ${NAS_USER}@${NAS_IP}:${NAS_UPLOAD_DIR}"
echo ""

# ── Schritt 1: Versions-Datei schreiben ───────────────────────────
DEPLOY_VERSION="$(date +%Y%m%d%H%M)"
echo "Version: $DEPLOY_VERSION" > "$COMPONENT_SRC/deployed_version.txt"
echo "[1/3] Deploy-Version: $DEPLOY_VERSION"

# ── Schritt 2: Tarball erstellen ──────────────────────────────────
echo "[2/3] Erstelle Tarball ..."
tar czf "$TARBALL" -C "$(dirname "$COMPONENT_SRC")" terraina_community/
echo "      $TARBALL  ($(du -sh "$TARBALL" | cut -f1))"

# ── Schritt 3: Upload auf NAS ─────────────────────────────────────
echo ""
echo "[3/3] Übertrage auf NAS ..."
scp "$TARBALL"        "${NAS_USER}@${NAS_IP}:${NAS_UPLOAD_DIR}/terraina_community.tar.gz"
scp "$NAS_SCRIPT_SRC" "${NAS_USER}@${NAS_IP}:${NAS_UPLOAD_DIR}/nas_install.sh"
echo "      Übertragung abgeschlossen."

# ── Anleitung ausgeben ────────────────────────────────────────────
echo ""
echo "Jetzt auf dem NAS ausführen:"
echo "──────────────────────────────────────────────────────────"
echo "  ssh ${NAS_USER}@${NAS_IP}"
echo "  sudo -i"
echo "  bash ${NAS_UPLOAD_DIR}/nas_install.sh"
echo "──────────────────────────────────────────────────────────"
echo ""
echo "Fertig. Warte auf NAS-Bestätigung."
