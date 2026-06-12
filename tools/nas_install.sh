#!/usr/bin/env bash
# nas_install.sh — Auf dem NAS als ROOT ausführen (sudo -i)

set -euo pipefail

HA_CONTAINER="home-assistant"
TARBALL="/tmp/terraina_community.tar.gz"
EXTRACT_DIR="/tmp"
COMPONENT="terraina_community"

echo "============================================================"
echo "  TERRAINA Community — NAS Install (als root)"
echo "============================================================"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "FEHLER: Dieses Script muss als root laufen (sudo -i)."
  exit 1
fi

if ! docker inspect "$HA_CONTAINER" > /dev/null 2>&1; then
  echo "FEHLER: Container '$HA_CONTAINER' nicht gefunden."
  exit 1
fi

# ── Schritt 1: Entpacken ──────────────────────────────────────────
echo ""
echo "[1/5] Entpacke $TARBALL ..."
tar xzf "$TARBALL" -C "$EXTRACT_DIR"
echo "      $EXTRACT_DIR/$COMPONENT — OK"

# ── Schritt 2: In Container kopieren ─────────────────────────────
echo ""
echo "[2/5] Kopiere in Container ..."
docker exec "$HA_CONTAINER" rm -rf /config/custom_components/$COMPONENT 2>/dev/null || true
docker cp "$EXTRACT_DIR/$COMPONENT" "$HA_CONTAINER":/config/custom_components/
echo "      Dateien im Container:"
docker exec "$HA_CONTAINER" ls /config/custom_components/$COMPONENT/ | sed 's/^/        /'

# ── Schritt 3: Protobuf & Import-Test ────────────────────────────
echo ""
echo "[3/5] Vorab-Check: protobuf-Version und pb2-Import ..."

PROTO_VER=$(docker exec "$HA_CONTAINER" python3 -c \
  "import google.protobuf; print(google.protobuf.__version__)" 2>&1)
echo "      protobuf: $PROTO_VER"

IMPORT_RESULT=$(docker exec "$HA_CONTAINER" python3 -c "
import sys
sys.path.insert(0, '/config')
try:
    from custom_components.terraina_community import terraina_pb2, grpc_stream, platform_token
    print('OK')
except Exception as e:
    print('FEHLER: ' + str(e))
" 2>&1)
echo "      Import-Test: $IMPORT_RESULT"

# ── Schritt 4: HA neu starten ─────────────────────────────────────
echo ""
echo "[4/5] Starte Home Assistant neu ..."
docker restart "$HA_CONTAINER"
echo "      Warte 45s auf Startabschluss ..."
sleep 45

# ── Schritt 5: Logs prüfen ────────────────────────────────────────
echo ""
echo "[5/5] Relevante Log-Zeilen:"
echo "──────────────────────────────────────────────────────────"
docker logs "$HA_CONTAINER" --tail 200 2>&1 \
  | grep -iE "terraina|reauth|app_token|grpc|protobuf|importerror" \
  | head -50 \
  || echo "  (keine Treffer)"

echo ""
echo "──────────────────────────────────────────────────────────"
echo "  Alle Fehler: docker logs $HA_CONTAINER --tail 200 2>&1 | grep -i error"
echo "============================================================"
