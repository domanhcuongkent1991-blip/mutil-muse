#!/usr/bin/env bash
# Cai dat he da-bot V2 tren PC (Linux/WSL). Chi dung thu vien chuan Python.
#
# Cach dung:
#   bash install.sh <bot_id> [--dir DIR] [--unit-dir DIR] [--dry-run]
#
#   bot_id       theo mau ^[a-z0-9][a-z0-9-]{0,31}$ (vi du: bot-a)
#   --dir        thu muc goc multibot (mac dinh: $MULTIBOT_BASE_DIR hoac
#                $MULTIBOT_DIR hoac ~/multibot)
#   --unit-dir   noi ghi unit systemd user (mac dinh: ~/.config/systemd/user)
#   --dry-run    chi tao file + render unit, KHONG dong vao systemd that
#                (dung cho test; unit render ghi vao <dir>/systemd/rendered/)
#
# Script chay lap lai duoc (idempotent): khong bao gio ghi de secret/token
# da co, khong tao lai queue secret da co, khong sua muc registry da co.
# Script KHONG chua secret/token nao. Queue secret moi sinh chi IN RA
# TERMINAL 1 LAN de chu nhan chuyen cho Muse qua kenh K3 — khong ghi vao
# bat ky file log nao.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_ID=""
TARGET_DIR="${MULTIBOT_BASE_DIR:-${MULTIBOT_DIR:-$HOME/multibot}}"
UNIT_DIR="${MULTIBOT_UNIT_DIR:-$HOME/.config/systemd/user}"
DRY_RUN=0
if [ "${MULTIBOT_DRY_RUN:-0}" = "1" ]; then
  DRY_RUN=1
fi

usage() {
  echo "Dung: bash install.sh <bot_id> [--dir DIR] [--unit-dir DIR] [--dry-run]" >&2
  echo "  bot_id theo mau: ^[a-z0-9][a-z0-9-]{0,31}$ (chu thuong, so, gach ngang)" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) TARGET_DIR="${2:?thieu gia tri cho --dir}"; shift 2 ;;
    --unit-dir) UNIT_DIR="${2:?thieu gia tri cho --unit-dir}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage; exit 0 ;;
    --*) echo "Tham so la: $1" >&2; usage; exit 2 ;;
    *)
      if [ -n "$BOT_ID" ]; then
        echo "Chi nhan 1 bot_id moi lan chay." >&2; usage; exit 2
      fi
      BOT_ID="$1"; shift ;;
  esac
done

if [ -z "$BOT_ID" ]; then
  echo "Thieu bot_id." >&2; usage; exit 2
fi
if ! [[ "$BOT_ID" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]]; then
  echo "bot_id khong dung mau ^[a-z0-9][a-z0-9-]{0,31}$: $BOT_ID" >&2
  exit 2
fi
if [ "$DRY_RUN" = "1" ]; then
  echo "== DRY-RUN: khong dong vao systemd that =="
fi

echo "== 1. Kiem tra Python 3.10+ =="
if ! command -v python3 >/dev/null 2>&1; then
  echo "LOI: khong thay python3. Cai Python 3.10 tro len roi chay lai." >&2
  exit 1
fi
python3 --version
if ! python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"; then
  echo "LOI: can Python >= 3.10." >&2
  exit 1
fi

echo "== 2. Kiem tra Tailscale (chi canh bao, khong tu sua) =="
if command -v tailscale >/dev/null 2>&1; then
  if tailscale status >/dev/null 2>&1; then
    echo "Tailscale dang chay."
    tailscale ip -4 2>/dev/null | head -1 | sed 's/^/Tailscale IP: /' || true
  else
    echo "CANH BAO: Tailscale da cai nhung 'tailscale status' khong chay duoc"
    echo "  (chua dang nhap hoac chua len mang). Chu nhan chay: sudo tailscale up"
    echo "  Gateway van cai dat duoc; phia Muse chi noi duoc khi Tailscale len."
  fi
else
  echo "CANH BAO: chua thay lenh tailscale trong Linux nay."
  echo "  Cai Tailscale vao Linux theo huong dan chinh thuc, cung tai khoan"
  echo "  voi phia Windows, roi chay: sudo tailscale up"
fi

echo "== 3. Kiem tra systemd user + loginctl =="
SYSTEMD_OK=0
if systemctl --user --version >/dev/null 2>&1 && command -v loginctl >/dev/null 2>&1; then
  SYSTEMD_OK=1
  echo "systemd user + loginctl: co."
else
  echo "CANH BAO: khong co systemd user hoac thieu loginctl trong Linux nay."
  echo "  Neu la WSL chua bat systemd: them [boot] systemd=true vao /etc/wsl.conf"
  echo "  roi 'wsl --shutdown' va mo lai. Van cai dat tiep; unit se chi duoc"
  echo "  render ra file, chu nhan bat systemd roi cai lai."
fi

echo "== 4. Tao cay thu muc (DESIGN §5) tai: $TARGET_DIR =="
mkdir -p "$TARGET_DIR/code" "$TARGET_DIR/config" "$TARGET_DIR/secrets" \
  "$TARGET_DIR/data" "$TARGET_DIR/locks" "$TARGET_DIR/backups" \
  "$TARGET_DIR/systemd" "$TARGET_DIR/ops" "$TARGET_DIR/ui-config"
chmod 700 "$TARGET_DIR/secrets" "$TARGET_DIR/locks" "$TARGET_DIR/ui-config" \
  "$TARGET_DIR/data"
mkdir -p "$TARGET_DIR/data/$BOT_ID/files"
chmod 700 "$TARGET_DIR/data/$BOT_ID"

echo "== 5. Copy code + template unit vao cay thu muc (neu chay tu goi) =="
# QUYET DONG GOI (vong 3): giu nguyen cay goi — shared/ + pc/ la package.
# ear_main.py import pc.config va shared.*, nen install.sh copy TOAN BO
# cay code (shared/ + pc/) vao code/; unit systemd dat PYTHONPATH tro
# ve code/. Chi copy ear/ + gateway/ nhu truoc thi import khong resolve
# duoc tren PC. Loai __pycache__ khoi ban copy.
SOURCE_ROOT="$(dirname "$SCRIPT_DIR")"
if [ "$SCRIPT_DIR" = "$TARGET_DIR/code/pc" ]; then
  echo "Script dang chay tu ban da cai — bo qua copy code."
elif [ -d "$SOURCE_ROOT/shared" ] && [ -d "$SOURCE_ROOT/pc" ] \
   && [ -f "$SOURCE_ROOT/pc/gateway/gateway_main.py" ]; then
  mkdir -p "$TARGET_DIR/code"
  cp -a "$SOURCE_ROOT/shared" "$TARGET_DIR/code/"
  cp -a "$SOURCE_ROOT/pc" "$TARGET_DIR/code/"
  find "$TARGET_DIR/code" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
  find "$TARGET_DIR/code" -name "*.pyc" -delete 2>/dev/null || true
  echo "Da copy toan bo cay code (shared/ + pc/) vao $TARGET_DIR/code/."
else
  echo "CANH BAO: khong thay cay code day du (shared/ + pc/) ke ben script."
  echo "  Copy thu cong toan bo cay code vao $TARGET_DIR/code/ truoc khi"
  echo "  khoi dong unit (can ca shared/ lan pc/ — chi copy ear/ + gateway/"
  echo "  thi tai khong chay duoc vi thieu import)."
fi
if [ -d "$SCRIPT_DIR/systemd" ]; then
  cp -a "$SCRIPT_DIR/systemd/"*.service "$TARGET_DIR/systemd/" 2>/dev/null || true
  cp -a "$SCRIPT_DIR/systemd/"*.timer "$TARGET_DIR/systemd/" 2>/dev/null || true
  echo "Da luu ban recovery cua template unit vao $TARGET_DIR/systemd/."
fi
GATEWAY_FILE="$TARGET_DIR/code/pc/gateway/gateway_main.py"
if [ -f "$GATEWAY_FILE" ]; then
  python3 -m py_compile "$GATEWAY_FILE"
  echo "Cu phap gateway OK."
else
  echo "CANH BAO: chua thay $GATEWAY_FILE."
  echo "  Copy toan bo cay code (shared/ + pc/) vao $TARGET_DIR/code/ truoc"
  echo "  khi khoi dong unit gateway."
fi
EAR_FILE="$TARGET_DIR/code/pc/ear/ear_main.py"
if [ -f "$EAR_FILE" ]; then
  python3 -m py_compile "$EAR_FILE"
  echo "Cu phap tai OK."
fi
CONFIG_FILE="$TARGET_DIR/code/pc/config.py"
if [ -f "$CONFIG_FILE" ] && [ -d "$TARGET_DIR/code/shared" ]; then
  python3 -m py_compile "$CONFIG_FILE"
  echo "Cu phap config + cay shared/ OK."
else
  echo "CANH BAO: thieu $CONFIG_FILE hoac $TARGET_DIR/code/shared/."
  echo "  Tai import pc.config va shared.* — thieu la khong chay duoc."
fi

echo "== 6. File secret (600) cho bot: $BOT_ID =="
TOKEN_FILE="$TARGET_DIR/secrets/$BOT_ID.token"
SECRET_FILE="$TARGET_DIR/secrets/$BOT_ID.queue_secret"
SYNC_SECRET_FILE="$TARGET_DIR/secrets/$BOT_ID.sync_secret"
# Token: tao file RONG neu chua co, KHONG BAO GIO ghi de. Chu nhan dien tay.
if [ ! -f "$TOKEN_FILE" ]; then
  : > "$TOKEN_FILE"
  echo "Da tao file token RONG: $TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"
# Queue secret: chi sinh khi file chua co hoac rong; da co thi giu nguyen
# va KHONG in ra.
SECRET_JUST_CREATED=0
if [ ! -f "$SECRET_FILE" ] || [ ! -s "$SECRET_FILE" ] \
   || ! grep -q '[^[:space:]]' "$SECRET_FILE" 2>/dev/null; then
  NEW_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  printf '%s\n' "$NEW_SECRET" > "$SECRET_FILE"
  chmod 600 "$SECRET_FILE"
  SECRET_JUST_CREATED=1
  echo ""
  echo "================================================================"
  echo "QUEUE SECRET MOI cho bot '$BOT_ID' (chi hien 1 LAN duy nhat):"
  echo "$NEW_SECRET"
  echo "================================================================"
  unset NEW_SECRET
  echo "Chu nhan: copy dong secret tren va gui RIENG cho Muse qua kenh K3"
  echo "(chat rieng voi Muse — KHONG dan vao chat nhom, khong gui qua kenh"
  echo "khac). Secret da luu vao file 600 tren may nay; neu lam mat dong"
  echo "tren, doc lai bang: cat $SECRET_FILE"
  echo ""
else
  chmod 600 "$SECRET_FILE"
  echo "File queue secret da co — giu nguyen, khong tao moi, khong in ra."
  echo "Can ban giao lai thi chu nhan tu doc: cat $SECRET_FILE"
fi
# Sync secret (GĐ3b): rieng biet queue secret, chi sinh khi chua co
# hoac rong; da co thi giu nguyen va KHONG in ra. In 1 lan duy nhat
# cung kenh K3 nhu queue secret.
SYNC_JUST_CREATED=0
if [ ! -f "$SYNC_SECRET_FILE" ] || [ ! -s "$SYNC_SECRET_FILE" ] \
   || ! grep -q '[^[:space:]]' "$SYNC_SECRET_FILE" 2>/dev/null; then
  NEW_SYNC_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  printf '%s\n' "$NEW_SYNC_SECRET" > "$SYNC_SECRET_FILE"
  chmod 600 "$SYNC_SECRET_FILE"
  SYNC_JUST_CREATED=1
  echo ""
  echo "================================================================"
  echo "SYNC SECRET MOI cho bot '$BOT_ID' (chi hien 1 LAN duy nhat):"
  echo "$NEW_SYNC_SECRET"
  echo "================================================================"
  unset NEW_SYNC_SECRET
  echo "Chu nhan: copy dong secret tren va gui RIENG cho Muse qua kenh K3"
  echo "(cung dot ban giao voi queue secret). Secret da luu vao file 600"
  echo "tren may nay; neu lam mat dong tren, doc lai bang: cat $SYNC_SECRET_FILE"
  echo ""
else
  chmod 600 "$SYNC_SECRET_FILE"
  echo "File sync secret da co — giu nguyen, khong tao moi, khong in ra."
fi
if [ ! -s "$TOKEN_FILE" ]; then
  echo "CANH BAO: file token dang RONG. Chu nhan lay token tu console cua"
  echo "  nen tang (Zalo Bot console / BotFather) va ghi vao file tren bang"
  echo "  trinh soan thao. KHONG dan token vao chat nhom."
fi

echo "== 7. Dang ky bot vao registry.json (trang thai TAT) =="
python3 - "$TARGET_DIR" "$BOT_ID" <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone
target, bot_id = sys.argv[1], sys.argv[2]
path = os.path.join(target, "registry.json")
data = {"version": 1, "bots": {}}
try:
    with open(path, encoding="utf-8") as f:
        loaded = json.load(f)
    if isinstance(loaded, dict) and isinstance(loaded.get("bots"), dict):
        data = loaded
except (OSError, ValueError):
    pass
if bot_id not in data["bots"]:
    data["bots"][bot_id] = {
        "bot_id": bot_id,
        "enabled": False,
        "paused": False,
        "paused_since": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    print("Da them bot '%s' vao registry (enabled=false)." % bot_id)
else:
    print("Bot '%s' da co trong registry — giu nguyen." % bot_id)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp, path)
PYEOF

if [ ! -f "$TARGET_DIR/ops/OPS-FILE.md" ]; then
  cat > "$TARGET_DIR/ops/OPS-FILE.md" <<'MDEOF'
# File van hanh (ops)

Ghi chep van hanh cua he da-bot tren PC nay. KHONG ghi secret/token vao day.

- Che do mang WSL: (dien sau khi kiem tra)
- Phuong an Tailscale: A (Tailscale trong Linux) / B (chuyen tiep cong)
- Linger: (da bat / chua — ngay gio kiem tra)
- Ngay hen TOTP cho UI: (sau GĐ4)
MDEOF
  echo "Da tao $TARGET_DIR/ops/OPS-FILE.md (mau, khong secret)."
fi

# Mau dia chi API cho UI kiem tra token (GĐ3): file RONG, khong chua
# host that. Chu nhan/agent PC dien dia chi API cua tung nen tang vao
# day; thieu file nay thi UI bao "chua cau hinh" va khong kiem tra
# duoc token (khong doan host).
if [ ! -f "$TARGET_DIR/ui-config/platform_endpoints.json" ]; then
  cat > "$TARGET_DIR/ui-config/platform_endpoints.json" <<'JSONEOF'
{
  "zalo": "",
  "telegram": ""
}
JSONEOF
  echo "Da tao $TARGET_DIR/ui-config/platform_endpoints.json (mau rong)."
fi

echo "== 8. Render unit systemd (thay __DIR__ bang thu muc cai that) =="
# Template lay tu ban recovery trong cay thu muc; neu chua co thi lay ke
# ben script (truong hop goi chua copy template vao cay).
TEMPLATE_DIR="$TARGET_DIR/systemd"
if [ ! -f "$TEMPLATE_DIR/tai@.service" ] \
   && [ -f "$SCRIPT_DIR/systemd/tai@.service" ]; then
  TEMPLATE_DIR="$SCRIPT_DIR/systemd"
fi
if [ ! -f "$TEMPLATE_DIR/tai@.service" ] \
   || [ ! -f "$TEMPLATE_DIR/multibot-gateway.service" ] \
   || [ ! -f "$TEMPLATE_DIR/multibot-sync-receiver.service" ] \
   || [ ! -f "$TEMPLATE_DIR/bot-ui.service" ] \
   || [ ! -f "$TEMPLATE_DIR/center-prune.timer" ] \
   || [ ! -f "$TEMPLATE_DIR/center-prune.service" ]; then
  echo "LOI: khong thay template unit trong $TEMPLATE_DIR." >&2
  exit 1
fi
render_unit() {
  local template="$1" dest="$2"
  python3 - "$template" "$dest" "$TARGET_DIR" <<'PYEOF'
import sys
template, dest, target = sys.argv[1], sys.argv[2], sys.argv[3]
with open(template, encoding="utf-8") as f:
    text = f.read()
if "__DIR__" not in text:
    raise SystemExit("template thieu placeholder __DIR__: %s" % template)
text = text.replace("__DIR__", target)
with open(dest, "w", encoding="utf-8") as f:
    f.write(text)
PYEOF
  if grep -q '__DIR__' "$dest"; then
    echo "LOI: unit render xong van con __DIR__: $dest" >&2
    exit 1
  fi
}
if [ "$DRY_RUN" = "1" ]; then
  RENDER_DIR="$TARGET_DIR/systemd/rendered"
  mkdir -p "$RENDER_DIR"
  render_unit "$TEMPLATE_DIR/tai@.service" "$RENDER_DIR/tai@.service"
  render_unit "$TEMPLATE_DIR/multibot-gateway.service" \
    "$RENDER_DIR/multibot-gateway.service"
  render_unit "$TEMPLATE_DIR/multibot-sync-receiver.service" \
    "$RENDER_DIR/multibot-sync-receiver.service"
  render_unit "$TEMPLATE_DIR/bot-ui.service" \
    "$RENDER_DIR/bot-ui.service"
  # Timer don dep (T3f): khong co placeholder __DIR__ nen copy
  # nguyen ban (khong qua render_unit — ham do bat buoc co __DIR__).
  cp "$TEMPLATE_DIR/center-prune.timer" "$RENDER_DIR/center-prune.timer"
  render_unit "$TEMPLATE_DIR/center-prune.service" \
    "$RENDER_DIR/center-prune.service"
  echo "Dry-run: unit render tai $RENDER_DIR (khong cai vao systemd)."
else
  mkdir -p "$UNIT_DIR"
  render_unit "$TEMPLATE_DIR/tai@.service" "$UNIT_DIR/tai@.service"
  render_unit "$TEMPLATE_DIR/multibot-gateway.service" \
    "$UNIT_DIR/multibot-gateway.service"
  render_unit "$TEMPLATE_DIR/multibot-sync-receiver.service" \
    "$UNIT_DIR/multibot-sync-receiver.service"
  render_unit "$TEMPLATE_DIR/bot-ui.service" \
    "$UNIT_DIR/bot-ui.service"
  cp "$TEMPLATE_DIR/center-prune.timer" "$UNIT_DIR/center-prune.timer"
  render_unit "$TEMPLATE_DIR/center-prune.service" \
    "$UNIT_DIR/center-prune.service"
  echo "Da ghi unit vao $UNIT_DIR."
  if [ "$SYSTEMD_OK" = "1" ]; then
    systemctl --user daemon-reload
    # Chi enable gateway + receiver. Tai cua tung bot chi enable/start
    # SAU KHI co allowlist (D5). UI chi enable SAU KHI dat mat khau
    # (unit tu choi chay khi chua co hash) — chu nhan bat tay theo
    # README-TRIEN-KHAI.md.
    systemctl --user enable multibot-gateway.service
    systemctl --user enable multibot-sync-receiver.service
    # Timer don dep kho trung tam (T3f): enable ngay duoc — lenh boc
    # tu quyet dinh co don hay khong (ngay khong phai ngay don thi
    # thoat 0, khong lam gi).
    systemctl --user enable --now center-prune.timer
    echo "Da enable multibot-gateway.service + multibot-sync-receiver.service (chua start)."
    echo "Da enable + start center-prune.timer (don dep 21:00 moi ngay)."
  else
    echo "Bo qua daemon-reload/enable: systemd user khong san sang."
  fi
fi

echo "== 9. Linger (unit chay ca khi khong co phien dang nhap) =="
if [ "$DRY_RUN" = "1" ]; then
  echo "Dry-run: bo qua loginctl enable-linger."
elif [ "$SYSTEMD_OK" = "1" ]; then
  if loginctl enable-linger "$USER" >/dev/null 2>&1; then
    echo "Da bat linger."
  else
    echo "CANH BAO: khong bat duoc linger (thuong can sudo lan dau)."
    echo "  Chay tay: sudo loginctl enable-linger $USER"
    echo "  Neu khong bat linger, unit user co the dung khi dang xuat."
  fi
else
  echo "Bo qua linger: loginctl khong san sang."
fi

echo ""
echo "== Xong cai dat cho bot '$BOT_ID' =="
echo "Thu muc goc: $TARGET_DIR"
echo "Viec tiep theo (xem README-TRIEN-KHAI.md):"
echo "  1. Dien token vao $TOKEN_FILE (chu nhan lay tu console)."
if [ "$SECRET_JUST_CREATED" = "1" ]; then
  echo "  2. Chuyen queue secret vua in o tren cho Muse qua kenh K3 (chat rieng)."
else
  echo "  2. Queue secret da co tu truoc — khong can ban giao lai."
fi
if [ "$SYNC_JUST_CREATED" = "1" ]; then
  echo "  2b. Chuyen sync secret vua in o tren cho Muse qua kenh K3 (cung dot voi queue secret) — de dong bo tra loi ve UI."
else
  echo "  2b. Sync secret da co tu truoc — khong can ban giao lai."
fi
echo "  3. Tao allowlist cho bot truoc khi bat (chua co allowlist thi CAM bat)."
echo "  4. Khoi dong gateway: systemctl --user start multibot-gateway"
echo "  5. Sau khi co allowlist: systemctl --user enable --now tai@$BOT_ID"
echo "  6. Khoi dong receiver dong bo tra loi: systemctl --user start multibot-sync-receiver"
echo "  7. Neu muon UI kiem tra token khi them/thay bot: dien dia chi API vao"
echo "     $TARGET_DIR/ui-config/platform_endpoints.json (xem README-TRIEN-KHAI.md)."
echo "  8. Dat mat khau UI lan dau, roi bat UI:"
echo "     python3 $TARGET_DIR/code/pc/ui/set_ui_password.py --root $TARGET_DIR"
echo "     systemctl --user enable --now bot-ui"
