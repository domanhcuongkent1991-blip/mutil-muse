#!/usr/bin/env bash
# restore_ui_config.sh — Khôi phục toàn bộ ui-config/ từ backup
# (C-25 / NS-J1, audit vòng sửa lớn v0.6.0).
#
# MỘT thao tác nguyên tử thay cho làm tay từng bước: copy backup vào
# thư mục đích + ghi marker center_view_restored.json rồi mới báo xong.
# Quên marker thì hệ thống fail-open: UI coi mã cũ là "chưa từng xem"
# và cho xem lại mã đã từng xem (phá Q-V060-17 "mỗi mã chỉ hiện tối
# đa 1 lần") — vì vậy script KHÔNG cho xong khi chưa có marker hợp lệ.
#
# Cách dùng (agent PC chạy trên PC, sau khi đã DỪNG UI):
#   bash pc/ops/restore_ui_config.sh <thư mục backup> <thư mục ui-config>
#
# Ví dụ:
#   bash pc/ops/restore_ui_config.sh /srv/backup/ui-config-2026-11-20 ./ui-config
#
# Quy tắc:
# - KHÔNG chạm thư mục secrets/ (token/mã bí mật không bao giờ khôi
#   phục từ backup — mất thì lấy lại/tạo mới theo doc 06 §2). Script
#   từ chối chạy khi backup hoặc đích trỏ vào secrets/.
# - KHÔNG tự khởi động lại UI. Agent PC khởi động lại UI theo hướng
#   dẫn áp gói sau khi script báo xong.
# - Chỉ dùng lệnh chuẩn cp/mkdir/printf/sha256sum; không cần quyền root.
# - File bắt buộc trong backup: accounts.json (thiếu -> lỗi, dừng ngay).
#   audit.jsonl: nếu backup CÓ thì copy + kiểm sha256; nếu backup KHÔNG
#   có thì chỉ cảnh báo và vẫn làm tiếp (audit là best-effort; marker
#   C-25 vẫn chặn xem mã cũ cho tới khi xoay mã, không phụ thuộc audit).
# - File KHÔNG bao giờ khôi phục từ backup (C-24): view_secret.hash +
#   center_view_state.json — nếu backup có 2 file này thì script BỎ QUA
#   (không copy) và cảnh báo. Marker cũ center_view_restored.json trong
#   backup (nếu có) cũng bị bỏ qua — script luôn ghi marker MỚI.
#
# Mã thoát: 0 = xong (marker đã kiểm hợp lệ); 1 = lỗi (ghi rõ lý do);
# 2 = sai cách dùng / tham số trỏ vào secrets/.

set -u
# Không dùng "set -e": script tự kiểm từng bước và dọn thư mục tạm.

BACKUP="${1:-}"
DEST="${2:-}"

fail() {
    echo "LỖI: $*" >&2
    exit 1
}

warn() {
    echo "CẢNH BÁO: $*" >&2
}

# --- 1. Kiểm tham số + từ chối secrets/ -------------------------------
if [ -z "$BACKUP" ] || [ -z "$DEST" ]; then
    echo "Cách dùng: bash pc/ops/restore_ui_config.sh <thư mục backup> <thư mục ui-config>" >&2
    exit 2
fi

# Từ chối khi bất kỳ tham số nào trỏ vào thư mục secrets/ (kiểm chuỗi
# đường dẫn: thành phần "secrets" ở đầu, giữa hoặc cuối đường dẫn).
case "/$BACKUP/" in
    */secrets/*) echo "LỖI: thư mục backup trỏ vào secrets/ — không khôi phục secret từ backup (doc 06 §2)." >&2; exit 2 ;;
esac
case "/$DEST/" in
    */secrets/*) echo "LỖI: thư mục đích là secrets/ — script này chỉ khôi phục ui-config/, không chạm secrets/." >&2; exit 2 ;;
esac

[ -d "$BACKUP" ] || fail "thư mục backup không tồn tại hoặc không phải thư mục: $BACKUP"

# File bắt buộc: accounts.json (không có thì không biết khôi phục gì).
[ -f "$BACKUP/accounts.json" ] || fail "backup thiếu file bắt buộc accounts.json: $BACKUP"

# audit.jsonl: có thì sẽ copy + kiểm sha256; thiếu thì chỉ cảnh báo.
if [ -f "$BACKUP/audit.jsonl" ]; then
    HAVE_AUDIT=1
else
    HAVE_AUDIT=0
    warn "backup không có audit.jsonl — vẫn khôi phục tiếp (audit chỉ là best-effort; marker C-25 vẫn chặn xem mã cũ cho tới khi xoay mã)."
fi

# 2 file C-24 không bao giờ khôi phục: chỉ cảnh báo khi backup có chúng.
for forbidden in view_secret.hash center_view_state.json center_view_restored.json; do
    if [ -e "$BACKUP/$forbidden" ]; then
        warn "backup có $forbidden — BỎ QUA, không copy (C-24/C-25: hash + trạng thái xem mã + marker cũ không vào backup; script ghi marker mới)."
    fi
done

# --- 2. Copy vào thư mục tạm cạnh đích, kiểm sha256 --------------------
DEST_PARENT="$(dirname "$DEST")"
[ -d "$DEST_PARENT" ] || mkdir -p "$DEST_PARENT" || fail "không tạo được thư mục cha của đích: $DEST_PARENT"

STAGING="$DEST_PARENT/.ui-config.restore.$$"
OLD="$DEST_PARENT/.ui-config.old.$$"
trap 'rm -rf "$STAGING" "$OLD"' EXIT

mkdir -p "$STAGING" || fail "không tạo được thư mục tạm: $STAGING"
cp -a "$BACKUP/." "$STAGING/" || fail "copy backup vào thư mục tạm thất bại"
# Loại 3 file không được khôi phục khỏi bản đã copy (nếu backup có).
rm -f "$STAGING/view_secret.hash" "$STAGING/center_view_state.json" \
      "$STAGING/center_view_restored.json"

# Kiểm sha256: mọi file thường trong backup (trừ 3 file trên) phải có
# trong thư mục tạm với đúng nội dung.
while IFS= read -r -d '' src; do
    rel="${src#$BACKUP/}"
    case "$rel" in
        view_secret.hash|center_view_state.json|center_view_restored.json) continue ;;
    esac
    [ -f "$STAGING/$rel" ] || fail "thiếu file sau copy: $rel"
    sum_src="$(sha256sum "$src" | cut -d' ' -f1)"
    sum_dst="$(sha256sum "$STAGING/$rel" | cut -d' ' -f1)"
    [ "$sum_src" = "$sum_dst" ] || fail "sha256 lệch sau copy: $rel"
done < <(find "$BACKUP" -type f -print0)

# --- 3. Đổi tên nguyên tử: tạm -> đích ---------------------------------
if [ -e "$DEST" ]; then
    mv "$DEST" "$OLD" || fail "không dời thư mục ui-config/ cũ sang chỗ tạm"
    if ! mv "$STAGING" "$DEST"; then
        mv "$OLD" "$DEST" 2>/dev/null || true
        fail "không đổi tên thư mục tạm thành đích — đã trả lại thư mục cũ"
    fi
    rm -rf "$OLD"
else
    mv "$STAGING" "$DEST" || fail "không đổi tên thư mục tạm thành đích"
fi

# --- 4. Ghi marker NGAY trong cùng lần chạy, TRƯỚC khi in "xong" ------
RESTORED_AT="$(date +%s)"
MARKER="$DEST/center_view_restored.json"
printf '{"restored_at": %s}' "$RESTORED_AT" > "$MARKER" \
    || fail "không ghi được marker: $MARKER"

# --- 5. Kiểm tra cuối: marker tồn tại + JSON hợp lệ --------------------
[ -f "$MARKER" ] || fail "marker không tồn tại sau khi ghi: $MARKER"
if command -v python3 >/dev/null 2>&1; then
    python3 - "$MARKER" <<'PYEOF' || fail "marker không phải JSON hợp lệ: $MARKER"
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)
assert isinstance(data, dict), "marker phải là object JSON"
assert isinstance(data.get("restored_at"), int), "restored_at phải là số nguyên (epoch giây)"
PYEOF
else
    grep -Eq '^\{"restored_at": [0-9]+\}$' "$MARKER" \
        || fail "marker không đúng định dạng {\"restored_at\": <epoch>}: $MARKER"
fi

trap - EXIT
echo "Xong: đã khôi phục ui-config/ từ $BACKUP (restored_at=$RESTORED_AT)."
echo "CẢNH BÁO: script KHÔNG tự khởi động lại UI — agent PC khởi động lại UI theo hướng dẫn áp gói, rồi xoay mã bí mật trung tâm trước khi cho xem mã trở lại."
exit 0
