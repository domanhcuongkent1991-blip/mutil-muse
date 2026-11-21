"""Tầng dữ liệu của Web UI — chỉ đọc file trên PC, không HTTP, không
secret, không gọi API nền tảng (``05`` §1/§4/§5).

Nguồn dữ liệu duy nhất cho lịch sử (``05`` §4): tin đến đọc từ file
hàng đợi của tai, trả lời đọc từ file trả lời do receiver ghi (GĐ3b).
KHÔNG có file tóm tắt toàn văn thứ hai. UI hợp nhất 2 luồng theo thời
gian, ghép cặp qua ``in_reply_to_event_key``.

Chế độ hiển thị theo bot:

- GĐ3b (đã đồng bộ, file trả lời còn mới): hiện toàn văn 2 chiều.
- GĐ3a (chưa có file trả lời, hoặc đồng bộ chậm hơn 30 phút): CHỈ
  hiện tin đến + 3 trạng thái đúng chữ trong ``05`` §4, kèm dòng ghi
  rõ giới hạn. CẤM các trạng thái "đã trả lời", "đang chờ xử lý",
  "đang trả lời", "bị loại", "lỗi xử lý" — UI không biết.

Cảnh báo (``05`` §5): huy hiệu CHỈ thấy tín hiệu phía PC (trạng thái
unit, ``last_poll_ok``, cờ 401/429, tồn ước lượng, trạng thái đồng
bộ). Tín hiệu phía Muse (nghẽn lane D3, kẹt claim) UI KHÔNG thấy —
không được ghi huy hiệu cho những tín hiệu đó.

Module này không chạy lệnh hệ thống: trạng thái unit do tầng app
truyền vào (đo bằng ``systemctl``), sức khỏe đo bằng ``last_poll_ok``
chứ không bằng "tiến trình còn chạy" (``05`` §1).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Số liệu theo thiết kế / SUY LUẬN (ghi rõ nguồn từng số).
PREVIEW_LIMIT = 40  # ký tự Unicode, cắt ở ranh giới ký tự (05 §1).
STALE_AFTER_SECONDS = 300  # state cũ quá 5 phút -> stale (04 §1.1).
DEAD_AFTER_SECONDS = 900  # chết >15 phút -> báo đỏ (UI5).
SYNC_STALE_AFTER_SECONDS = 1800  # đồng bộ lỗi quá 30 phút -> về GĐ3a (04 §4).
BACKLOG_ALERT_THRESHOLD = 10  # SUY LUẬN: tồn ước lượng >= 10 thì vàng.
HISTORY_PER_PAGE = 50  # phân trang lịch sử (05 §4).
ACTIVITY_LIMIT = 30  # dòng hoạt động mới nhất toàn hệ (05 §1: 20-50).

# --- Hằng số V2 (thiết kế lại giao diện, UI-DESIGN-V2.md) -----------------
ZALO_MONTHLY_QUOTA = 3000  # gói BASIC, cùng số với muse-side/accounting.
QUOTA_WARN_FRACTION = 0.8  # SUY LUẬN: cảnh báo quota ở 80% (2400/3000).
# GC-G04 (T3m): 1 khoảng duy nhất [0.5, 0.95] cho ngưỡng quota,
# 1 nguồn hằng số duy nhất — cả tầng kiểm tra
# (_normalize_alert_fraction) lẫn tầng tính
# (quota_estimate_from_counters) đều dùng 2 hằng số này (SPEC TT20).
QUOTA_WARN_FRACTION_MIN = 0.5
QUOTA_WARN_FRACTION_MAX = 0.95

# Cài đặt cảnh báo quota (v0.5.0, T3g — SPEC TT20, DESIGN §6):
# file ui-config/alert_settings.json, khóa quota_warn_fraction.
ALERT_WARN_FRACTION_MIN = QUOTA_WARN_FRACTION_MIN
ALERT_WARN_FRACTION_MAX = QUOTA_WARN_FRACTION_MAX
ALERT_HISTORY_DAYS = 30  # lịch sử cảnh báo giữ 30 ngày (V2 §5).
POLL_HISTORY_DAYS = 7  # lịch sử lần lấy tin giữ 7 ngày (SUY LUẬN).
POLL_SAMPLE_MIN_GAP_SECONDS = 300  # ghi mẫu sức khỏe tối đa 5 phút/lần.
ATTENTION_MINUTES = 3  # "cần chú ý": tin chưa lấy mà tồn quá 3 phút
# (diễn giải ngưỡng D3 "lane >3 phút" ở mức từng tin; xem WEB-UI.md §8).
ALERT_EVENT_LIMIT = 100  # dòng sự kiện cảnh báo tối đa khi đọc.
POLL_SAMPLE_LIMIT = 48  # điểm tối đa trên dải sức khỏe trang chi tiết.

# Khóa cảnh báo do compute_alerts sinh ra (ack chỉ chấp nhận khóa này).
ALERT_KEYS = ("token", "dead", "stale", "unit", "rate", "backlog",
              "sync", "token-pending", "awaiting-sync", "dual-fetch")
# Khóa cảnh báo "cần chủ nhân" (đợt mẫu rẻ Hades): ánh xạ tĩnh theo key,
# KHÔNG theo level — 4 key này hiện trùng 4 key đỏ là trùng hợp (GC-03).
# token: token hỏng, chỉ chủ nhân thay được. dead: bot không lấy tin
# > 15 phút, sự cố hệ thống. unit: tiến trình tai không chạy.
# dual-fetch: 2 nhãn cùng lấy, chỉ chủ nhân gỡ trùng.
NEEDS_OWNER_KEYS = ("token", "dead", "unit", "dual-fetch")


def alert_needs_owner(alert):
    """True khi cảnh báo thuộc nhóm chỉ chủ nhân xử lý được."""
    return isinstance(alert, dict) and alert.get("key") in NEEDS_OWNER_KEYS
# Nhãn dự phòng khi file trạng thái cũ không còn giữ câu cảnh báo.
ALERT_KEY_LABELS = {
    "token": "Token có vấn đề — bot đang chờ token mới",
    "dead": "Bot không lấy được tin hơn 15 phút",
    "stale": "Chưa lấy được tin mới hơn 5 phút",
    "unit": "Tiến trình tai không chạy",
    "rate": "Đang bị giới hạn tốc độ (429) — chờ backoff",
    "backlog": "Tồn ước lượng quá ngưỡng",
    "sync": "Đồng bộ trả lời chậm hơn 30 phút — đang hiện giao diện rút gọn",
    "token-pending": "Đang chờ xác minh token 2 đầu (PC xong, Muse chưa)",
    "awaiting-sync": "Chờ đồng bộ về Muse — chưa thấy lần lấy tin đầu tiên",
    "dual-fetch": "Có thể 2 tài khoản đang cùng lấy tin bot này",
}

# --- Hằng số doc 09 §7 (v0.4) — mọi chỗ dùng gắn trỏ §7 (N29) ---------
DUAL_FETCH_WINDOW_SECONDS = 300  # cửa sổ trùng lấy 5 phút (doc 09 §7).
DUAL_FETCH_MIN_COUNT = 2  # lượt tối thiểu/nhãn trong cửa sổ (doc 09 §7).
DUAL_FETCH_ACK_SECONDS = 86400  # ack khóa dual-fetch hết hạn 24h (§7).
TRANSFER_EXPIRY_SECONDS = 72 * 3600  # hết hạn luồng chuyển (§7).
TRANSFER_QUIET_SECONDS = 90  # lặng-fetch ở bước 1 (§7).
TRANSFER_STEP7_TIMEOUT_SECONDS = 900  # chờ tin test bước 7 (§7).
TRANSFER_ATTEST_OPEN_SECONDS = 300  # mở tầng xác nhận sau 5 phút (§7).
TRANSFER_REPLY_WINDOW_SECONDS = 600  # cửa sổ ghép cặp trả lời (§7).
TRANSFER_REASON_MAX_CHARS = 200  # giới hạn ô lý do ~200 ký tự (§7).
# Mức nghiêm trọng hiển thị của cảnh báo (V2 §5).
SEVERITY_LABELS = {"red": "Nghiêm trọng", "yellow": "Cảnh báo"}

# Trạng thái GĐ3a — ĐÚNG CHỮ theo 05 §4, không thêm trạng thái khác.
STATUS_NEW = "Mới nhận — Muse chưa lấy"
STATUS_FETCHED = "Muse đã lấy"
CHIP_NO_REPLY_SEEN = "chưa thấy trả lời trên UI — xem trong cuộc trò chuyện"

# Dòng ghi rõ giới hạn GĐ3a, hiện thường xuyên ở đầu trang (05 §4).
G3A_NOTICE = (
    "Trang này chỉ hiện tin nhắn gửi tới bot. Câu trả lời của bot xem "
    "trong chính cuộc trò chuyện Zalo/Telegram. \u201cMuse đã lấy\u201d "
    "nghĩa là tin đã tới bộ não, chưa chắc đã trả lời xong."
)

_REDACT_AUTH = re.compile(r"(Authorization\s*:\s*)\S+", re.IGNORECASE)
_REDACT_BOT_URL = re.compile(r"/bot[A-Za-z0-9:_\-./]+/")
_REDACT_BEARER = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)

# Mẫu account_id: cùng mẫu bắt buộc như bot_id (DESIGN §10, doc 08 §2.4).
ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# Bảng bỏ dấu tiếng Việt cho slug hóa tên gọi -> account_id.
_VIETNAMESE_FOLD = str.maketrans({
    "à": "a", "á": "a", "ả": "a", "ã": "a", "ạ": "a",
    "ă": "a", "ằ": "a", "ắ": "a", "ẳ": "a", "ẵ": "a", "ặ": "a",
    "â": "a", "ầ": "a", "ấ": "a", "ẩ": "a", "ẫ": "a", "ậ": "a",
    "đ": "d",
    "è": "e", "é": "e", "ẻ": "e", "ẽ": "e", "ẹ": "e",
    "ê": "e", "ề": "e", "ế": "e", "ể": "e", "ễ": "e", "ệ": "e",
    "ì": "i", "í": "i", "ỉ": "i", "ĩ": "i", "ị": "i",
    "ò": "o", "ó": "o", "ỏ": "o", "õ": "o", "ọ": "o",
    "ô": "o", "ồ": "o", "ố": "o", "ổ": "o", "ỗ": "o", "ộ": "o",
    "ơ": "o", "ờ": "o", "ớ": "o", "ở": "o", "ỡ": "o", "ợ": "o",
    "ù": "u", "ú": "u", "ủ": "u", "ũ": "u", "ụ": "u",
    "ư": "u", "ừ": "u", "ứ": "u", "ử": "u", "ữ": "u", "ự": "u",
    "ỳ": "y", "ý": "y", "ỷ": "y", "ỹ": "y", "ỵ": "y",
})


def now_epoch():
    return time.time()


def parse_iso_to_epoch(text):
    """ISO-8601 -> epoch giây; không parse được -> None."""
    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = text.strip()
    if cleaned.endswith(("Z", "z")):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def format_hms(epoch):
    """Epoch -> HH:MM:SS giờ địa phương của PC."""
    try:
        return datetime.fromtimestamp(epoch).astimezone().strftime("%H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "--:--:--"


def format_day_time(epoch):
    """Epoch -> DD/MM HH:MM giờ địa phương của PC."""
    try:
        return datetime.fromtimestamp(epoch).astimezone().strftime("%d/%m %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "--/-- --:--"


def preview_text(text, limit=PREVIEW_LIMIT):
    """Xem trước ≤40 ký tự Unicode, cắt ở ranh giới ký tự (05 §1).

    Xuống dòng thành khoảng trắng; cắt thì thêm "…". ``None`` -> "".
    Python cắt theo code point nên không cắt giữa 1 emoji đơn; chuỗi
    emoji nối ZWJ có thể bị cắt giữa cụm — chấp nhận, ghi rõ ở đây.
    """
    if not isinstance(text, str):
        return ""
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "…"


def short_id(value, length=8):
    """Rút gọn định danh để hiển thị (tối đa 8 ký tự, SPEC S6(5))."""
    text = "" if value is None else str(value)
    return text[:length]


def read_json_file(path):
    """Đọc JSON khoan dung: thiếu/hỏng/không phải JSON -> None."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def write_json_file(path, payload, mode=0o600):
    """Ghi JSON nguyên tử (tmp + fsync + rename), đặt quyền mode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp_path, mode)
    except OSError:
        pass
    os.replace(tmp_path, path)


def read_jsonl_rows(path):
    """Đọc file .jsonl: list các dòng dict hợp lệ (bỏ dòng hỏng).

    File hàng đợi/trả lời có giới hạn ~5000 dòng nên đọc toàn bộ là đủ.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_registry(root):
    """Đọc registry.json; thiếu/hỏng -> {"version": 1, "bots": {}}."""
    data = read_json_file(Path(root) / "registry.json")
    if not isinstance(data, dict):
        return {"version": 1, "bots": {}}
    bots = data.get("bots")
    if isinstance(bots, dict):
        return {"version": data.get("version", 1), "bots": bots}
    if isinstance(bots, list):
        merged = {}
        for entry in bots:
            if isinstance(entry, dict) and entry.get("bot_id"):
                merged[entry["bot_id"]] = entry
        return {"version": data.get("version", 1), "bots": merged}
    return {"version": 1, "bots": {}}


@contextlib.contextmanager
def _registry_lock(root):
    """Khóa flock cho chu trình đọc-sửa-ghi registry.json."""
    lock_path = Path(root) / "registry.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def save_registry(root, data):
    """Ghi registry.json kiểu tmp+rename dưới khóa flock."""
    path = Path(root) / "registry.json"
    with _registry_lock(root):
        tmp_path = path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    return data


def modify_registry(root, mutate_fn):
    """Đọc + sửa + ghi registry trong cùng 1 khóa (chống race).

    ``mutate_fn`` nhận dict registry, sửa tại chỗ, trả về giá trị tùy
    ý (trả lại cho caller). Trả về (giá trị của mutate_fn, registry).
    """
    path = Path(root) / "registry.json"
    with _registry_lock(root):
        data = read_json_file(path)
        if not isinstance(data, dict) or not isinstance(
            data.get("bots"), dict
        ):
            data = {"version": 1, "bots": {}}
        result = mutate_fn(data)
        tmp_path = path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    return result, data


def modify_registry_checked(root, mutate_fn):
    """Như ``modify_registry`` nhưng registry hỏng thì TỪ CHỐI ghi.

    Dùng cho bước 6 của luồng chuyển (doc 09 §4.3): registry hỏng /
    malformed thì raise ValueError và KHÔNG rebuild rỗng, không ghi
    gì. ``mutate_fn`` raise thì cũng không ghi gì (raise-để-hủy, để
    kiểm tra đồng thời bên trong khóa có hiệu lực).
    """
    path = Path(root) / "registry.json"
    with _registry_lock(root):
        data = read_json_file(path)
        if not isinstance(data, dict) or not isinstance(
            data.get("bots"), dict
        ):
            raise ValueError("registry.json hỏng — từ chối ghi")
        result = mutate_fn(data)
        tmp_path = path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    return result, data


# ---------------------------------------------------------------------------
# Tài khoản Muse (doc 08 §4, MA5/MA6) — danh sách quản lý, không phải
# đăng nhập nhiều người dùng. File không chứa secret, chỉ tên + ghi chú.
# ---------------------------------------------------------------------------

def slugify_account_id(display_name):
    """Tên gọi -> account_id ứng cử: chữ thường, bỏ dấu, gạch ngang.

    Theo mẫu DESIGN §10 (tối đa 32 ký tự). Rỗng sau khi slug hóa thì
    trả về "tai-khoan". KHÔNG kiểm tra trùng — việc đó của
    ``unique_account_id``.
    """
    text = (display_name or "").strip().lower().translate(_VIETNAMESE_FOLD)
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    slug = slug[:32].strip("-")
    if not slug or not slug[0].isalnum():
        return "tai-khoan"
    return slug


def unique_account_id(accounts, base):
    """account_id duy nhất trong ``accounts``: trùng thì thêm -2/-3...

    Cắt gốc để hậu tố vẫn vừa 32 ký tự. ``accounts`` là dict
    {account_id: record}.
    """
    candidate = base[:32].strip("-") or "tai-khoan"
    if candidate not in accounts:
        return candidate
    index = 2
    while True:
        suffix = "-%d" % index
        candidate = (base[:32 - len(suffix)].strip("-") + suffix)
        if candidate not in accounts:
            return candidate
        index += 1


# --- Màu chat + công tắc thảo luận (v0.6.0, T3l — doc 10 §3.8 §6a,
# Q-V060-34 ĐÃ CHỐT, quyết D10) ----------------------------------------
# Quy tắc cấp màu mặc định (ghi rõ để đội sau khỏi đoán):
# - ``muse-chinh`` tím #7c3aed, ``muse-phu`` xanh lá #16a34a (cố định
#   theo account_id, không theo thứ tự);
# - chủ nhân ("owner", không nằm trong accounts.json) xám đậm #57534e;
# - tài khoản thêm sau nhận lần lượt #c2410c, #be185d, #475569 theo
#   thứ tự xuất hiện trong accounts.json (thứ tự ghi file = thứ tự
#   tham gia, vì tài khoản mới được thêm vào cuối file); hết 3 màu
#   thì quay vòng lại từ #c2410c.
# Màu đã đặt tay trong file (hex #rrggbb hợp lệ) KHÔNG bị ghi đè.
CENTER_CHAT_COLOR_OWNER = "#57534e"
CENTER_CHAT_COLOR_FIXED = {"muse-chinh": "#7c3aed",
                           "muse-phu": "#16a34a"}
CENTER_CHAT_COLOR_EXTRA = ("#c2410c", "#be185d", "#475569")
CENTER_CHAT_COLOR_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# 1 lượt thảo luận = 1 tin hoi có to_account; chạm 5 lượt thì vòng dừng.
CENTER_DISCUSSION_ROUND_MAX_TURNS = 5
CENTER_DISCUSSION_STOPPED_LEVEL = "thông tin"


def _default_chat_color(account_id, extra_index):
    """Màu mặc định cho tài khoản chưa đặt tay (xem quy tắc trên)."""
    fixed = CENTER_CHAT_COLOR_FIXED.get(account_id)
    if fixed is not None:
        return fixed
    return CENTER_CHAT_COLOR_EXTRA[
        extra_index % len(CENTER_CHAT_COLOR_EXTRA)]


def center_chat_colors(root):
    """{account_id: màu chat} + "owner" -> xám đậm — cho khung chat.

    Màu lấy đúng như ``read_accounts`` đã cấp (theo thứ tự xuất
    hiện trong accounts.json); nhánh dự phòng chỉ chạy khi
    ``read_accounts`` không cấp được màu thì liệt kê theo thứ tự
    xuất hiện như ``read_accounts`` đã làm (G-L6: trước đây truyền
    ``extra_index=0`` cho mọi tài khoản thêm sau nên mọi tài khoản
    đều nhận cùng 1 màu #c2410c)."""
    accounts = (read_accounts(root).get("accounts") or {})
    colors = {}
    extra_index = 0
    for account_id, record in accounts.items():
        color = record.get("chat_color")
        if not isinstance(color, str) \
                or not CENTER_CHAT_COLOR_HEX_RE.match(color):
            # Giong read_accounts: chi tai khoan khong co mau dat tay
            # hop le moi chiem 1 vi tri trong day mau them.
            color = _default_chat_color(account_id, extra_index)
            if account_id not in CENTER_CHAT_COLOR_FIXED:
                extra_index += 1
        colors[account_id] = color
    colors["owner"] = CENTER_CHAT_COLOR_OWNER
    return colors


def _as_to_account_list(value):
    """Chuẩn hóa to_account (chuỗi v3 / danh sách v4 / None) về list.

    Dùng chung cho phía UI khi đếm vòng thảo luận (store/receiver có
    hàm riêng cùng tên theo C-18 — không import chéo, mỗi tầng 1 bản
    sao nhỏ, cùng luật: chuỗi 1 người -> [chuỗi]; None/rỗng -> [])."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def read_accounts(root):
    """Đọc ui-config/accounts.json -> {"accounts": {id: record}}.

    Thiếu/hỏng -> danh sách rỗng. Chỉ giữ mục có account_id khớp mẫu
    và display_name là chuỗi; mục lạ bị bỏ (không crash, không đoán).
    Cờ ``center_participating`` được giữ nguyên khi là bool thật
    (doc 10 §5.5, mẫu ``center_store.read_accounts``); vắng cờ hoặc
    giá trị lạ (chuỗi/số/...) thì về False, không đoán, không crash.

    (v0.6.0, T3l — doc 10 §3.8 §6a, Q-V060-34 ĐÃ CHỐT) Mỗi tài khoản
    1 màu chat riêng, lưu khóa ``chat_color`` trong accounts.json;
    tài khoản thiếu khóa thì nhận màu mặc định theo quy tắc cấp màu
    (xem ``CENTER_CHAT_COLORS_*`` + ``_default_chat_color`` — KHÔNG
    ghi đè màu đã đặt tay; màu lạ (không phải hex #rrggbb) thì về
    mặc định). Công tắc thảo luận ``center_discussion_enabled``
    (bool, mặc định True; vắng/lạ -> True).
    """
    data = read_json_file(Path(root) / "ui-config" / "accounts.json")
    accounts = {}
    if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
        extra_index = 0
        for account_id, record in data["accounts"].items():
            if not isinstance(account_id, str) \
                    or not ACCOUNT_ID_RE.match(account_id):
                continue
            if not isinstance(record, dict):
                continue
            name = record.get("display_name")
            if not isinstance(name, str) or not name.strip():
                continue
            flag = record.get("center_participating")
            removed = record.get("center_removed")
            color = record.get("chat_color")
            if not isinstance(color, str) \
                    or not CENTER_CHAT_COLOR_HEX_RE.match(color):
                color = _default_chat_color(account_id, extra_index)
                if account_id not in CENTER_CHAT_COLOR_FIXED:
                    extra_index += 1
            discussion = record.get("center_discussion_enabled", True)
            accounts[account_id] = {
                "display_name": name,
                "notes": record.get("notes") if isinstance(
                    record.get("notes"), str) else "",
                "created_at": record.get("created_at") if isinstance(
                    record.get("created_at"), str) else "",
                "center_participating": flag if isinstance(flag, bool)
                else False,
                # (v0.6.0, T3j — DESIGN §3.11 §4.2) Phân biệt "tạm
                # ngưng" (chỉ tắt cờ, giữ mã — vẫn được xem mã) với
                # "gỡ hẳn" (tắt cờ + xoay mã — KHÔNG được xem mã):
                # cả 2 đều tắt center_participating nên cần cờ riêng.
                # Gỡ hẳn đặt True, tham gia (lại) xóa về False.
                # Vắng/lạ -> False (tài khoản cũ chưa có cờ này được
                # coi là chưa từng gỡ — đúng với trạng thái thật vì
                # gỡ hẳn trước v0.6.0 cũng chỉ tắt cờ).
                "center_removed": removed if isinstance(removed, bool)
                else False,
                "center_discussion_enabled": (
                    discussion if isinstance(discussion, bool)
                    else True),
                "chat_color": color,
            }
    return {"accounts": accounts}


@contextlib.contextmanager
def _accounts_lock(root):
    """Khóa flock riêng cho chu trình đọc-sửa-ghi accounts.json."""
    lock_path = Path(root) / "ui-config" / "accounts.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def modify_accounts(root, mutate_fn):
    """Đọc + sửa + ghi accounts.json trong cùng 1 khóa (chống race).

    ``mutate_fn`` nhận dict {"accounts": {...}}, sửa tại chỗ, trả về
    giá trị tùy ý. Trả về (giá trị của mutate_fn, dữ liệu đã ghi).
    """
    path = Path(root) / "ui-config" / "accounts.json"
    with _accounts_lock(root):
        data = read_accounts(root)
        result = mutate_fn(data)
        write_json_file(path, data, mode=0o600)
    return result, data


# ---------------------------------------------------------------------------
# Cài đặt chung (nhóm Giao diện) — trạng thái đóng/mở node (T3, v0.2.3).
# Nguồn duy nhất: DESIGN-TRUNG-TAM.md §8.1 (13 khóa theo TÊN node,
# chung 1 node); bảng mặc định ở SPEC-TRUNG-TAM.md TT11 (7 ĐÓNG /
# 6 MỞ). File chỉ chứa trạng thái hiển thị — không secret, không dữ
# liệu bot. Khóa lạ trong file thì bỏ qua lặng lẽ.
# ---------------------------------------------------------------------------

# Thứ tự khóa theo bảng TT11 (trang chính, rồi trang chi tiết bot).
UI_PREF_KEYS = (
    "five-stats", "bot-cards", "activity", "open-alerts",
    "event-history-30d", "health", "controls", "config", "stats-7d",
    "alerts-history-detail", "fetches", "message-history", "ear-log",
)

# Node ĐÓNG theo mặc định (7 node). Node vắng mặt = MỞ.
UI_PREF_DEFAULT_COLLAPSED = frozenset((
    "activity", "event-history-30d", "config", "alerts-history-detail",
    "fetches", "message-history", "ear-log",
))

# "Mở tất cả / Đóng tất cả" chỉ trong tab hiện tại (Q-T3-06).
UI_PREF_TAB_KEYS = {
    "all": ("five-stats", "bot-cards", "activity"),
    "zalo": ("five-stats", "bot-cards", "activity"),
    "telegram": ("five-stats", "bot-cards", "activity"),
    "alerts": ("open-alerts", "event-history-30d"),
    "bot-detail": ("health", "controls", "config", "stats-7d",
                   "alerts-history-detail", "fetches", "message-history",
                   "ear-log"),
}


def _ui_prefs_path(root):
    return Path(root) / "ui-config" / "ui_prefs.json"


@contextlib.contextmanager
def _ui_prefs_lock(root):
    """Khóa flock riêng cho ui_prefs.json (không dùng chung lock với
    registry/accounts để toggle không chặn thao tác tài khoản/bot)."""
    lock_path = Path(root) / "ui-config" / "ui_prefs.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _normalize_ui_prefs(data):
    """Chuẩn hóa dữ liệu prefs về {khóa: bool đóng?} đủ 13 khóa.

    File lưu dạng {"version": 1, "collapsed": {khóa: true, ...}} — chỉ
    lưu trạng thái ĐÓNG; khóa vắng = MỞ. Dữ liệu hỏng/sai schema ->
    mặc định 13 node, không crash, không ghi đè file hỏng.
    """
    collapsed = {}
    if isinstance(data, dict):
        raw = data.get("collapsed")
        if isinstance(raw, dict):
            for key in UI_PREF_KEYS:
                value = raw.get(key)
                collapsed[key] = value is True
            return collapsed
    return {key: key in UI_PREF_DEFAULT_COLLAPSED
            for key in UI_PREF_KEYS}


# -- Công tắc theme (v0.6.0, T3k — phong cách C, frag-04 §3.2) --------
# Khóa "theme" cùng cấp với "version"/"collapsed" trong ui_prefs.json,
# KHÔNG nằm trong "collapsed" (nó không phải node đóng/mở). Chỉ nhận
# "light" (Sáng Superhuman, mặc định) hoặc "dark" (Tối Hades); giá trị
# lưu "dark" ánh xạ sang data-theme="hades", POST theme=hades KHÔNG
# hợp lệ. File thiếu khóa / giá trị lạ -> "light", không crash.
UI_THEME_VALUES = ("light", "dark")
UI_THEME_DEFAULT = "light"
UI_THEME_DATA_ATTRS = {"light": "light", "dark": "hades"}


def _load_ui_prefs_raw(root):
    """Đọc ui_prefs.json 1 lần -> (collapsed, theme, khóa-lạ).

    ``collapsed``: dict đủ 13 khóa (file hỏng -> mặc định, log 1 dòng
    như ``load_ui_prefs`` cũ); ``theme``: "light"/"dark" (thiếu/lạ ->
    "light"); ``khóa-lạ``: dict khóa top-level ngoài
    version/collapsed/theme (giữ nguyên khi ghi lại, luật GC-02).
    """
    data = read_json_file(_ui_prefs_path(root))
    if data is None:
        return _normalize_ui_prefs(None), UI_THEME_DEFAULT, {}
    if not isinstance(data, dict) or not isinstance(
            data.get("collapsed"), dict):
        # File hỏng/sai schema: về mặc định, không ghi đè file hỏng
        # (chủ nhân sửa tay hoặc bấm "Về mặc định" để ghi lại).
        try:
            os.write(2, b"ui-prefs: file hong, dung mac dinh 13 node\n")
        except OSError:
            pass
        collapsed = _normalize_ui_prefs(None)
    else:
        collapsed = _normalize_ui_prefs(data)
    theme = data.get("theme") if isinstance(data, dict) else None
    if theme not in UI_THEME_VALUES:
        theme = UI_THEME_DEFAULT
    extra = {}
    if isinstance(data, dict):
        extra = {key: value for key, value in data.items()
                 if key not in ("version", "collapsed", "theme")}
    return collapsed, theme, extra


def load_ui_theme(root):
    """Đọc khóa theme trong ui_prefs.json ("light"/"dark").

    File thiếu/hỏng/khóa lạ -> "light" (mặc định Superhuman), không
    crash, không ghi đè file hỏng.
    """
    _collapsed, theme, _extra = _load_ui_prefs_raw(root)
    return theme


def load_ui_prefs_full(root):
    """Đọc ui_prefs.json 1 lần -> (collapsed, theme).

    Dùng ở handler đã cần cả 2 (vd trang Cài đặt) để giữ hợp đồng
    "đọc 1 lần/request" (frag-04 §3.2).
    """
    collapsed, theme, _extra = _load_ui_prefs_raw(root)
    return collapsed, theme


def load_ui_prefs(root):
    """Đọc ui_prefs.json -> {khóa: True nếu node ĐÓNG}. File thiếu
    hoặc hỏng -> mặc định 13 node (TT11), ghi log 1 dòng, không crash.
    """
    collapsed, _theme, _extra = _load_ui_prefs_raw(root)
    return collapsed


def _write_ui_prefs_locked(root, collapsed, theme=None, extra=None):
    """Ghi ui_prefs.json nguyên tử (tmp + rename), quyền 600. Chỉ
    gọi khi đang giữ _ui_prefs_lock.

    Giữ nguyên khóa ``theme`` khi ghi ``collapsed`` và giữ nguyên
    khóa lạ top-level (frag-04 §3.2); theme mặc định "light" thì
    không ghi khóa (thiếu khóa == "light" khi đọc).
    """
    payload = {"version": 1,
               "collapsed": {key: True for key, closed in collapsed.items()
                             if closed and key in UI_PREF_KEYS}}
    if theme in UI_THEME_VALUES and theme != UI_THEME_DEFAULT:
        payload["theme"] = theme
    for key, value in (extra or {}).items():
        if key not in payload:
            payload[key] = value
    write_json_file(_ui_prefs_path(root), payload, mode=0o600)
    return payload


def set_ui_theme(root, theme):
    """Ghi khóa theme ("light"/"dark") vào ui_prefs.json, giữ nguyên
    collapsed + khóa lạ (đọc-sửa-ghi toàn file trong cùng 1 khóa).

    Giá trị ngoài 2 chuỗi trên -> ValueError (handler trả 400, không
    ghi gì). Trả về (theme_cũ, theme_mới).
    """
    if theme not in UI_THEME_VALUES:
        raise ValueError("theme không hợp lệ")
    with _ui_prefs_lock(root):
        collapsed, old_theme, extra = _load_ui_prefs_raw(root)
        _write_ui_prefs_locked(root, collapsed, theme=theme, extra=extra)
    return old_theme, theme


def modify_ui_prefs(root, mutate_fn):
    """Đọc + sửa + ghi ui_prefs.json trong cùng 1 khóa (chống race).

    ``mutate_fn`` nhận dict {khóa: bool đóng?} đủ 13 khóa, sửa tại
    chỗ, trả về giá trị tùy ý. Trả về (giá trị của mutate_fn, dict
    collapsed đã ghi). Ghi lại giữ nguyên khóa theme + khóa lạ.
    """
    with _ui_prefs_lock(root):
        collapsed, theme, extra = _load_ui_prefs_raw(root)
        result = mutate_fn(collapsed)
        _write_ui_prefs_locked(root, collapsed, theme=theme, extra=extra)
    return result, collapsed


def reset_ui_prefs(root):
    """Xóa file prefs để về mặc định 13 node (TT11). Trả về dict
    collapsed mặc định đã áp dụng.

    "Về mặc định" CHỈ reset 13 node — KHÔNG reset theme (frag-04
    §3.2): theme đang "dark" (hoặc file có khóa lạ) thì ghi lại file
    mặc định + giữ theme/khóa lạ; không có gì cần giữ thì xóa file
    như cũ.
    """
    with _ui_prefs_lock(root):
        _collapsed, theme, extra = _load_ui_prefs_raw(root)
        defaults = _normalize_ui_prefs(None)
        if theme == UI_THEME_DEFAULT and not extra:
            try:
                _ui_prefs_path(root).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        else:
            _write_ui_prefs_locked(root, defaults, theme=theme,
                                   extra=extra)
        return defaults


def _alert_settings_path(root):
    return Path(root) / "ui-config" / "alert_settings.json"


@contextlib.contextmanager
def _alert_settings_lock(root):
    """Khóa flock riêng cho alert_settings.json (cùng mẫu
    _ui_prefs_lock — không dùng chung lock với registry/accounts)."""
    lock_path = Path(root) / "ui-config" / "alert_settings.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _normalize_alert_fraction(value):
    """Chuẩn hóa ngưỡng cảnh báo quota về float trong [0.5, 0.95];
    thiếu/hỏng/ngoài khoảng/không phải số -> hằng số
    QUOTA_WARN_FRACTION, không crash (SPEC TT20, DESIGN §6)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return QUOTA_WARN_FRACTION
    if not QUOTA_WARN_FRACTION_MIN <= value <= QUOTA_WARN_FRACTION_MAX:
        return QUOTA_WARN_FRACTION
    return float(value)


def load_alert_settings(root):
    """Đọc ui-config/alert_settings.json -> float ngưỡng cảnh báo
    quota. File thiếu/hỏng/ngoài khoảng -> mặc định 0.8, không crash.
    Đọc 1 lần/request ở tầng app (không đọc lặp trong vòng lặp bot)."""
    data = read_json_file(_alert_settings_path(root))
    if not isinstance(data, dict):
        return QUOTA_WARN_FRACTION
    return _normalize_alert_fraction(data.get("quota_warn_fraction"))


def validate_alert_settings_updates(updates):
    """Kiểm tra dict cập nhật alert_settings.json (từ POST
    /api/settings/alerts). Trả về (clean, lỗi): khóa lạ -> lỗi (từ
    chối TOÀN BỘ, không ghi gì — SPEC TT15); giá trị ngoài [0.5,
    0.95] -> lỗi."""
    if not isinstance(updates, dict):
        return None, "dữ liệu không hợp lệ"
    clean = {}
    for key, raw in updates.items():
        if key != "quota_warn_fraction":
            return None, "khóa không hợp lệ: %s" % key
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
                or not QUOTA_WARN_FRACTION_MIN <= raw \
                <= QUOTA_WARN_FRACTION_MAX:
            return None, "ngưỡng cảnh báo quota phải trong khoảng 50–95%"
        clean[key] = float(raw)
    return clean, None


def save_alert_settings(root, updates):
    """Ghi các khóa đã kiểm tra (validate trước) vào
    ui-config/alert_settings.json: flock + tmp+rename, quyền 600.
    Khóa lạ ĐÃ CÓ trong file được giữ nguyên (cùng mẫu GC-02 của
    save_center_config — chỉ từ chối khóa lạ ở POST mới). Trả về
    ngưỡng đầy đủ sau khi ghi."""
    root = Path(root)
    path = _alert_settings_path(root)
    with _alert_settings_lock(root):
        data = read_json_file(path)
        if not isinstance(data, dict):
            data = {}
        # GC-G02 (T3m): giu nguyen version hien co neu la so nguyen
        # >= 1; chi dat version = 1 khi thieu/khong hop le. Khong
        # tu tang version (tang chi khi co dot doi schema that).
        version = data.get("version")
        if not isinstance(version, int) or isinstance(version, bool) \
                or version < 1:
            data["version"] = 1
        for key, value in updates.items():
            data[key] = value
        write_json_file(path, data, mode=0o600)
    return load_alert_settings(root)


def owner_account_of(entry):
    """owner_account của 1 mục registry: chuỗi hợp lệ hoặc None.

    Bot cũ thiếu trường thì coi như null (chưa gắn), vẫn chạy đúng
    (doc 08 §2.2/§7). Giá trị không phải chuỗi -> None.
    """
    if not isinstance(entry, dict):
        return None
    value = entry.get("owner_account")
    return value if isinstance(value, str) and value else None


def account_display_name(accounts, account_id):
    """Tên gọi của account_id, hoặc None khi chưa có trong danh sách."""
    if not isinstance(accounts, dict) or not account_id:
        return None
    record = accounts.get(account_id)
    if isinstance(record, dict):
        name = record.get("display_name")
        if isinstance(name, str) and name:
            return name
    return None


def bots_by_account(registry):
    """Nhóm bot_id theo owner_account; chưa gắn nằm ở khóa None."""
    grouped = {}
    bots = (registry or {}).get("bots") or {}
    for bot_id, entry in bots.items():
        grouped.setdefault(owner_account_of(entry), []).append(bot_id)
    for bot_ids in grouped.values():
        bot_ids.sort()
    return grouped


# ---------------------------------------------------------------------------
# Luồng chuyển bot — đọc trạng thái + predicate dùng chung (doc 09 §4.2).
# Ghi trạng thái (khóa riêng transfers.lock) nằm ở pc/ui/transfers.py.
# ---------------------------------------------------------------------------

def read_transfers(root):
    """Đọc ui-config/transfers.json -> {"version": 1, "transfers": {}}.

    Thiếu/hỏng -> rỗng. Chỉ giữ bản ghi là dict có đủ transfer_id /
    bot_id / status.
    """
    data = read_json_file(Path(root) / "ui-config" / "transfers.json")
    transfers = {}
    if isinstance(data, dict) and isinstance(data.get("transfers"), dict):
        for transfer_id, record in data["transfers"].items():
            if not isinstance(record, dict):
                continue
            if record.get("transfer_id") != transfer_id:
                continue
            if not isinstance(record.get("bot_id"), str):
                continue
            if not isinstance(record.get("status"), str):
                continue
            transfers[transfer_id] = record
    return {"version": 1, "transfers": transfers}


def is_transfer_active(record, now=None):
    """Predicate "đang chạy" DUY NHẤT (C6): status active VÀ chưa quá
    expires_at. Đánh giá lười ở mọi đường đọc; expires_at không parse
    được thì coi như hết hạn (không khóa treo vì bản ghi hỏng)."""
    if now is None:
        now = now_epoch()
    if not isinstance(record, dict) or record.get("status") != "active":
        return False
    expires = parse_iso_to_epoch(record.get("expires_at"))
    if expires is None:
        return False
    return now < expires


def transfer_step_done(record, step):
    """True khi bước ``step`` của luồng đã done."""
    steps = record.get("steps") if isinstance(record, dict) else None
    if not isinstance(steps, dict):
        return False
    entry = steps.get(str(step))
    return isinstance(entry, dict) and entry.get("status") == "done"


def active_transfer_for_bot(transfers_data, bot_id, now=None):
    """Luồng đang chạy (C6) của 1 bot, hoặc None. Có tối đa 1 luồng
    đang chạy/bot (mở luồng thứ 2 bị chặn); nếu file có 2 bản ghi lạ
    thì lấy bản mới tạo nhất."""
    if now is None:
        now = now_epoch()
    transfers = (transfers_data or {}).get("transfers") or {}
    candidates = [record for record in transfers.values()
                  if record.get("bot_id") == bot_id
                  and is_transfer_active(record, now)]
    if not candidates:
        return None
    candidates.sort(key=lambda r: str(r.get("created_at") or ""))
    return candidates[-1]


def read_transfer_log(root):
    """Đọc ui-config/transfer_log.jsonl -> list sự kiện dict hợp lệ."""
    return read_jsonl_rows(Path(root) / "ui-config" / "transfer_log.jsonl")


def persistent_reminder_for_bot(events, bot_id):
    """Dòng nhắc bền sau hủy/hết hạn/quay lại (R5, doc 09 §6.2).

    Suy từ SỰ KIỆN CUỐI của bot trong transfer_log.jsonl: cancelled /
    expired sau bước 6 (cleanup_owed=true), hoặc rolled_back mà Y vẫn
    giữ cấu hình (cleanup_owed=true). Sự kiện mới hơn (started,
    completed, cleanup_confirmed) thì hết nhắc. Trả về dict
    {"kind", "text"} hoặc None.
    """
    last = None
    for event in events or []:
        if isinstance(event, dict) and event.get("bot_id") == bot_id:
            last = event
    if last is None:
        return None
    detail = last.get("detail") if isinstance(last.get("detail"), dict) \
        else {}
    if not detail.get("cleanup_owed"):
        return None
    kind = last.get("kind")
    if kind in ("cancelled", "expired"):
        return {
            "kind": kind,
            "text": (
                "Luồng chuyển bot này đã %s sau khi đổi nhãn — tài khoản "
                "cũ vẫn có thể gửi tin. Dọn dẹp tay ở tài khoản cũ (gỡ "
                "credential khỏi Vault, xóa cấu hình lấy tin/đồng bộ) "
                "rồi bấm xác nhận đã dọn, hoặc bắt đầu luồng chuyển mới."
                % ("hủy" if kind == "cancelled" else "hết hạn")),
        }
    if kind == "rolled_back":
        return {
            "kind": kind,
            "text": (
                "Luồng chuyển đã quay lại nhưng tài khoản mới vẫn giữ "
                "secret — nợ dọn: gỡ cấu hình/credential của bot này ở "
                "tài khoản mới, rồi bấm xác nhận đã dọn."),
        }
    return None


def read_bot_config(root, bot_id):
    """Đọc config/<bot_id>.json (không secret); thiếu/hỏng -> {}."""
    data = read_json_file(Path(root) / "config" / ("%s.json" % bot_id))
    return data if isinstance(data, dict) else {}


def read_allowlist(root, bot_id):
    """Đọc config/<bot_id>.allowlist.json; thiếu/hỏng -> None.

    None nghĩa là "chưa có allowlist" (D5: cấm bật). File có nhưng
    rỗng dict vẫn là "có allowlist" (fail-closed ở dispatcher).
    """
    data = read_json_file(
        Path(root) / "config" / ("%s.allowlist.json" % bot_id))
    return data if isinstance(data, dict) else None


def read_state(root, bot_id):
    """Đọc data/<bot_id>/state.json của tai; thiếu/hỏng -> {}."""
    data = read_json_file(Path(root) / "data" / bot_id / "state.json")
    return data if isinstance(data, dict) else {}


def read_fetch_state(root):
    """Đọc gateway-fetch-state.json (file riêng của gateway)."""
    data = read_json_file(Path(root) / "gateway-fetch-state.json")
    return data if isinstance(data, dict) else {}


def _fetch_entry(fetch_state, bot_id):
    """Mục fetch của bot trong gateway-fetch-state.json; không có -> {}.

    Chấp nhận cả 2 dạng: {"bots": {bot_id: {...}}} (gateway ghi) và
    {bot_id: {...}} (dạng phẳng, dùng trong test cũ).
    """
    if not isinstance(fetch_state, dict):
        return {}
    bots = fetch_state.get("bots")
    entry = bots.get(bot_id) if isinstance(bots, dict) else None
    if not isinstance(entry, dict):
        entry = fetch_state.get(bot_id)
    return entry if isinstance(entry, dict) else {}


def fetch_state_entry(fetch_state, bot_id):
    """Mục fetch của bot (public wrapper của ``_fetch_entry``)."""
    return _fetch_entry(fetch_state, bot_id)


# ---------------------------------------------------------------------------
# Nhãn lấy tin tự khai + cảnh báo trùng lấy (doc 09 §3/§5, F27).
# Nhãn là lời tự khai, KHÔNG phải danh tính (MA8) — mọi câu chữ đều
# dùng "Có thể" và nêu "nhãn tự khai", CẤM khẳng định người lấy.
# ---------------------------------------------------------------------------

def active_fetch_labels(fetch_labels, now, window=DUAL_FETCH_WINDOW_SECONDS,
                        min_count=DUAL_FETCH_MIN_COUNT):
    """Nhãn đang hoạt động: ≥2 mốc trong recent_seen_at thuộc cửa sổ.

    ``fetch_labels`` là dict {nhãn: {recent_seen_at: [...]}} từ
    fetch-state. Dữ liệu hỏng/thiếu -> tập rỗng (mặc định an toàn:
    không báo, không crash — N11).
    """
    active = set()
    if not isinstance(fetch_labels, dict):
        return active
    cutoff = now - window
    for label, info in fetch_labels.items():
        if not isinstance(label, str) or not label:
            continue
        if not isinstance(info, dict):
            continue
        recent = info.get("recent_seen_at")
        if not isinstance(recent, list):
            continue
        count = 0
        for stamp in recent:
            epoch = parse_iso_to_epoch(stamp)
            if epoch is not None and epoch >= cutoff:
                count += 1
                if count >= min_count:
                    break
        if count >= min_count:
            active.add(label)
    return active


def labels_seen_since(fetch_labels, since_epoch):
    """Nhãn có dấu vết sau mốc ``since_epoch``: bất kỳ mốc nào trong
    recent_seen_at, hoặc last_seen_at, lớn hơn mốc đó (R3: request
    409/400 chỉ chạm last_seen_at nên vẫn tính là dấu vết)."""
    seen = set()
    if not isinstance(fetch_labels, dict):
        return seen
    for label, info in fetch_labels.items():
        if not isinstance(label, str) or not label:
            continue
        if not isinstance(info, dict):
            continue
        stamps = []
        recent = info.get("recent_seen_at")
        if isinstance(recent, list):
            stamps.extend(recent)
        last = info.get("last_seen_at")
        if isinstance(last, str):
            stamps.append(last)
        for stamp in stamps:
            epoch = parse_iso_to_epoch(stamp)
            if epoch is not None and epoch > since_epoch:
                seen.add(label)
                break
    return seen


def dual_fetch_alert_text(labels):
    """Câu cảnh báo §5.6 — CHỈ mang account_id (S16), tên gọi tra lúc
    hiển thị. ``labels`` đã sắp xếp để câu chữ ổn định."""
    joined = ", ".join(labels)
    return (
        "Có thể 2 tài khoản đang cùng lấy tin bot này — nhãn tự khai "
        "gần đây: %s. Kiểm tra queue secret có giao nhầm cho 2 tài "
        "khoản không. Nhãn do phía lấy tự khai nên đây là dấu hiệu, "
        "không phải bằng chứng chắc chắn." % joined
    )


def dual_fetch_display_text(labels, accounts):
    """Câu cảnh báo §5.6 kèm tên gọi (hiển thị): tên tra từ accounts
    lúc render; nhãn lạ kèm ghi chú "chưa có trong danh sách tài
    khoản". Tầng hiển thị phải esc() kết quả (tên do chủ nhân gõ)."""
    parts = []
    account_map = (accounts or {}).get("accounts") or {}
    for label in labels:
        name = account_display_name(account_map, label)
        if name:
            parts.append("%s (%s)" % (label, name))
        else:
            parts.append("%s (chưa có trong danh sách tài khoản)" % label)
    joined = ", ".join(parts)
    return (
        "Có thể 2 tài khoản đang cùng lấy tin bot này — nhãn tự khai "
        "gần đây: %s. Kiểm tra queue secret có giao nhầm cho 2 tài "
        "khoản không. Nhãn do phía lấy tự khai nên đây là dấu hiệu, "
        "không phải bằng chứng chắc chắn." % joined
    )


def dual_fetch_hidden(transfer, active_labels, now=None):
    """Predicate tạm ẩn DUY NHẤT (B6): luồng active (C6) VÀ
    current_step ≥ 4 VÀ chưa xong bước 8 VÀ tập nhãn đang hoạt động
    ⊆ {nhãn đích, nhãn cũ (cả 2 nguồn: from_account_id +
    from_label_at_start)}. Nhãn thứ 3 -> không ẩn. Ẩn chỉ ở tầng
    hiển thị (R4) — hàm này không xóa/sửa trạng thái cảnh báo."""
    if now is None:
        now = now_epoch()
    if not is_transfer_active(transfer, now):
        return False
    try:
        current_step = int(transfer.get("current_step"))
    except (TypeError, ValueError):
        return False
    if current_step < 4:
        return False
    if transfer_step_done(transfer, 8):
        return False
    allowed = set()
    for key in ("to_account_id", "from_account_id", "from_label_at_start"):
        value = transfer.get(key)
        if isinstance(value, str) and value:
            allowed.add(value)
    return bool(active_labels) and set(active_labels) <= allowed


def summary_dual_fetch_hidden(summary, now=None):
    """Cảnh báo dual-fetch của tóm tắt này có đang tạm ẩn không."""
    if now is None:
        now = now_epoch()
    transfer = summary.get("transfer") if isinstance(summary, dict) else None
    labels = summary.get("dual_fetch_labels") if isinstance(summary, dict) \
        else None
    if labels is None:
        labels = active_fetch_labels(
            summary.get("fetch_labels"), now) if isinstance(summary, dict) \
            else set()
    return dual_fetch_hidden(transfer, set(labels or []), now)


def label_display_state(fetch_entry):
    """3 trạng thái hiển thị nhãn (S11) từ 1 mục fetch-state.

    Trả về dict: state ("no_data" | "unlabelled" | "labels"), labels
    (list {id, last_seen_at, fetch_count} xếp theo lần thấy cuối),
    unlabelled ({last_seen_at, fetch_count} hoặc None),
    has_unlabelled_field (dòng lượt không nhãn CHỈ hiện khi trường
    unlabelled TỒN TẠI — phân biệt gateway cũ với gateway mới 0 lượt).
    """
    if not isinstance(fetch_entry, dict):
        fetch_entry = {}
    has_labels_field = "fetch_labels" in fetch_entry
    has_unlabelled_field = "unlabelled" in fetch_entry
    labels = []
    raw_labels = fetch_entry.get("fetch_labels")
    if isinstance(raw_labels, dict):
        for label, info in raw_labels.items():
            if not isinstance(label, str) or not label:
                continue
            if not isinstance(info, dict):
                continue
            count = info.get("fetch_count")
            labels.append({
                "id": label,
                "last_seen_at": parse_iso_to_epoch(
                    info.get("last_seen_at")),
                "fetch_count": count if isinstance(count, int)
                and not isinstance(count, bool) else 0,
            })
    labels.sort(key=lambda item: item["last_seen_at"] or 0, reverse=True)
    unlabelled = None
    raw_unlabelled = fetch_entry.get("unlabelled")
    if isinstance(raw_unlabelled, dict):
        count = raw_unlabelled.get("fetch_count")
        unlabelled = {
            "last_seen_at": parse_iso_to_epoch(
                raw_unlabelled.get("last_seen_at")),
            "fetch_count": count if isinstance(count, int)
            and not isinstance(count, bool) else 0,
        }
    if not has_labels_field and not has_unlabelled_field:
        state = "no_data"
    elif not labels:
        state = "unlabelled"
    else:
        state = "labels"
    return {
        "state": state,
        "labels": labels,
        "unlabelled": unlabelled,
        "has_unlabelled_field": has_unlabelled_field,
    }


def fetched_seq_for(fetch_state, bot_id):
    """Seq đã fetch của bot: ưu tiên served_seq (đơn điệu), rồi
    last_fetched_seq; chưa có gì -> 0 (mọi tin đều "mới nhận")."""
    entry = _fetch_entry(fetch_state, bot_id)
    for key in ("served_seq", "last_fetched_seq"):
        value = entry.get(key)
        if isinstance(value, int) and not isinstance(value, bool) \
                and value >= 0:
            return value
    return 0


def has_muse_fetched(fetch_state, bot_id):
    """True nếu gateway đã ghi nhận ÍT NHẤT 1 lần fetch queue thành
    công cho bot này (kể cả đợt rỗng 0 event — trường hợp K3).

    Tín hiệu: ``fetch_count`` >= 1 hoặc ``last_fetch_at`` không rỗng
    — cả 2 đều do ``record_fetch`` của gateway ghi trên MỖI lần phục
    vụ queue thành công, kể cả đợt không có event. CỐ TÌNH không dùng
    served_seq: đợt fetch đầu rỗng không đặt served_seq.
    """
    entry = _fetch_entry(fetch_state, bot_id)
    count = entry.get("fetch_count")
    if isinstance(count, int) and not isinstance(count, bool) \
            and count >= 1:
        return True
    last = entry.get("last_fetch_at")
    return isinstance(last, str) and bool(last.strip())


def is_awaiting_muse_sync(entry, fetch_state, bot_id):
    """Trạng thái "đang chờ đồng bộ về Muse" HIỆU DỤNG của bot.

    Cờ ``awaiting_muse_sync`` trong registry chỉ được ĐẶT lúc tạo bot;
    không đoạn code nào xóa nó (lỗi thật sáng 16/11: nút Bật bị chặn
    vĩnh viễn dù fetch đầu đã thành công). Hướng sửa: UI SUY RA trạng
    thái từ dữ liệu fetch gateway đã ghi, thay vì đọc cờ thô — giữ
    gateway ở ranh giới chỉ-đọc, giữ UI là bên duy nhất ghi registry,
    và tự khỏi cho bot đã fetch trước khi vá (không cần sửa tay
    registry lần nữa). Cờ trong registry chỉ được dọn lười (lazy
    clear) khi bật bot thành công (cùng lần ghi registry đó).
    """
    if not isinstance(entry, dict):
        return False
    if not entry.get("awaiting_muse_sync"):
        return False
    return not has_muse_fetched(fetch_state, bot_id)


# ---------------------------------------------------------------------------
# Trung tâm: trạng thái tài khoản trên tab Trung tâm (v0.2.4, SPEC TT11 /
# DESIGN §9, doc 10 §5.6 bước 6 + §7.2/§7.3). Nguồn dữ liệu đúng 1 nơi
# cho từng mục — không tạo kho mới, không suy từ nguồn khác.
# ---------------------------------------------------------------------------

# "Đang hoạt động" = kéo feed trung tâm trong 15 phút gần nhất (SUY LUẬN,
# doc 10 §7.2). Ngược lại "Chưa thấy kéo" — KHÔNG cảnh báo 7 ngày im lặng
# (Q-AM-08).
CENTER_ACTIVE_SECONDS = 900
# Trạng thái việc "chưa xong" để đếm đang làm / quá hạn (TT5).
CENTER_OPEN_STATUSES = ("moi", "da-nhan", "dang-lam", "da-bao-xong")


def _saigon_timezone():
    """Múi giờ Asia/Saigon: Asia/Ho_Chi_Minh, UTC+7, không DST. Thử tên
    chuẩn trước, rồi về +07:00 cố định (bằng đúng Asia/Ho_Chi_Minh vì
    múi này không có DST)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Ho_Chi_Minh")
    except Exception:
        return timezone(timedelta(hours=7))


def format_day_time_saigon(epoch):
    """Epoch -> "DD/MM HH:MM" giờ Asia/Saigon (DESIGN §9 mục (3))."""
    try:
        return datetime.fromtimestamp(
            epoch, _saigon_timezone()).strftime("%d/%m %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "--/-- --:--"


def format_date_saigon(epoch):
    """Epoch -> "DD/MM/YYYY" giờ Asia/Saigon (ngày cấp mã, doc 10 §5.6
    bước 6)."""
    try:
        return datetime.fromtimestamp(
            epoch, _saigon_timezone()).strftime("%d/%m/%Y")
    except (TypeError, ValueError, OverflowError, OSError):
        return "--/--/----"


def read_center_fetch_state(root):
    """Lần kéo feed trung tâm gần nhất/tài khoản: {account_id: epoch}.

    Đọc mục "center" riêng trong gateway-fetch-state.json (do center
    gateway ghi mỗi lần feed GET 200 đã xác thực — DESIGN §9 CHẶN-2).
    File chưa có mục "center" thì đọc về {} (tương thích ngược). Mốc
    hỏng/không parse được thì bỏ, không crash, không đoán.
    """
    data = read_fetch_state(root)
    center = data.get("center") if isinstance(data, dict) else None
    out = {}
    if not isinstance(center, dict):
        return out
    for account_id, entry in center.items():
        if not isinstance(account_id, str) or not isinstance(entry, dict):
            continue
        epoch = parse_iso_to_epoch(entry.get("last_fetch_at"))
        if epoch is not None:
            out[account_id] = epoch
    return out


def center_fetch_status(last_epoch, now=None):
    """Trạng thái kéo tin từ mốc cuối: (đang_hoạt_động, nhãn hiển thị).

    Nhãn "Đang hoạt động" khi kéo trong 15 phút gần nhất, ngược lại
    "Chưa thấy kéo" — kể cả chưa từng kéo (None). Không ghi "offline"
    chắc chắn (Q-AM-08: chỉ "chưa thấy kéo" sau 15 phút).
    """
    if now is None:
        now = now_epoch()
    if last_epoch is not None \
            and now - last_epoch <= CENTER_ACTIVE_SECONDS:
        return True, "Đang hoạt động"
    return False, "Chưa thấy kéo"


def center_secret_status(root, account_id):
    """Mã bí mật trung tâm: {"has": bool, "issued_at": epoch|None}.

    "has" = đủ cặp fetch+push (file tồn tại, dòng đầu không rỗng).
    "issued_at" = mtime file fetch_secret (DESIGN §9). Ngày cấp suy từ
    mtime chỉ là ƯỚC LƯỢNG — nguồn chính xác là dòng xoay mã trong
    audit.jsonl; UI ghi chú điều này, không tin mtime khi lệch audit.
    Chỉ đọc metadata (tồn tại + mtime) — KHÔNG đọc giá trị secret.
    """
    secrets_dir = Path(root) / "secrets"
    has = True
    for kind in ("fetch", "push"):
        path = secrets_dir / ("center-%s.%s_secret" % (account_id, kind))
        try:
            first = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            has = False
            break
        if not first or not first[0].strip():
            has = False
            break
    issued_at = None
    try:
        issued_at = (secrets_dir / ("center-%s.fetch_secret" % account_id)
                     ).stat().st_mtime
    except OSError:
        issued_at = None
    return {"has": has, "issued_at": issued_at}


# ---------------------------------------------------------------------------
# Trạng thái xem mã bí mật trung tâm (v0.6.0, T3j — ngoại lệ F18(3),
# DESIGN-TRUNG-TAM.md §3.11 bước 5, SPEC TT23/TS3).
#
# File ``ui-config/center_view_state.json`` (600, KHÔNG vào backup):
# cờ "đã xem" theo tài khoản (răng duy nhất của quy tắc "mỗi mã chỉ
# hiện tối đa 1 lần", Q-V060-17) + bộ đếm nhập sai mật khẩu xem mã +
# khóa tạm. File KHÔNG chứa giá trị mã, mật khẩu, hay hash.
# ---------------------------------------------------------------------------

# Tự ẩn mã sau 30 giây (Q-V060-18); 5 lần sai/15 phút -> khóa 900 giây
# (Q-V060-20); quá 24 giờ chưa xoay mã sau khi xem -> cảnh báo mức
# nghiêm trọng + khóa lượt xem mới (Q-V060-26).
CENTER_VIEW_AUTO_HIDE_SECONDS = 30
CENTER_VIEW_MAX_FAILURES = 5
CENTER_VIEW_FAILURE_WINDOW_SECONDS = 15 * 60
CENTER_VIEW_LOCK_SECONDS = 900
CENTER_VIEW_ROTATION_DEADLINE_SECONDS = 24 * 3600
CENTER_VIEW_STATE_FILENAME = "center_view_state.json"
# Dấu khôi phục toàn bộ ui-config/ từ backup (C-25): file
# ``ui-config/center_view_restored.json`` do quy trình khôi phục tạo
# ({"restored_at": <epoch>}); chừng nào mã của tài khoản còn cũ hơn
# mốc này thì từ chối xem cho tới khi xoay mã (audit sau khôi phục
# có thể đã lùi về trước lần xem nên không dùng audit làm bằng chứng
# "chưa từng xem"). Xem read_center_view_state + ui_app.
CENTER_VIEW_RESTORED_FILENAME = "center_view_restored.json"


def new_center_view_state():
    """File trạng thái xem mã rỗng (đúng schema DESIGN §3.11 bước 5)."""
    return {"accounts": {}, "failed_attempts": {}, "locked_until": None}


def read_center_view_state(root):
    """Đọc file trạng thái xem mã -> (state, status).

    status: "ok" (đọc được, đã chuẩn hóa khóa thiếu/kiểu lạ về mặc
    định từng khóa), "missing" (file chưa từng tồn tại — trường hợp
    cài mới/nâng cấp từ bản chưa có tính năng xem mã), "corrupt"
    (file hỏng — JSON sai hoặc không phải dict). "missing"/"corrupt"
    KHÔNG tự về mặc định mở ở tầng này: tầng app quyết định
    fail-closed hay cho xem lần đầu theo audit (DESIGN §3.11 bước 5).
    """
    path = Path(root) / "ui-config" / CENTER_VIEW_STATE_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, "missing"
    try:
        data = json.loads(text)
    except ValueError:
        return None, "corrupt"
    if not isinstance(data, dict):
        return None, "corrupt"
    state = new_center_view_state()
    accounts = data.get("accounts")
    if isinstance(accounts, dict):
        for account_id, entry in accounts.items():
            if not isinstance(account_id, str) or not isinstance(
                    entry, dict):
                continue
            viewed_at = entry.get("viewed_at")
            state["accounts"][account_id] = {
                "viewed_at": viewed_at if isinstance(
                    viewed_at, (int, float)) else None,
                "rotated_after_view": bool(
                    entry.get("rotated_after_view")),
            }
    attempts = data.get("failed_attempts")
    if isinstance(attempts, dict):
        for ip, stamps in attempts.items():
            if not isinstance(ip, str) or not isinstance(stamps, list):
                continue
            kept = [s for s in stamps
                    if isinstance(s, (int, float))]
            if kept:
                state["failed_attempts"][ip] = kept
    locked_until = data.get("locked_until")
    if isinstance(locked_until, (int, float)):
        state["locked_until"] = locked_until
    return state, "ok"


@contextlib.contextmanager
def _center_view_state_lock(root):
    """Khóa flock riêng cho chu trình đọc-sửa-ghi file trạng thái
    xem mã (cùng mẫu _accounts_lock)."""
    lock_path = Path(root) / "ui-config" / "center_view_state.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_center_view_state(root, state):
    """Ghi file trạng thái xem mã nguyên tử (tmp+rename), quyền 600."""
    write_json_file(
        Path(root) / "ui-config" / CENTER_VIEW_STATE_FILENAME,
        state, mode=0o600)


def modify_center_view_state(root, mutate_fn):
    """Đọc + sửa + ghi file trạng thái xem mã trong cùng 1 khóa
    flock (chống race 2 tab cùng xem). File thiếu/hỏng thì bắt đầu
    từ trạng thái rỗng — tầng app đã fail-closed ở lượt xem trước
    đó, còn xoay mã (đường phục hồi) phải ghi được file mới.
    Trả về giá trị của mutate_fn."""
    path = Path(root) / "ui-config" / CENTER_VIEW_STATE_FILENAME
    with _center_view_state_lock(root):
        state, _status = read_center_view_state(root)
        if state is None:
            state = new_center_view_state()
        result = mutate_fn(state)
        write_json_file(path, state, mode=0o600)
    return result


def mark_center_view_rotated(root, account_id):
    """Xoay mã xong: gỡ cờ nhắc "đã xem — chưa xoay" của tài khoản.

    Chỉ khi tài khoản đang có lượt xem chờ xoay (viewed_at đã đặt)
    thì đặt viewed_at=None + rotated_after_view=True và trả True
    (để audit center_secret_rotate ghi thêm trường đó — SPEC TS3).
    Xoay mà không qua xem thì không đổi gì, trả False."""
    def _mark(state):
        entry = state["accounts"].get(account_id)
        if not entry or entry.get("viewed_at") is None:
            return False
        entry["viewed_at"] = None
        entry["rotated_after_view"] = True
        return True

    return modify_center_view_state(root, _mark)


def clear_center_view_entry(root, account_id):
    """Xóa mục trạng thái xem mã của tài khoản (khi gỡ hẳn — tài
    khoản không còn, không còn lượt xem chờ xoay nào)."""
    def _clear(state):
        state["accounts"].pop(account_id, None)

    modify_center_view_state(root, _clear)


def center_view_restored_at(root):
    """Mốc khôi phục toàn bộ ui-config/ từ backup (C-25) — epoch,
    hoặc None khi không có dấu khôi phục / dấu hỏng."""
    data = read_json_file(
        Path(root) / "ui-config" / CENTER_VIEW_RESTORED_FILENAME)
    if not isinstance(data, dict):
        return None
    restored_at = data.get("restored_at")
    if isinstance(restored_at, (int, float)):
        return restored_at
    return None


def audit_has_center_view(root, account_id):
    """True khi audit.jsonl có ít nhất 1 dòng center_secret_view
    của tài khoản (dùng phân biệt "chưa từng xem" với "file trạng
    thái mất" khi file chưa từng tồn tại — DESIGN §3.11 bước 5).
    Audit là best-effort nên chỉ dùng cho trường hợp nâng cấp thông
    thường, KHÔNG dùng sau khôi phục backup (C-25)."""
    for row in read_jsonl_rows(
            Path(root) / "ui-config" / "audit.jsonl"):
        if row.get("action") == "center_secret_view" \
                and row.get("account_id") == account_id:
            return True
    return False


def center_view_pending_items(root, now=None):
    """Các tài khoản đang có lượt xem chờ xoay mã: list
    {account_id, display_name, viewed_at, overdue_seconds} (sắp xếp
    theo viewed_at tăng dần). overdue_seconds = số giây đã chờ xoay
    (âm khi chưa tới hạn — để tầng hiển thị tự quyết có quá hạn
    24 giờ hay không). Lỗi đọc -> []."""
    if now is None:
        now = time.time()
    try:
        state, status = read_center_view_state(root)
    except OSError:
        return []
    if state is None:
        return []
    accounts = read_accounts(root).get("accounts") or {}
    out = []
    for account_id, entry in state["accounts"].items():
        viewed_at = entry.get("viewed_at")
        if viewed_at is None:
            continue
        record = accounts.get(account_id) or {}
        out.append({
            "account_id": account_id,
            "display_name": record.get("display_name") or account_id,
            "viewed_at": viewed_at,
            "overdue_seconds": now - viewed_at,
        })
    out.sort(key=lambda item: item["viewed_at"])
    return out


def append_audit_line(root, client_ip, action, bot_id, before, after,
                      result, task_id=None, account_id=None):
    """Ghi 1 dòng audit append-only vào ui-config/audit.jsonl (600).

    Best-effort: lỗi ghi không chặn thao tác. Không secret, không
    nội dung tin. ``task_id`` chỉ dùng cho dòng trung tâm (TS3);
    ``account_id`` chỉ dùng cho dòng quản lý tài khoản / cấp-xoay/
    xem secret trung tâm — khóa chỉ thêm khi có giá trị để dòng cũ
    không đổi. (Tách từ UiApp._audit để CLI đặt mật khẩu xem mã
    cũng ghi được cùng 1 định dạng.)"""
    try:
        audit_dir = Path(root) / "ui-config"
        audit_dir.mkdir(parents=True, exist_ok=True)
        path = audit_dir / "audit.jsonl"
        entry = {
            "ts": now_epoch(),
            "ip": client_ip,
            "action": action,
            "bot_id": bot_id,
            "task_id": task_id,
            "before": before,
            "after": after,
            "result": result,
        }
        if account_id is not None:
            entry["account_id"] = account_id
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                     0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def count_center_tasks_by_account(tasks, now=None):
    """Đếm việc/tài khoản từ danh sách việc (tasks_state.json qua
    CenterStore.list_tasks): {account_id: {"open": n, "overdue": n}}.

    "open" = trạng thái chưa xong (moi/da-nhan/dang-lam/da-bao-xong);
    "overdue" = open và now > due_at (TT5). Đếm trên hub, không đếm
    bản local phía Muse.
    """
    if now is None:
        now = now_epoch()
    counts = {}
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        assigned = task.get("assigned_to")
        if isinstance(assigned, str):
            assigned = [assigned] if assigned else []
        if not isinstance(assigned, list):
            continue
        for account_id in assigned:
            if not isinstance(account_id, str) or not account_id:
                continue
            entry = counts.setdefault(
                account_id, {"open": 0, "overdue": 0})
            if task.get("status") in CENTER_OPEN_STATUSES:
                entry["open"] += 1
                due = parse_iso_to_epoch(task.get("due_at"))
                if due is not None and now > due:
                    entry["overdue"] += 1
    return counts


def is_center_orphan(task, accounts):
    """Việc mồ côi (Q-AM-06): KHÔNG CÒN ai trong danh sách
    ``assigned_to`` đang tham gia — tất cả đều tạm ngưng/gỡ (hoặc
    không còn trong danh sách tài khoản), hoặc danh sách rỗng
    (Q-V040-2 gỡ hết người làm việc). Việc còn ít nhất 1 người đang
    tham gia không phải mồ côi. Chỉ là nhãn hiển thị — KHÔNG tự
    chuyển việc. ``accounts``: dict {"accounts": {...}}
    (read_accounts) hoặc dict phẳng {account_id: record}.
    """
    if not isinstance(task, dict):
        return False
    assigned = task.get("assigned_to")
    if isinstance(assigned, str):
        assigned = [assigned] if assigned else []
    if not isinstance(assigned, list) or not assigned:
        return True
    account_map = accounts.get("accounts") if isinstance(accounts, dict) \
        else None
    if not isinstance(account_map, dict):
        account_map = accounts if isinstance(accounts, dict) else {}
    for account_id in assigned:
        record = account_map.get(account_id)
        if isinstance(record, dict) \
                and record.get("center_participating") is True:
            return False
    return True


def read_capability(root, bot_id):
    """Đọc data/<bot_id>/capability.json (ảnh chụp getMe); -> {}."""
    data = read_json_file(Path(root) / "data" / bot_id / "capability.json")
    return data if isinstance(data, dict) else {}


def capability_bot_id(capability):
    """Trích id bot từ ảnh chụp getMe (Zalo/Telegram đều có "id" ở
    cấp response hoặc trong "result"). Không có -> None."""
    if not isinstance(capability, dict):
        return None
    for candidate in (capability, capability.get("result")):
        if isinstance(candidate, dict):
            value = candidate.get("id")
            if value is not None:
                return str(value)
    return None


def list_bots(root):
    """Danh sách bot_id trong registry, sắp xếp theo tên."""
    return sorted(load_registry(root)["bots"].keys())


def sync_status(root, bot_id, now=None):
    """Trạng thái đồng bộ trả lời của 1 bot (04 §4 + 05 §4).

    Trả về dict: mode ("g3b"/"g3a"), line (câu hiển thị), replies_count,
    last_sent_at (epoch), file_mtime, age_seconds, fallback (True khi
    GĐ3a do đồng bộ chậm >30 phút — cần biểu ngữ).
    """
    if now is None:
        now = now_epoch()
    path = Path(root) / "data" / bot_id / "replies.jsonl"
    rows = read_jsonl_rows(path)
    count = len(rows)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    last_sent_at = None
    for row in reversed(rows):
        parsed = parse_iso_to_epoch(row.get("sent_at"))
        if parsed is not None:
            last_sent_at = parsed
            break
    if count == 0:
        if mtime is not None:
            line = "Chưa đồng bộ được — đang chờ trả lời đầu tiên"
        else:
            line = "Chưa có dữ liệu trả lời"
        return {
            "mode": "g3a", "line": line, "replies_count": 0,
            "last_sent_at": None, "file_mtime": mtime,
            "age_seconds": None, "fallback": False,
        }
    refs = [value for value in (mtime, last_sent_at) if value is not None]
    ref = max(refs) if refs else None
    age = (now - ref) if ref is not None else None
    if age is not None and age > SYNC_STALE_AFTER_SECONDS:
        return {
            "mode": "g3a",
            "line": "Đồng bộ trả lời chậm hơn 30 phút — đang hiện giao "
                    "diện rút gọn (chỉ tin đến, không hiện trả lời cũ "
                    "như mới)",
            "replies_count": count, "last_sent_at": last_sent_at,
            "file_mtime": mtime, "age_seconds": age, "fallback": True,
        }
    minutes = int(age // 60) if age is not None else 0
    if minutes <= 0:
        late = "chậm dưới 1 phút"
    elif minutes == 1:
        late = "chậm 1 phút"
    else:
        late = "chậm %d phút" % minutes
    when = format_hms(last_sent_at) if last_sent_at is not None else "--:--:--"
    return {
        "mode": "g3b",
        "line": "Trả lời đồng bộ tới %s (%s)" % (when, late),
        "replies_count": count, "last_sent_at": last_sent_at,
        "file_mtime": mtime, "age_seconds": age, "fallback": False,
    }


def format_age_label(minutes):
    """L8: nhãn tuổi tin theo ngưỡng — "Tồn N phút" / "Tồn N giờ
    M phút" / "Tồn N ngày M giờ". Phút lẻ sau ngày và phút lẻ sau
    giờ tròn đều bỏ (vd 2236 phút -> "Tồn 1 ngày 13 giờ", 60 phút
    -> "Tồn 1 giờ", 1440 phút -> "Tồn 1 ngày")."""
    minutes = int(minutes)
    if minutes >= 1440:
        days, rem = divmod(minutes, 1440)
        hours = rem // 60
        if hours:
            return "Tồn %d ngày %d giờ" % (days, hours)
        return "Tồn %d ngày" % days
    if minutes >= 60:
        hours, mins = divmod(minutes, 60)
        if mins:
            return "Tồn %d giờ %d phút" % (hours, mins)
        return "Tồn %d giờ" % hours
    return "Tồn %d phút" % minutes


def incoming_status(seq, fetched_seq, received_epoch, now=None):
    """Trạng thái GĐ3a của 1 tin đến: đúng 3 trạng thái (05 §4).

    Trả về (fetch_label, age_label_or_None). age_label là nhãn tuổi
    theo ngưỡng của ``format_age_label`` (L8) khi tuổi tin >= 1 phút
    — chỉ ghi tuổi, không cam kết.
    """
    if now is None:
        now = now_epoch()
    label = STATUS_FETCHED if seq <= fetched_seq else STATUS_NEW
    age_label = None
    if received_epoch is not None:
        minutes = int((now - received_epoch) // 60)
        if minutes >= 1:
            age_label = format_age_label(minutes)
    return label, age_label


def _record_text(record):
    """Text hiển thị của 1 bản ghi chuẩn: text, rồi caption, rồi
    file_hint, rồi chuỗi giữ chỗ cho tin không nội dung."""
    if not isinstance(record, dict):
        return ""
    text = record.get("text")
    if isinstance(text, str) and text:
        return text
    caption = record.get("caption")
    if isinstance(caption, str) and caption:
        return caption
    hint = record.get("file_hint")
    if isinstance(hint, str) and hint:
        return hint
    return "[tin không có nội dung]"


def _incoming_item(bot_id, row, fetched_seq, now, platform=""):
    record = row.get("record") if isinstance(row, dict) else None
    if not isinstance(record, dict):
        record = {}
    received = parse_iso_to_epoch(row.get("received_at"))
    if received is None:
        received = parse_iso_to_epoch(record.get("received_at"))
    seq = row.get("seq")
    if not isinstance(seq, int):
        seq = 0
    fetch_label, age_label = incoming_status(seq, fetched_seq, received, now)
    event_id = row.get("event_id") or record.get("event_id") or ""
    attention = False
    if fetch_label == STATUS_NEW and received is not None:
        attention = (now - received) > ATTENTION_MINUTES * 60
    return {
        "kind": "in",
        "ts": received if received is not None else 0,
        "ts_iso": row.get("received_at") or record.get("received_at") or "",
        "bot_id": bot_id,
        "platform": platform,
        "chat_id": str(record.get("chat_id") or ""),
        "chat_title": record.get("chat_title"),
        "sender_id": str(record.get("sender_id") or ""),
        "sender_name": record.get("sender_name"),
        "text": _record_text(record),
        "caption": record.get("caption"),
        "event_id": str(event_id),
        "event_key": "%s__%s" % (bot_id, event_id) if event_id else None,
        "reply_key": None,
        "seq": seq,
        "fetch_label": fetch_label,
        "age_label": age_label,
        "status": fetch_label,
        "paired": False,
        "in_reply_to_event_key": None,
        "error_class": None,
        "message_id": record.get("message_id"),
        "attention": attention,
    }


def _reply_item(bot_id, row, platform=""):
    sent = parse_iso_to_epoch(row.get("sent_at"))
    status = row.get("status")
    label = {"sent": "Đã gửi", "failed": "Gửi lỗi",
             "dead": "Thư chết"}.get(status, "Đã gửi")
    return {
        "kind": "out",
        "ts": sent if sent is not None else 0,
        "ts_iso": row.get("sent_at") or "",
        "bot_id": bot_id,
        "platform": platform,
        "chat_id": str(row.get("chat_id") or ""),
        "chat_title": row.get("chat_title"),
        "sender_id": "",
        "sender_name": None,
        "text": row.get("text_full") or row.get("caption") or "",
        "caption": row.get("caption"),
        "event_id": "",
        "event_key": None,
        "reply_key": row.get("reply_key"),
        "seq": None,
        "fetch_label": None,
        "age_label": None,
        "status": label,
        "paired": False,
        "in_reply_to_event_key": row.get("in_reply_to_event_key"),
        "error_class": row.get("error_class"),
        "message_id": row.get("message_id"),
        "attention": status == "dead",
        # retry_of (NS-02, T3m): reply_key thu chet goc khi day la
        # lan gui lai do chu nhan bam tay; None voi tin thuong.
        # retry_count: so lan Sender tu thu lai (toi da 6).
        "retry_of": row.get("retry_of"),
        "retry_count": row.get("retry_count"),
    }


# Cau tieng Viet than thien cho ly do thu chet (NS-02, T3m) — khong
# lo ma loi kho (RETRYABLE_NETWORK, ...) ra giao dien.
DEAD_ERROR_TEXT = {
    "RETRYABLE_NETWORK": "Lỗi mạng — đã thử lại nhiều lần vẫn không gửi được.",
    "TOKEN_INVALID": "Mã bot không còn hiệu lực — cần cấp lại mã mới.",
    "RATE_LIMITED": "Bị giới hạn gửi (quá nhiều tin trong thời gian ngắn).",
    "CHAT_GONE": "Chat không còn tồn tại hoặc bot đã bị gỡ khỏi chat.",
    "PERMANENT": "Nền tảng từ chối tin này.",
    "UNKNOWN": "Lỗi không rõ nguyên nhân.",
}


def dead_error_text(error_class):
    """Cau tieng Viet than thien cho error_class cua thu chet."""
    if isinstance(error_class, str):
        text = DEAD_ERROR_TEXT.get(error_class)
        if text:
            return text
    return "Lỗi không rõ nguyên nhân."


def dead_letters(root, bot_id, now=None, page=1, per_page=20):
    """Danh sach thu chet dang mo (NS-02, T3m), moi nhat truoc.

    Doc thang ``replies.jsonl`` (chi file tra loi) roi dung
    ``_reply_item`` — KHONG goi ``merged_history``: muc Thu chet
    la muc thuong (khong phai node) nen hien tren moi lan render
    trang chi tiet, va luat node dong bo qua lenh doc (DESIGN
    §8.1) van giu nguyen. Chi lay tra loi khi dong bo che do g3b
    (nhu merged_history; g3a khong co file tra loi).
    Gom chuoi thu lai theo ``retry_of`` (moi lan thu deu tro ve
    reply_key GOC): chi hien "dau chuoi" (tip) con ``dead`` — dong
    goc da co lan thu moi hon thi an; thu da co lan thu thanh cong
    (status ``sent``) thi bien khoi danh sach. Moi tip mang them
    ``attempts`` (so lan da bam gui lai trong chuoi) va ``root_key``
    (reply_key thu goc). Phan trang tay: mac dinh 20/trang, toi da
    200/trang. Tra ve (tips, total, page, pages)."""
    if now is None:
        now = now_epoch()
    page = max(1, int(page))
    per_page = min(max(1, int(per_page)), 200)
    reply_rows = read_jsonl_rows(
        Path(root) / "data" / bot_id / "replies.jsonl")
    if sync_status(root, bot_id, now)["mode"] != "g3b":
        return [], 0, 1, 1
    platform = read_bot_config(root, bot_id).get("platform") or ""
    dead_items = [
        _reply_item(bot_id, row, platform) for row in reply_rows
        if isinstance(row, dict) and row.get("status") == "dead"
    ]
    dead_items.sort(key=lambda item: item["ts"], reverse=True)
    retries_by_root = {}
    sent_roots = set()
    for row in reply_rows:
        if not isinstance(row, dict):
            continue
        retry_of = row.get("retry_of")
        if not isinstance(retry_of, str) or not retry_of:
            continue
        retries_by_root.setdefault(retry_of, []).append(row)
        if row.get("status") == "sent":
            sent_roots.add(retry_of)
    tips = []
    for item in dead_items:
        key = item.get("reply_key")
        if not isinstance(key, str) or not key:
            continue
        root_key = item.get("retry_of") or key
        if root_key in sent_roots:
            continue
        chain = retries_by_root.get(root_key, [])
        if chain:
            # Chi giu dong chet moi nhat cua chuoi; dong goc va cac
            # lan thu cu hon da bi lan thu moi hon thay the.
            last_dead = None
            for row in chain:
                if row.get("status") == "dead":
                    last_dead = row
            if last_dead is None:
                continue
            if key != last_dead.get("reply_key"):
                continue
        item = dict(item)
        item["attempts"] = len(chain)
        item["root_key"] = root_key
        tips.append(item)
    total = len(tips)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    return tips[start:start + per_page], total, page, pages


def merged_history(root, bot_id, now=None, page=1, per_page=HISTORY_PER_PAGE,
                   chat_filter=None, status_filter=None):
    """Lịch sử hợp nhất của 1 bot, mới nhất trước, phân trang.

    GĐ3b: tin đến + trả lời, ghép cặp qua in_reply_to_event_key; tin
    đến chưa thấy trả lời tương ứng mang chip CHIP_NO_REPLY_SEEN.
    GĐ3a: chỉ tin đến + 3 trạng thái, kèm cờ g3a_notice.
    Lọc: chat_filter (chat_id đúng), status_filter ("new"/"fetched"
    cho tin đến, "sent"/"failed"/"dead" cho trả lời).
    Trả về (items, total, page, pages, sync).
    """
    if now is None:
        now = now_epoch()
    page = max(1, int(page))
    per_page = max(1, min(int(per_page), 200))
    sync = sync_status(root, bot_id, now)
    fetched = fetched_seq_for(read_fetch_state(root), bot_id)
    platform = read_bot_config(root, bot_id).get("platform") or ""
    queue_rows = read_jsonl_rows(Path(root) / "data" / bot_id / "queue.jsonl")
    items = [_incoming_item(bot_id, row, fetched, now, platform)
             for row in queue_rows]
    if sync["mode"] == "g3b":
        reply_rows = read_jsonl_rows(
            Path(root) / "data" / bot_id / "replies.jsonl")
        replies = [_reply_item(bot_id, row, platform) for row in reply_rows]
        by_event_key = {}
        for item in items:
            if item["event_key"]:
                by_event_key[item["event_key"]] = item
        for reply in replies:
            target = by_event_key.get(reply["in_reply_to_event_key"])
            if target is not None:
                target["paired"] = True
                reply["paired"] = True
                if not reply["chat_title"]:
                    reply["chat_title"] = target["chat_title"]
        items.extend(replies)
    items.sort(key=lambda item: item["ts"], reverse=True)
    if chat_filter:
        items = [i for i in items if i["chat_id"] == chat_filter]
    if status_filter:
        def _match(item):
            if item["kind"] == "in":
                want = {"new": STATUS_NEW, "fetched": STATUS_FETCHED}.get(
                    status_filter)
                return want is not None and item["fetch_label"] == want
            return {"sent": "Đã gửi", "failed": "Gửi lỗi",
                    "dead": "Thư chết"}.get(status_filter) == item["status"]
        items = [i for i in items if _match(i)]
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    return items[start:start + per_page], total, page, pages, sync


CENTER_BOT_ID = "center"


def _center_incoming(root):
    """Tin den qua trung tam tu ``data/center/events.jsonl``.

    Quy tac dem #11 dot A (21/11/2026) — tin trung tam tinh CHUNG
    vao "hom nay", khong hien rieng mot so:
    - chi row ``kind == "message"``: su kien he thong
      (task_created/task_status/task_reassigned) khong phai tin nhan.
    - chi tin cua tai khoan khac: bo tin cua ``owner`` (tin cua chu
      nhan = chieu di, nhu replies) va tin hub tu sinh
      (``source == "hub"``).
    - moi record kind (hoi/bao-cao/giao-viec/khac) deu tinh: tat ca
      deu hien bong bong o tab Chat; bo bao-cao se lech lai voi tab
      Chat (dung loi dang sua).
    - moc thoi gian: truong ``at`` cua row, du phong
      ``record.created_at`` (2 truong cung 1 moc khi ghi).
    - trung ``msg_id`` chi tinh 1 lan.
    - khong the dem trung voi queue bot: 2 kenh ghi boi 2 dau ghi
      khac nhau, khong gian id rieng (msg_id trung tam / event_id
      nen tang); item trung tam mang ``event_key`` tien to
      ``center__``.

    Tra list dict {ts, ts_iso, seq, msg_id, room, sender, body} da
    khu trung, xep theo seq tang dan. File thieu/hong -> []. Tin
    khong co moc thoi gian parse duoc thi ts=0 (hien cuoi feed,
    khong vao so ngay/tuan)."""
    rows = read_jsonl_rows(Path(root) / "data" / "center" / "events.jsonl")
    seen = set()
    out = []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != "message":
            continue
        record = row.get("record")
        if not isinstance(record, dict):
            continue
        sender = record.get("from_account") or ""
        if sender == "owner" or record.get("source") == "hub":
            continue
        msg_id = row.get("msg_id") or record.get("msg_id") or ""
        if msg_id:
            if msg_id in seen:
                continue
            seen.add(msg_id)
        ts = parse_iso_to_epoch(row.get("at"))
        if ts is None:
            ts = parse_iso_to_epoch(record.get("created_at"))
        seq = row.get("seq")
        out.append({
            "ts": ts if ts is not None else 0,
            "ts_iso": row.get("at") or record.get("created_at") or "",
            "seq": seq if isinstance(seq, int) else None,
            "msg_id": str(msg_id),
            "room": record.get("room") or "chung",
            "sender": sender,
            "body": record.get("body") or "",
        })
    return out


def _center_item(item):
    """1 tin trung tam thanh dong feed (cung hinh dang
    ``_incoming_item`` de renderer dung chung)."""
    msg_id = item["msg_id"]
    return {
        "kind": "in",
        "ts": item["ts"],
        "ts_iso": item["ts_iso"],
        "bot_id": CENTER_BOT_ID,
        "platform": CENTER_BOT_ID,
        "chat_id": item["room"],
        "chat_title": "Trung tâm · %s" % item["room"],
        "sender_id": item["sender"],
        "sender_name": item["sender"] or None,
        "text": item["body"],
        "caption": None,
        "event_id": msg_id,
        "event_key": ("center__%s" % msg_id) if msg_id else None,
        "reply_key": None,
        "seq": item["seq"],
        "fetch_label": None,
        "age_label": None,
        "status": "Trung tâm",
        "paired": False,
        "in_reply_to_event_key": None,
        "error_class": None,
        "message_id": msg_id,
        "attention": False,
    }


def activity_feed(root, bot_ids, now=None, limit=ACTIVITY_LIMIT,
                  platforms=None, direction=None, attention_only=False,
                  query=None):
    """Hoạt động mới nhất toàn hệ: hợp nhất các bot, mới nhất trước.

    Mỗi bot đóng góp theo chế độ của nó (GĐ3a: chỉ tin đến; GĐ3b:
    cả 2 chiều). Tin đến mang trạng thái GĐ3a; trả lời mang trạng
    thái gửi của nó.

    (#11 dot A) Them tin den qua trung tam (``_center_incoming``):
    tin trung tam tinh CHUNG vao feed, khong tach rieng. Chi them
    khi khong loc nen tang, hoac loc co "center" — loc platforms
    rieng (vd {"zalo"}) thi khong keo tin trung tam vao.

    Lọc (V2 §4): ``platforms`` (tập nền tảng), ``direction``
    ("in"/"out"), ``attention_only`` (chỉ tin "cần chú ý"). ``query``
    (chuỗi tìm kiếm) được áp SAU khi đã cắt cửa sổ ``limit`` dòng —
    tức chỉ tìm trong danh sách đang hiện, KHÔNG tìm toàn văn lịch
    sử. Chuỗi tìm so khớp không phân biệt hoa/thường trên nội dung
    tin + tên chat + tên bot.
    """
    if now is None:
        now = now_epoch()
    fetch_state = read_fetch_state(root)
    all_items = []
    for bot_id in bot_ids:
        platform = read_bot_config(root, bot_id).get("platform") or ""
        if platforms is not None and platform not in platforms:
            continue
        fetched = fetched_seq_for(fetch_state, bot_id)
        queue_rows = read_jsonl_rows(
            Path(root) / "data" / bot_id / "queue.jsonl")
        items = [_incoming_item(bot_id, row, fetched, now, platform)
                 for row in queue_rows]
        sync = sync_status(root, bot_id, now)
        if sync["mode"] == "g3b":
            reply_rows = read_jsonl_rows(
                Path(root) / "data" / bot_id / "replies.jsonl")
            items.extend(_reply_item(bot_id, row, platform)
                         for row in reply_rows)
        all_items.extend(items)
    if platforms is None or CENTER_BOT_ID in platforms:
        all_items.extend(_center_item(item)
                         for item in _center_incoming(root))
    if direction in ("in", "out"):
        all_items = [i for i in all_items if i["kind"] == direction]
    if attention_only:
        all_items = [i for i in all_items if i.get("attention")]
    all_items.sort(key=lambda item: item["ts"], reverse=True)
    window = all_items[:limit]
    if query:
        needle = query.strip().lower()
        if needle:
            window = [i for i in window
                      if needle in (i.get("text") or "").lower()
                      or needle in (i.get("chat_title") or "").lower()
                      or needle in (i.get("bot_id") or "").lower()]
    return window


def compute_alerts(summary, now=None):
    """Cảnh báo từ tín hiệu phía PC của 1 bot (05 §5).

    Trả về list {key, level, text}. Chỉ tín hiệu PC — không huy hiệu
    cho nghẽn lane/kẹt claim phía Muse.
    """
    if now is None:
        now = now_epoch()
    alerts = []
    enabled = bool(summary.get("enabled"))
    state_age = summary.get("state_age_seconds")
    if enabled:
        if summary.get("token_state") == "WAIT_TOKEN" or summary.get("flag_401"):
            alerts.append({
                "key": "token", "level": "red",
                "text": "Token có vấn đề — bot đang chờ token mới",
            })
        if state_age is not None and state_age > DEAD_AFTER_SECONDS:
            alerts.append({
                "key": "dead", "level": "red",
                "text": "Bot không lấy được tin hơn 15 phút",
            })
        elif state_age is not None and state_age > STALE_AFTER_SECONDS:
            alerts.append({
                "key": "stale", "level": "yellow",
                "text": "Chưa lấy được tin mới hơn 5 phút",
            })
        if summary.get("unit_state") == "inactive":
            alerts.append({
                "key": "unit", "level": "red",
                "text": "Tiến trình tai không chạy",
            })
        if summary.get("flag_429"):
            alerts.append({
                "key": "rate", "level": "yellow",
                "text": "Đang bị giới hạn tốc độ (429) — chờ backoff",
            })
    backlog = summary.get("backlog_estimate") or 0
    if backlog >= BACKLOG_ALERT_THRESHOLD:
        alerts.append({
            "key": "backlog", "level": "yellow",
            "text": "Tồn ước lượng %d tin chưa tới Muse" % backlog,
        })
    sync = summary.get("sync") or {}
    if sync.get("fallback"):
        alerts.append({
            "key": "sync", "level": "yellow",
            "text": "Đồng bộ trả lời chậm hơn 30 phút — đang hiện giao "
                    "diện rút gọn",
        })
    if summary.get("token_pending_verification"):
        alerts.append({
            "key": "token-pending", "level": "yellow",
            "text": "Đang chờ xác minh token 2 đầu (PC xong, Muse chưa)",
        })
    if summary.get("awaiting_muse_sync"):
        alerts.append({
            "key": "awaiting-sync", "level": "yellow",
            "text": "Chờ đồng bộ về Muse — chưa thấy lần lấy tin đầu tiên",
        })
    # Trùng lấy (doc 09 §5.3, F27): nhánh NGOÀI khối enabled (S17) —
    # bot tắt + 2 fetcher vẫn nổ. Điều kiện chỉ dựa vào nhãn lấy tin,
    # độc lập owner_account; lượt không nhãn không tham gia. Tạm ẩn
    # theo luồng chuyển KHÔNG xử lý ở đây (R4: chỉ tầng hiển thị ẩn).
    active_labels = active_fetch_labels(summary.get("fetch_labels"), now)
    if len(active_labels) >= 2:
        alerts.append({
            "key": "dual-fetch", "level": "red",
            "text": dual_fetch_alert_text(sorted(active_labels)),
        })
    return alerts


def _local_day_start(epoch, days_back=0):
    """Mốc 00:00 giờ địa phương của PC, lùi ``days_back`` ngày."""
    day = datetime.fromtimestamp(epoch).astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0)
    return (day - timedelta(days=days_back)).timestamp()


def _local_month_start(epoch):
    """Mốc 00:00 ngày 1 của tháng hiện tại (giờ địa phương của PC)."""
    return datetime.fromtimestamp(epoch).astimezone().replace(
        day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def _counters_from_rows(queue_rows, reply_rows, fetched_seq, now):
    """Đếm từ 2 file của 1 bot (đọc 1 lần, dùng cho thẻ + dải số).

    - today_in / week_in: tin đến trong ngày / 7 ngày (giờ địa phương).
    - month_received: tin đến trong tháng dương lịch mà Muse đã lấy
      (seq <= fetched_seq — khớp định nghĩa đếm "received" phía
      Muse; tin chưa lấy không tính).
    - month_sent / sent_total / dead_total: trả lời theo trạng thái
      (chỉ "sent" tính vào quota — khớp đếm "sent" phía Muse).
    """
    today_start = _local_day_start(now)
    week_start = _local_day_start(now, 6)
    month_start = _local_month_start(now)
    counters = {
        "today_in": 0, "week_in": 0, "month_received": 0,
        "month_sent": 0, "sent_total": 0, "dead_total": 0,
        "week_sent": 0, "week_dead": 0,
    }
    for row in queue_rows:
        if not isinstance(row, dict):
            continue
        received = parse_iso_to_epoch(row.get("received_at"))
        if received is None:
            record = row.get("record")
            if isinstance(record, dict):
                received = parse_iso_to_epoch(record.get("received_at"))
        if received is None:
            continue
        if received >= today_start:
            counters["today_in"] += 1
        if received >= week_start:
            counters["week_in"] += 1
        seq = row.get("seq")
        if not isinstance(seq, int):
            seq = 0
        if received >= month_start and seq <= fetched_seq:
            counters["month_received"] += 1
    for row in reply_rows:
        if not isinstance(row, dict):
            continue
        status = row.get("status")
        sent = parse_iso_to_epoch(row.get("sent_at"))
        if status == "sent":
            counters["sent_total"] += 1
            if sent is not None and sent >= month_start:
                counters["month_sent"] += 1
            if sent is not None and sent >= week_start:
                counters["week_sent"] += 1
        elif status == "dead":
            counters["dead_total"] += 1
            if sent is not None and sent >= week_start:
                counters["week_dead"] += 1
    return counters


def bot_counters(root, bot_id, now=None, fetch_state=None):
    """Đếm của 1 bot (đọc file queue/replies). Xem
    ``_counters_from_rows`` để biết định nghĩa từng số."""
    if now is None:
        now = now_epoch()
    if fetch_state is None:
        fetch_state = read_fetch_state(root)
    fetched = fetched_seq_for(fetch_state, bot_id)
    queue_rows = read_jsonl_rows(Path(root) / "data" / bot_id / "queue.jsonl")
    reply_rows = read_jsonl_rows(
        Path(root) / "data" / bot_id / "replies.jsonl")
    return _counters_from_rows(queue_rows, reply_rows, fetched, now)


def quota_estimate_from_counters(counters, platform,
                                 warn_fraction=None):
    """Ước lượng quota Zalo từ đếm cục bộ; bot không phải Zalo -> None.

    ``warn_fraction``: ngưỡng cảnh báo quota (mặc định hằng số
    QUOTA_WARN_FRACTION; tầng app truyền giá trị đọc từ
    alert_settings.json — đổi là có hiệu lực ngay ở lần tính sau).
    Giá trị không hợp lệ -> về hằng số.

    Đếm CẢ 2 chiều (tin đến đã lấy + tin gửi thành công trong tháng)
    vì chưa biết Zalo đếm chiều nào (UC-04). Luôn ghi rõ "ước tính"
    ở tầng hiển thị — không phải số chính thức của nền tảng. Tháng
    tính theo dương lịch (GIẢ ĐỊNH TẠM THỜI — chưa kiểm chứng Zalo
    reset quota khi nào); file queue/replies chỉ giữ ~30 ngày nên
    số đầu tháng có thể thiếu nếu dữ liệu đã bị cắt tỉa.
    """
    if platform != "zalo":
        return None
    used = counters["month_received"] + counters["month_sent"]
    if isinstance(warn_fraction, bool) \
            or not isinstance(warn_fraction, (int, float)) \
            or not QUOTA_WARN_FRACTION_MIN <= warn_fraction \
            <= QUOTA_WARN_FRACTION_MAX:
        warn_fraction = QUOTA_WARN_FRACTION
    return {
        "used": used,
        "limit": ZALO_MONTHLY_QUOTA,
        "pct": (used * 100 // ZALO_MONTHLY_QUOTA
                if ZALO_MONTHLY_QUOTA else 0),
        "warn": used >= ZALO_MONTHLY_QUOTA * warn_fraction,
    }


def quota_estimate(root, bot_id, platform, now=None, warn_fraction=None):
    """Ước lượng quota Zalo của 1 bot (đọc file)."""
    if warn_fraction is None:
        warn_fraction = load_alert_settings(root)
    return quota_estimate_from_counters(
        bot_counters(root, bot_id, now=now), platform,
        warn_fraction=warn_fraction)


def count_stats(root, bot_ids, now=None):
    """Dải số tổng quan (V2 §1): tin đến hôm nay / 7 ngày, trả lời đã
    gửi (tổng số trong file trả lời còn giữ), tin đang tồn (tổng tồn
    ước lượng), thư chết. Gọi với bot_ids đã lọc theo nền tảng để có
    dải số riêng từng tab.

    (#11 dot A) "Tin đến hôm nay / 7 ngày" tinh CA tin den qua
    trung tam (``_center_incoming``) CHUNG vao cung so, khong tach
    rieng — khop voi nhung gi tab Chat hien tu events.jsonl."""
    if now is None:
        now = now_epoch()
    fetch_state = read_fetch_state(root)
    totals = {"today_in": 0, "week_in": 0, "sent": 0, "backlog": 0,
              "dead": 0}
    for bot_id in bot_ids:
        counters = bot_counters(root, bot_id, now=now,
                                fetch_state=fetch_state)
        totals["today_in"] += counters["today_in"]
        totals["week_in"] += counters["week_in"]
        totals["sent"] += counters["sent_total"]
        totals["dead"] += counters["dead_total"]
        state = read_state(root, bot_id)
        latest_seq = state.get("queue_latest_seq")
        if not isinstance(latest_seq, int):
            latest_seq = 0
        fetched = fetched_seq_for(fetch_state, bot_id)
        totals["backlog"] += max(0, latest_seq - fetched)
    today_start = _local_day_start(now)
    week_start = _local_day_start(now, 6)
    for item in _center_incoming(root):
        if item["ts"] >= today_start:
            totals["today_in"] += 1
        if item["ts"] >= week_start:
            totals["week_in"] += 1
    return totals


def sort_summaries(summaries):
    """Thứ tự thẻ (V2 §2): bot có vấn đề lên đầu, còn lại xếp theo
    tên hiển thị (không phân biệt hoa/thường), hòa thì theo bot_id."""
    return sorted(
        summaries,
        key=lambda s: (0 if s.get("alerts") else 1,
                       (s.get("display_name") or s["bot_id"]).lower(),
                       s["bot_id"]))


def bot_summary(root, bot_id, entry, now=None, unit_state="unknown",
                fetch_state=None, accounts=None, transfers_data=None,
                quota_warn_fraction=None):
    """Tóm tắt 1 bot cho thẻ trên màn hình chính (05 §1).

    ``unit_state``: "active"/"inactive"/"unknown" (tầng app đo bằng
    systemctl; "unknown" khi không đo được — không đoán).
    ``accounts``/``transfers_data``: dữ liệu đã đọc 1 lần mỗi request
    (N11) — khi None thì tự đọc (đường gọi trực tiếp vẫn đúng).
    ``quota_warn_fraction``: ngưỡng cảnh báo quota đã đọc 1 lần mỗi
    request (None -> hằng số QUOTA_WARN_FRACTION)."""
    if now is None:
        now = now_epoch()
    if fetch_state is None:
        fetch_state = read_fetch_state(root)
    if accounts is None:
        accounts = read_accounts(root)
    if transfers_data is None:
        transfers_data = read_transfers(root)
    config = read_bot_config(root, bot_id)
    state = read_state(root, bot_id)
    fetched = fetched_seq_for(fetch_state, bot_id)
    fetch_entry = _fetch_entry(fetch_state, bot_id)
    fetch_labels = fetch_entry.get("fetch_labels")
    if not isinstance(fetch_labels, dict):
        fetch_labels = {}
    transfer = active_transfer_for_bot(transfers_data, bot_id, now)
    owner_id = owner_account_of(entry)
    latest_seq = state.get("queue_latest_seq")
    if not isinstance(latest_seq, int):
        latest_seq = 0
    updated = parse_iso_to_epoch(state.get("updated_at"))
    state_age = (now - updated) if updated is not None else None
    last_error_class = state.get("last_error_class")
    token_state = state.get("token_state")
    queue_rows = read_jsonl_rows(Path(root) / "data" / bot_id / "queue.jsonl")
    last_message = None
    if queue_rows:
        last_row = queue_rows[-1]
        record = last_row.get("record")
        if isinstance(record, dict):
            last_message = {
                "preview": preview_text(_record_text(record)),
                "ts": parse_iso_to_epoch(last_row.get("received_at")),
                "chat_title": record.get("chat_title"),
            }
    reply_rows = read_jsonl_rows(
        Path(root) / "data" / bot_id / "replies.jsonl")
    counters = _counters_from_rows(queue_rows, reply_rows, fetched, now)
    platform = config.get("platform") or entry.get("platform") or "?"
    allowlist = read_allowlist(root, bot_id)
    sync = sync_status(root, bot_id, now)
    summary = {
        "bot_id": bot_id,
        "display_name": config.get("display_name") or bot_id,
        "platform": platform,
        "enabled": bool(entry.get("enabled", False)),
        "paused": bool(entry.get("paused", False)),
        "paused_since": entry.get("paused_since"),
        "token_pending_verification": bool(
            entry.get("token_pending_verification", False)),
        "awaiting_muse_sync": is_awaiting_muse_sync(
            entry, fetch_state, bot_id),
        "owner_account": owner_id,
        "owner_display_name": account_display_name(
            accounts.get("accounts") or {}, owner_id) if owner_id else None,
        "fetch_labels": fetch_labels,
        "fetch_entry": fetch_entry,
        "transfer": transfer,
        "transfer_active": transfer is not None,
        "transfer_hide_until": parse_iso_to_epoch(
            transfer.get("expires_at")) if transfer else None,
        "dual_fetch_labels": sorted(active_fetch_labels(fetch_labels, now)),
        "transfer_to_display_name": account_display_name(
            accounts.get("accounts") or {},
            transfer.get("to_account_id")) if transfer else None,
        "unit_state": unit_state,
        "last_poll_ok": state.get("last_poll_ok"),
        "last_poll_at": state.get("last_poll_at"),
        "state_age_seconds": state_age,
        "stale": state_age is None or state_age > STALE_AFTER_SECONDS,
        "token_state": token_state,
        "flag_401": token_state == "WAIT_TOKEN"
                    or last_error_class == "TOKEN_INVALID",
        "flag_429": last_error_class == "RATE_LIMITED",
        "consec_err": state.get("consec_err"),
        "last_error_class": last_error_class,
        "latest_seq": latest_seq,
        "fetched_seq": fetched,
        "backlog_estimate": max(0, latest_seq - fetched),
        "last_message": last_message,
        "sync": sync,
        "has_allowlist": allowlist is not None,
        "allowed_chat_count": len(allowlist.get("allowed_chats") or [])
        if isinstance(allowlist, dict) else 0,
        "today_in": counters["today_in"],
        "week_in": counters["week_in"],
        "week_sent": counters["week_sent"],
        "week_dead": counters["week_dead"],
        "quota": quota_estimate_from_counters(
            counters, platform, warn_fraction=quota_warn_fraction),
    }
    summary["alerts"] = compute_alerts(summary, now)
    return summary


def redact_log_line(line):
    """Lược 1 dòng log trước khi hiện trên UI (SPEC S6).

    Xóa giá trị header Authorization/Bearer và đoạn token trong URL
    dạng /bot.../; cắt dòng còn tối đa 500 ký tự. Log của tai vốn đã
    chỉ có metadata — đây là lớp lược thứ hai ở phía hiển thị.
    """
    if not isinstance(line, str):
        line = str(line)
    # Thứ tự quan trọng: lược "Bearer <giá trị>" TRƯỚC, vì pattern
    # Authorization sẽ nuốt chữ "Bearer" và để lọt giá trị phía sau.
    redacted = _REDACT_BEARER.sub(r"\1[REDACTED]", line)
    redacted = _REDACT_AUTH.sub(r"\1[REDACTED]", redacted)
    redacted = _REDACT_BOT_URL.sub("/bot[REDACTED]/", redacted)
    if len(redacted) > 500:
        redacted = redacted[:500] + "…"
    return redacted


# ---------------------------------------------------------------------------
# Trạng thái cảnh báo + đã xem (bộ đếm "cảnh báo chưa xem" ở màn hình chính)
# ---------------------------------------------------------------------------

def _alert_state_path(root):
    return os.path.join(root, "ui-config", "alert_state.json")


def _alert_seen_path(root):
    return os.path.join(root, "ui-config", "alert_seen.json")


def _alert_since(entry):
    """Mốc xuất hiện từ 1 mục trạng thái cảnh báo.

    Định dạng mới (V2): {"since": epoch, "text": "..."}; định dạng cũ
    (GĐ3): số epoch trần. File cũ trên máy thật vẫn đọc được.
    """
    if isinstance(entry, dict):
        value = entry.get("since")
    else:
        value = entry
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _alert_text(entry, key):
    """Câu cảnh báo đã lưu, hoặc nhãn dự phòng theo khóa."""
    if isinstance(entry, dict):
        text = entry.get("text")
        if isinstance(text, str) and text:
            return text
    return ALERT_KEY_LABELS.get(key, key)


def update_alert_state(root, summaries, now):
    """Cập nhật mốc xuất hiện của từng cảnh báo đang có + ghi sự kiện.

    ``{bot_id: {key: {"since": epoch, "text": ...}}}``: cảnh báo mới
    xuất hiện thì ``since=now``; cảnh báo đã hết thì xóa khỏi file.
    Cảnh báo mất rồi xuất hiện lại có ``since`` mới — đúng để tính
    "chưa xem" lại từ đầu. Mỗi lần xuất hiện/biến mất đều ghi 1 sự
    kiện vào ``ui-config/alert_events.jsonl`` (V2 §5). Ghi
    best-effort (lỗi thì bỏ qua, không chặn trang).
    """
    try:
        current = {}
        for summary in summaries:
            keys = {}
            for alert in summary.get("alerts") or []:
                keys[alert["key"]] = (alert.get("text") or "",
                                      alert.get("level") or "yellow")
            current[summary["bot_id"]] = keys
        path = _alert_state_path(root)
        previous = read_json_file(path) or {}
        merged = {}
        raised = []
        cleared = []
        for bot_id, keys in current.items():
            old = previous.get(bot_id) or {}
            if not isinstance(old, dict):
                old = {}
            merged_keys = {}
            for key, (text, level) in keys.items():
                since = _alert_since(old.get(key))
                if since is None:
                    since = now
                    raised.append((bot_id, key, text, level))
                merged_keys[key] = {"since": since, "text": text}
            merged[bot_id] = merged_keys
            for key, entry in old.items():
                if key not in keys:
                    cleared.append((bot_id, key, _alert_text(entry, key)))
        for bot_id, old in previous.items():
            if bot_id not in current and isinstance(old, dict):
                for key, entry in old.items():
                    cleared.append((bot_id, key, _alert_text(entry, key)))
        write_json_file(path, merged, mode=0o600)
        if raised or cleared:
            _append_alert_events(root, raised, cleared, now)
        return merged
    except OSError:
        return {}


def _ack_still_effective(key, acked_at, now):
    """Ack còn hiệu lực không. Riêng khóa dual-fetch: ack quá 24 giờ
    (doc 09 §7) thì coi như chưa ack (B1) — cảnh báo hiện lại với
    since cũ, không sinh since/sự kiện mới. Khóa khác: ack vĩnh viễn
    cho tới khi cảnh báo mất rồi xuất hiện lại (since mới)."""
    if acked_at is None:
        return False
    if key == "dual-fetch" and now - acked_at > DUAL_FETCH_ACK_SECONDS:
        return False
    return True


def count_unseen_alerts(root, summaries, now=None):
    """Đếm cảnh báo chưa xem: cảnh báo đang có mà chủ nhân chưa mở
    trang chi tiết của bot đó kể từ lúc cảnh báo xuất hiện. Cảnh báo
    dual-fetch đang tạm ẩn theo luồng chuyển (R4) không đếm; ack
    dual-fetch quá 24 giờ coi như chưa ack (B1)."""
    if now is None:
        now = now_epoch()
    try:
        state = read_json_file(_alert_state_path(root)) or {}
        seen = read_json_file(_alert_seen_path(root)) or {}
    except OSError:
        return 0
    acks = read_alert_acks(root)
    count = 0
    for summary in summaries:
        bot_id = summary["bot_id"]
        bot_state = state.get(bot_id) or {}
        bot_seen = seen.get(bot_id) or {}
        bot_acks = acks.get(bot_id) or {}
        hidden_dual = summary_dual_fetch_hidden(summary, now)
        for alert in summary.get("alerts") or []:
            key = alert["key"]
            if key == "dual-fetch" and hidden_dual:
                continue
            since = _alert_since(bot_state.get(key))
            if since is None:
                # Không có mốc (file trạng thái chưa ghi được): tính là
                # chưa xem để không giấu cảnh báo.
                count += 1
            elif bot_seen.get(key, 0) < since:
                count += 1
            # Ack dual-fetch hết hạn thì hiện lại: nếu đã xem (seen >=
            # since) nhưng ack đã hết hạn và điều kiện còn đúng thì
            # vẫn tính là chưa xem lại — xử lý qua nhánh ack bên dưới.
            if key == "dual-fetch" and since is not None \
                    and bot_seen.get(key, 0) >= since:
                acked_at = bot_acks.get(key)
                # Chỉ tính lại khi dấu "đã xem" chính là do lần ack đó
                # ghi (không có lần mở trang chi tiết nào mới hơn ack);
                # mở trang sau ack thì dấu xem mới hơn ack và vẫn tính
                # là đã xem.
                if acked_at is not None and acked_at >= since \
                        and bot_seen.get(key, 0) <= acked_at \
                        and not _ack_still_effective(key, acked_at, now):
                    count += 1
    return count


def mark_alerts_seen(root, bot_id, alert_keys, now):
    """Ghi dấu chủ nhân đã xem cảnh báo của 1 bot (khi mở trang chi
    tiết). Best-effort, không raise."""
    try:
        seen = read_json_file(_alert_seen_path(root)) or {}
        bot_seen = seen.get(bot_id) or {}
        for key in alert_keys:
            bot_seen[key] = now
        seen[bot_id] = bot_seen
        write_json_file(_alert_seen_path(root), seen, mode=0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# V2: sự kiện cảnh báo + xác nhận + lịch sử lần lấy tin + thống kê ngày
# ---------------------------------------------------------------------------

def _alert_events_path(root):
    return Path(root) / "ui-config" / "alert_events.jsonl"


def _alert_acks_path(root):
    return Path(root) / "ui-config" / "alert_acks.json"


def _append_alert_events(root, raised, cleared, now):
    """Ghi sự kiện xuất hiện/biến mất của cảnh báo (append-only).

    ``raised``: list (bot_id, key, text, level); ``cleared``: list
    (bot_id, key, text). Mỗi lần ghi đều cắt tỉa sự kiện quá 30 ngày
    (ghi lại file khi có dòng bị loại). Best-effort: lỗi thì bỏ qua,
    không chặn trang.
    """
    try:
        path = _alert_events_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for bot_id, key, text, level in raised:
            lines.append({"ts": now, "kind": "raised", "bot_id": bot_id,
                          "key": key, "text": text, "level": level})
        for bot_id, key, text in cleared:
            lines.append({"ts": now, "kind": "cleared", "bot_id": bot_id,
                          "key": key, "text": text})
        with open(path, "a", encoding="utf-8") as handle:
            for event in lines:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        _prune_alert_events(root, now)
    except OSError:
        pass


def _prune_alert_events(root, now):
    """Loại sự kiện quá 30 ngày khỏi file (ghi lại khi có dòng bị loại)."""
    path = _alert_events_path(root)
    rows = read_jsonl_rows(path)
    cutoff = now - ALERT_HISTORY_DAYS * 86400
    kept = [row for row in rows
            if isinstance(row.get("ts"), (int, float)) and row["ts"] >= cutoff]
    if len(kept) != len(rows):
        tmp_path = path.with_suffix(".jsonl.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for row in kept:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)


def read_alert_events(root, now, days=ALERT_HISTORY_DAYS, bot_id=None,
                      limit=ALERT_EVENT_LIMIT):
    """Sự kiện cảnh báo trong ``days`` ngày, mới nhất trước.

    Chỉ nhận dòng hợp lệ (kind raised/cleared/acked, ts là số). Lọc
    theo bot khi có ``bot_id``.
    """
    cutoff = now - days * 86400
    events = []
    for row in read_jsonl_rows(_alert_events_path(root)):
        ts = row.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        if ts < cutoff:
            continue
        if row.get("kind") not in ("raised", "cleared", "acked"):
            continue
        if bot_id is not None and row.get("bot_id") != bot_id:
            continue
        events.append({
            "ts": ts,
            "kind": row.get("kind"),
            "bot_id": str(row.get("bot_id") or ""),
            "key": str(row.get("key") or ""),
            "text": row.get("text") or "",
            "level": row.get("level") or "",
        })
    events.sort(key=lambda event: event["ts"], reverse=True)
    return events[:limit]


def alert_events_totals(root, now=None, days=ALERT_HISTORY_DAYS,
                        bot_id=None, limit=ALERT_EVENT_LIMIT):
    """Dem nhe cho tom tat node lich su su kien/canh bao dong (lech
    T3 (b), T3m): (so dong dang giu, gio moi nhat) — chi dem +
    lay max ts, KHONG goi read_alert_events (handler bo qua lenh
    doc cua node dong; tong van can cho tom tat — TT11). So dong
    cat o ``limit`` nhu node khi mo (chi giu toi da 100 dong)."""
    if now is None:
        now = now_epoch()
    cutoff = now - days * 86400
    count = 0
    newest = None
    for row in read_jsonl_rows(_alert_events_path(root)):
        ts = row.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        if ts < cutoff:
            continue
        if row.get("kind") not in ("raised", "cleared", "acked"):
            continue
        if bot_id is not None and row.get("bot_id") != bot_id:
            continue
        if count < limit:
            count += 1
        if newest is None or ts > newest:
            newest = ts
    return count, newest


def history_totals(root, bot_id, now=None):
    """Dem nhe cho tom tat node Lich su tin dong (lech T3 (b),
    T3m): (tong so muc, gio moi nhat) — chi dem dong + lay max
    gio, KHONG goi merged_history (handler bo qua lenh doc cua
    node dong). Tong giong merged_history: tin den + tra loi (chi
    cong tra loi khi dong bo che do g3b)."""
    if now is None:
        now = now_epoch()
    total = 0
    newest = None
    queue_rows = read_jsonl_rows(
        Path(root) / "data" / bot_id / "queue.jsonl")
    total += len(queue_rows)
    for row in queue_rows:
        if not isinstance(row, dict):
            continue
        parsed = parse_iso_to_epoch(row.get("received_at"))
        if parsed is None:
            record = row.get("record")
            if isinstance(record, dict):
                parsed = parse_iso_to_epoch(record.get("received_at"))
        if parsed is not None and (newest is None or parsed > newest):
            newest = parsed
    if sync_status(root, bot_id, now)["mode"] == "g3b":
        reply_rows = read_jsonl_rows(
            Path(root) / "data" / bot_id / "replies.jsonl")
        total += len(reply_rows)
        for row in reply_rows:
            if not isinstance(row, dict):
                continue
            parsed = parse_iso_to_epoch(row.get("sent_at"))
            if parsed is not None and (
                    newest is None or parsed > newest):
                newest = parsed
    return total, newest


def read_alert_acks(root):
    """Đọc ``{bot_id: {key: acked_at}}``; thiếu/hỏng -> {}."""
    data = read_json_file(_alert_acks_path(root))
    if not isinstance(data, dict):
        return {}
    out = {}
    for bot_id, keys in data.items():
        if isinstance(keys, dict):
            out[bot_id] = {key: value for key, value in keys.items()
                           if isinstance(value, (int, float))}
    return out


def ack_alert(root, bot_id, key, now, text=None):
    """Xác nhận 1 cảnh báo đang mở: ghi mốc ack + sự kiện "acked" + ghi
    dấu đã xem (ack mạnh hơn xem — banner chưa xem cũng hết). Khóa lạ
    thì từ chối (ValueError) để tầng app trả 404. Best-effort khi ghi
    file (lỗi OSError thì bỏ qua mốc ack, vẫn ghi sự kiện nếu được).
    """
    if key not in ALERT_KEYS:
        raise ValueError("khóa cảnh báo không hợp lệ: %r" % (key,))
    try:
        acks = read_alert_acks(root)
        bot_acks = acks.get(bot_id) or {}
        bot_acks[key] = now
        acks[bot_id] = bot_acks
        write_json_file(_alert_acks_path(root), acks, mode=0o600)
    except OSError:
        pass
    mark_alerts_seen(root, bot_id, [key], now)
    try:
        path = _alert_events_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts": now, "kind": "acked", "bot_id": bot_id, "key": key,
                 "text": text or ALERT_KEY_LABELS.get(key, key)}
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --- Canh bao chat tac trung tam (v0.6.0, T3i — D4) ---------------------
# Nguong D4 (QUYET-DINH-QUA-DEM-20-11): tin cho > 3 phut HOAC
# >= 3 tin/phong -> 1 dong muc "can xem" trong tab Canh bao.
CENTER_CHAT_STUCK_WAIT_SECONDS = 180
CENTER_CHAT_STUCK_MIN_COUNT = 3
CENTER_CHAT_STUCK_LEVEL = "cần xem"


def _center_chat_stuck_path(root):
    return Path(root) / "data" / "center" / "chat_stuck_alerts.jsonl"


def _center_chat_waiting_by_room(events):
    """{room: [candidate]} — tin hoi dang cho tra loi theo tung phong.

    Candidate: tin message kind=hoi, khong phai cua chu nhan, khong
    phai tin hub tu sinh (source="hub"), va chua co tin tra loi sau
    no trong cung phong (tin tra loi = tin sau do cua chu nhan hoac
    cua tai khoan khac). Chi tra metadata (seq, tai khoan gui, gio
    tao), KHONG mang noi dung tin."""
    by_room = {}
    latest_reply_seq = {}
    for row in events:
        if row.get("kind") != "message":
            continue
        record = row.get("record") or {}
        room = record.get("room") or "chung"
        sender = record.get("from_account") or ""
        seq = row.get("seq")
        if not isinstance(seq, int):
            continue
        if record.get("kind") == "hoi" and sender != "owner" \
                and record.get("source") != "hub":
            by_room.setdefault(room, []).append({
                "seq": seq, "account_id": sender,
                "created_at": parse_iso_to_epoch(
                    record.get("created_at")),
            })
        # Tin sau do cua chu nhan/tai khoan khac la tin tra loi cho
        # moi tin cho truoc do trong phong (tin cua chinh nguoi gui
        # thi chi la noi tiep, khong phai tra loi).
        latest_reply_seq.setdefault(room, []).append((seq, sender))
    waiting = {}
    for room, candidates in by_room.items():
        replies = latest_reply_seq.get(room) or []
        kept = []
        for cand in candidates:
            answered = any(
                seq > cand["seq"] and sender != cand["account_id"]
                for seq, sender in replies)
            if not answered:
                kept.append(cand)
        if kept:
            kept.sort(key=lambda item: item["seq"])
            waiting[room] = kept
    return waiting


def center_chat_stuck_alerts(root, now=None):
    """Phong chat tac cho muc trung tam o tab Canh bao (v0.6.0, T3i
    — D4: 1 dong muc "can xem" moi phong tac).

    Nguon chinh: ``data/center/chat_stuck_alerts.jsonl`` do phia
    Muse (``center_dispatcher.record_chat_stuck_alerts``, T3h) ghi
    — lay dong moi nhat moi (tai khoan, phong). File nay CHI co
    phia Muse nen tren PC thuong vang: khi do tinh TRUC TIEP tu
    ``data/center/events.jsonl`` tren PC theo cung nguong D4. Du
    lay tu nguon nao, dong nao cung duoc kiem lai do tuoi tren
    events hien tai (tin cu nhat phai van dang cho — tranh hien
    canh bao da het tac tu file cu) va so lieu (so tin cho, so giay
    cho) tinh lai moi luc hien. Phong cua tai khoan da ngung thi
    khong tinh (tai khoan do khong keo tin nua). Chi tra metadata,
    KHONG mang noi dung tin. Tra [] khi khong co phong tac."""
    if now is None:
        now = time.time()
    events = read_jsonl_rows(Path(root) / "data" / "center"
                             / "events.jsonl")
    waiting = _center_chat_waiting_by_room(events)
    if not waiting:
        return []
    accounts = (read_accounts(root).get("accounts") or {})

    def _room_active(room):
        if not room.startswith("dm:"):
            return True
        record = accounts.get(room[3:])
        return bool(record and record.get("center_participating"))

    def _stuck_item(room, candidates, account_id=None):
        oldest = candidates[0]
        age = None
        if oldest.get("created_at") is not None:
            age = int(now - oldest["created_at"])
        if not ((age is not None
                 and age > CENTER_CHAT_STUCK_WAIT_SECONDS)
                or len(candidates) >= CENTER_CHAT_STUCK_MIN_COUNT):
            return None
        return {
            "account_id": account_id or oldest["account_id"],
            "room": room,
            "waiting_count": len(candidates),
            "oldest_seq": oldest["seq"],
            "oldest_wait_seconds": age,
            "level": CENTER_CHAT_STUCK_LEVEL,
        }

    flagged = {}
    for row in read_jsonl_rows(_center_chat_stuck_path(root)):
        account_id = row.get("account_id")
        room = row.get("room")
        oldest_seq = row.get("oldest_seq")
        if not isinstance(account_id, str) \
                or not isinstance(room, str) \
                or not isinstance(oldest_seq, int):
            continue
        flagged[(account_id, room)] = oldest_seq
    out = []
    if flagged:
        # File co dong: chi hien phong dispatcher da danh dau, nhung
        # so lieu tinh lai tren events hien tai (khong tin so cu).
        for (account_id, room), oldest_seq in flagged.items():
            candidates = waiting.get(room) or []
            if not _room_active(room):
                continue
            if not any(cand["seq"] == oldest_seq
                       for cand in candidates):
                continue
            item = _stuck_item(room, candidates, account_id)
            if item is not None:
                out.append(item)
    else:
        for room, candidates in waiting.items():
            if not _room_active(room):
                continue
            item = _stuck_item(room, candidates)
            if item is not None:
                out.append(item)
    out.sort(key=lambda item: item["oldest_seq"])
    return out


# --- Vòng thảo luận đã dừng (v0.6.0, T3l — doc 10 §3.8 §4b,
# DESIGN §3.10 bước 2/5, quyết D9) ------------------------------------
# UI tự tính lại từ kho hub trên PC (data/center/events.jsonl), CÙNG
# thuật toán đếm với phía Muse (khóa discussion_rounds):
# - 1 lượt = 1 tin kind=hoi có to_account (chuỗi v3 hoặc danh sách
#   v4); tin không ghi to_account thì không phải tin thảo luận.
# - Khóa vòng = (phòng, người mở, tập người tham gia): tin thảo luận
#   thuộc về vòng đang mở trong phòng có cùng TẬP người tham gia
#   ({người gửi} ∪ to_account) — cặp A↔B không làm cặp C↔D dừng oan
#   (D9); người mở = người gửi tin đầu tiên của vòng.
# - Tin hoi của chủ nhân vào phòng thì đặt lại mọi vòng trong phòng
#   đó về 0/mở (Q-V060-04, tiêu chí 5 T3l: bộ đếm về 0) và KHÔNG tính
#   là 1 lượt; tin hub tự sinh (source="hub") không tính. Vòng
#   count==0 được GIỮ LẠI (không xóa) — đúng định nghĩa phía Muse
#   (G-L5, AUDIT-V060: thống nhất về 1 định nghĩa, UI theo phía
#   Muse); tin thảo luận tiếp theo khớp vòng đó là lượt 1 của lần
#   mở mới (opened_seq cập nhật theo tin đó).
# - Chạm 5 lượt -> state "dong" (dừng). Tin sau khi dừng vẫn ghi
#   vào kho (chủ nhân thấy) nhưng không đánh thức ai (phần wake do
#   thành viên logic làm — UI chỉ HIỂN THỊ, không chặn ghi tin);
#   count giữ 5, chỉ cập nhật last_seq (như phía Muse).
# Chỉ trả metadata (phòng/người mở/người tham gia/số lượt), KHÔNG
# mang nội dung tin.


def center_discussion_rounds(root):
    """Mọi vòng thảo luận, theo thứ tự mở vòng (kể cả vòng vừa
    được chủ nhân đặt lại về 0 — vòng đó được giữ lại).

    Mỗi vòng: {room, opener, participants (list đã xếp), count,
    state ("mo"/"dong"), opened_seq, last_seq}. Vòng vừa được chủ
    nhân đặt lại (count 0, state "mo") được GIỮ LẠI trong danh
    sách; tin sau khi vòng đã dừng không tăng count quá 5, chỉ cập
    nhật last_seq, state giữ "dong" — cùng định nghĩa với phía
    Muse (``center_discussion.compute_discussion_rounds``)."""
    events = read_jsonl_rows(Path(root) / "data" / "center"
                             / "events.jsonl")
    rounds = {}
    order = []
    for row in events:
        if row.get("kind") != "message":
            continue
        record = row.get("record") or {}
        if record.get("kind") != "hoi":
            continue
        if record.get("source") == "hub":
            continue
        room = record.get("room") or "chung"
        sender = record.get("from_account") or ""
        seq = row.get("seq")
        if not isinstance(seq, int):
            continue
        if sender == "owner":
            # Chủ nhân xen tin: mọi vòng trong phòng về 0/mở; tin
            # của chủ nhân không tính là 1 lượt (tiêu chí 5: bộ đếm
            # về 0). Vòng GIỮ LẠI (G-L5) — không xóa khỏi danh sách.
            for key in [key for key in order if key[0] == room]:
                rounds[key]["count"] = 0
                rounds[key]["state"] = "mo"
            continue
        targets = _as_to_account_list(record.get("to_account"))
        if not targets:
            continue
        participants = frozenset([sender] + targets)
        # Tim vong trong cung phong co cung TAP nguoi tham gia (khop
        # chinh xac tap — nhu phia Muse); neu co nhieu vong cung tap
        # (khac nguoi mo, theo D9) thi chon vong hoat dong gan nhat
        # (last_seq lon nhat).
        current = None
        current_key = None
        for key in order:
            if key[0] != room or key[2] != participants:
                continue
            candidate = rounds[key]
            if current is None \
                    or candidate["last_seq"] > current["last_seq"]:
                current = candidate
                current_key = key
        if current is None:
            current_key = (room, sender, participants)
            current = {"room": room, "opener": sender,
                       "participants": sorted(participants),
                       "count": 0, "state": "mo",
                       "opened_seq": seq, "last_seq": seq}
            rounds[current_key] = current
            order.append(current_key)
        if current["count"] == 0:
            # Vong vua duoc chu nhan mo lai: tin nay la luot 1.
            current["opened_seq"] = seq
            current["count"] = 1
            current["state"] = "mo"
        elif current["count"] < CENTER_DISCUSSION_ROUND_MAX_TURNS:
            current["count"] += 1
            if current["count"] >= CENTER_DISCUSSION_ROUND_MAX_TURNS:
                current["state"] = "dong"
        # count da cham 5: giu nguyen 5/"dong", chi cap nhat last_seq.
        current["last_seq"] = seq
    return [rounds[key] for key in order]


def center_discussion_stopped_rounds(root):
    """Vòng thảo luận đã dừng (chạm 5 lượt), mới nhất trước."""
    stopped = [item for item in center_discussion_rounds(root)
               if item["state"] == "dong"]
    stopped.sort(key=lambda item: item["last_seq"], reverse=True)
    return stopped


def open_alerts(root, summaries, now):
    """Cảnh báo đang mở CHƯA xác nhận, mới nhất trước, đỏ trước vàng.

    Cảnh báo đã xác nhận thì ẩn; mất rồi xuất hiện lại (mốc since mới
    hơn mốc ack) thì hiện lại. Mỗi mục: bot_id, display_name,
    platform, key, level, text, since.

    Riêng dual-fetch (doc 09 §5.4): ack quá 24 giờ coi như chưa ack
    (B1); cảnh báo đang tạm ẩn theo luồng chuyển thì ẩn ở tầng hiển
    thị này (R4) — giữ since/lịch sử, không sinh cleared giả. Câu
    dual-fetch hiển thị kèm tên gọi tra lúc render (S16); văn bản lưu
    trữ chỉ mang account_id.
    """
    try:
        state = read_json_file(_alert_state_path(root)) or {}
    except OSError:
        state = {}
    acks = read_alert_acks(root)
    accounts = read_accounts(root)
    out = []
    for summary in summaries:
        bot_id = summary["bot_id"]
        bot_state = state.get(bot_id) or {}
        bot_acks = acks.get(bot_id) or {}
        hidden_dual = summary_dual_fetch_hidden(summary, now)
        for alert in summary.get("alerts") or []:
            key = alert["key"]
            if key == "dual-fetch" and hidden_dual:
                continue
            since = _alert_since(bot_state.get(key))
            if since is None:
                since = now
            acked_at = bot_acks.get(key)
            if acked_at is not None and acked_at >= since \
                    and _ack_still_effective(key, acked_at, now):
                continue
            text = alert.get("text") or ""
            if key == "dual-fetch":
                labels = summary.get("dual_fetch_labels")
                if labels:
                    text = dual_fetch_display_text(labels, accounts)
            out.append({
                "bot_id": bot_id,
                "display_name": summary.get("display_name") or bot_id,
                "platform": summary.get("platform") or "?",
                "key": key,
                "level": alert.get("level") or "yellow",
                "text": text,
                "since": since,
            })
    rank = {"red": 0, "yellow": 1}
    out.sort(key=lambda item: (rank.get(item["level"], 1), -item["since"]))
    return out


def _poll_history_path(root, bot_id):
    return Path(root) / "ui-config" / "poll_history" / ("%s.jsonl" % bot_id)


def poll_sample_status(summary):
    """Trạng thái sức khỏe của 1 mẫu: ok/stale/dead/unknown."""
    if summary.get("last_poll_ok") is False:
        return "dead"
    age = summary.get("state_age_seconds")
    if age is None:
        return "unknown"
    if age > DEAD_AFTER_SECONDS:
        return "dead"
    if age > STALE_AFTER_SECONDS:
        return "stale"
    return "ok"


def record_poll_sample(root, bot_id, summary, now):
    """Ghi 1 mẫu sức khỏe (tối đa 1 mẫu/5 phút/bot), cắt tỉa quá 7 ngày.

    Mẫu chỉ ghi khi có người mở giao diện (fragment làm tươi mỗi 5
    giây) — khoảng trống khi không ai mở UI là bình thường, tầng hiển
    thị ghi rõ giới hạn này. Bot đang tắt thì không ghi mẫu (tránh
    chuỗi "dead" giả). Best-effort, không raise.
    """
    if not summary.get("enabled"):
        return
    try:
        path = _poll_history_path(root, bot_id)
        rows = read_jsonl_rows(path)
        last_ts = None
        for row in reversed(rows):
            ts = row.get("ts")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                last_ts = ts
                break
        if last_ts is not None and now - last_ts < POLL_SAMPLE_MIN_GAP_SECONDS:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        sample = {"ts": now, "status": poll_sample_status(summary),
                  "age_seconds": summary.get("state_age_seconds")}
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
        cutoff = now - POLL_HISTORY_DAYS * 86400
        kept = [row for row in rows + [sample]
                if isinstance(row.get("ts"), (int, float))
                and row["ts"] >= cutoff]
        if len(kept) != len(rows) + 1:
            tmp_path = path.with_suffix(".jsonl.tmp")
            with open(tmp_path, "w", encoding="utf-8") as handle:
                for row in kept:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            os.replace(tmp_path, path)
    except OSError:
        pass


def read_poll_samples(root, bot_id, now, days=POLL_HISTORY_DAYS,
                      limit=POLL_SAMPLE_LIMIT):
    """Mẫu sức khỏe trong ``days`` ngày, cũ trước mới sau (để vẽ dải),
    tối đa ``limit`` mẫu mới nhất."""
    cutoff = now - days * 86400
    samples = []
    for row in read_jsonl_rows(_poll_history_path(root, bot_id)):
        ts = row.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        if ts < cutoff:
            continue
        if row.get("status") not in ("ok", "stale", "dead", "unknown"):
            continue
        samples.append({"ts": ts, "status": row["status"],
                        "age_seconds": row.get("age_seconds")})
    samples.sort(key=lambda item: item["ts"])
    return samples[-limit:]


def daily_stats(root, bot_id, now, days=7):
    """Thống kê theo ngày của 1 bot: mới nhất trước.

    Mỗi mục: {"label": "DD/MM", "incoming": n, "sent": n, "dead": n}
    theo ngày dương lịch giờ địa phương của PC.
    """
    queue_rows = read_jsonl_rows(Path(root) / "data" / bot_id / "queue.jsonl")
    reply_rows = read_jsonl_rows(
        Path(root) / "data" / bot_id / "replies.jsonl")
    out = []
    for back in range(days):
        start = _local_day_start(now, back)
        end = start + 86400
        label = datetime.fromtimestamp(start).astimezone().strftime("%d/%m")
        incoming = 0
        for row in queue_rows:
            if not isinstance(row, dict):
                continue
            ts = parse_iso_to_epoch(row.get("received_at"))
            if ts is None:
                record = row.get("record")
                if isinstance(record, dict):
                    ts = parse_iso_to_epoch(record.get("received_at"))
            if ts is not None and start <= ts < end:
                incoming += 1
        sent = 0
        dead = 0
        for row in reply_rows:
            if not isinstance(row, dict):
                continue
            ts = parse_iso_to_epoch(row.get("sent_at"))
            if ts is None or not (start <= ts < end):
                continue
            if row.get("status") == "sent":
                sent += 1
            elif row.get("status") == "dead":
                dead += 1
        out.append({"label": label, "incoming": incoming, "sent": sent,
                    "dead": dead})
    return out


def list_chats(root, bot_id, limit=200):
    """Danh sách chat/nhóm đã nhắn tới bot (từ file queue): mỗi mục
    {"chat_id", "title", "key"}; tên lấy từ dòng mới nhất của chat đó;
    xếp theo tên (không phân biệt hoa/thường).

    ``key`` là khóa lọc mờ (SHA-256 của chat_id, cắt 16 ký tự) để
    form lọc lịch sử không phải render chat_id thô ra HTML — chat_id
    thô không bao giờ xuất hiện trên trang (giữ như luồng thêm chat
    theo seq).
    """
    chats = {}
    order = {}
    for index, row in enumerate(read_jsonl_rows(
            Path(root) / "data" / bot_id / "queue.jsonl")):
        if not isinstance(row, dict):
            continue
        record = row.get("record")
        if not isinstance(record, dict):
            continue
        chat_id = str(record.get("chat_id") or "")
        if not chat_id:
            continue
        title = record.get("chat_title")
        chats[chat_id] = title if isinstance(title, str) and title else None
        order[chat_id] = index
    items = [{"chat_id": chat_id,
              "title": chats[chat_id] or short_id(chat_id),
              "key": chat_filter_key(chat_id)}
             for chat_id in chats]
    items.sort(key=lambda item: (item["title"].lower(), item["chat_id"]))
    return items[:limit]


def chat_filter_key(chat_id):
    """Khóa lọc mờ của 1 chat_id (SHA-256, 16 ký tự hex)."""
    digest = hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()
    return digest[:16]


def resolve_chat_filter(root, bot_id, key):
    """Đổi khóa lọc mờ -> chat_id thật; khóa lạ -> None (không lọc)."""
    if not key:
        return None
    for chat in list_chats(root, bot_id):
        if chat["key"] == key:
            return chat["chat_id"]
    return None


def write_bot_config(root, bot_id, config):
    """Ghi ``config/<bot_id>.json`` nguyên tử, quyền 600."""
    path = Path(root) / "config" / ("%s.json" % bot_id)
    write_json_file(path, config, mode=0o600)


def write_allowlist(root, bot_id, allowlist):
    """Ghi ``config/<bot_id>.allowlist.json`` nguyên tử, quyền 600."""
    path = Path(root) / "config" / ("%s.allowlist.json" % bot_id)
    write_json_file(path, allowlist, mode=0o600)


def read_platform_endpoints(root):
    """Đọc ``ui-config/platform_endpoints.json``: {platform: base_url}.

    UI KHÔNG hard-code host API thật (rào chắn cấm). File này do agent
    PC ghi lúc triển khai; thiếu/không hợp lệ -> {} (UI báo rõ là
    chưa cấu hình địa chỉ API, không kiểm tra được token).
    """
    data = read_json_file(Path(root) / "ui-config" / "platform_endpoints.json")
    if not isinstance(data, dict):
        return {}
    out = {}
    for platform in ("zalo", "telegram"):
        value = data.get(platform)
        if isinstance(value, str) and value.strip():
            out[platform] = value.strip()
    return out
