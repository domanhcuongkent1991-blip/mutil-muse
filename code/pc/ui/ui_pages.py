"""Hiển thị HTML của Web UI — mọi giá trị động đều escape (05 §6).

Không có template engine: HTML dựng bằng hàm Python + ``html.escape``
cho MỌI giá trị động (tên bot, tên chat, nội dung tin, id, lỗi...).
Không có <script>/<style> inline, không handler inline — CSP
``script-src 'self'; style-src 'self'`` có hiệu lực (xem ui_static).

Các fragment (``*_fragment_html``) dùng chung cho trang đầy đủ lẫn
endpoint polling của JS: server render 1 lần, JS chỉ thay innerHTML
bằng fragment đã escape sẵn.
"""

from __future__ import annotations

import html
import re
import time
from urllib.parse import quote, urlencode

from pc.ui.ui_store import (
    CHIP_NO_REPLY_SEEN,
    G3A_NOTICE,
    SEVERITY_LABELS,
    UI_PREF_DEFAULT_COLLAPSED,
    alert_needs_owner,
    center_fetch_status,
    dead_error_text,
    format_date_saigon,
    format_day_time,
    format_day_time_saigon,
    label_display_state,
    parse_iso_to_epoch,
    preview_text,
    short_id,
    summary_dual_fetch_hidden,
)

# Tên ngắn 10 mốc của luồng chuyển bot (doc 08 §6 / doc 09 §4.3).
TRANSFER_STEP_TITLES = (
    "Chuẩn bị",
    "Dừng lấy tin ở tài khoản cũ",
    "Kiểm tra xả hết ở tài khoản cũ",
    "Chuyển credential",
    "Chuyển secret",
    "Chuyển lịch sử/trí nhớ",
    "Đổi nhãn chủ quản",
    "Bật lấy tin ở tài khoản mới + kiểm chứng",
    "Dọn dẹp ở tài khoản cũ",
    "Ghi nhật ký chuyển",
)


def esc(value):
    """Escape HTML cho mọi giá trị động (kể cả trong thuộc tính)."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def human_file_size(size_bytes):
    """Dung lượng file dạng người đọc, dấu phẩy thập phân kiểu Việt
    Nam (vd: 11 byte, 2,4 KB, 3,1 MB, 2 GB). Số âm/không phải số -> "—"."""
    try:
        size = int(size_bytes)
    except (TypeError, ValueError):
        return "—"
    if size < 0:
        return "—"
    if size < 1024:
        return "%d byte" % size
    if size < 1024 * 1024:
        return "%s KB" % _one_decimal(size / 1024)
    if size < 1024 * 1024 * 1024:
        return "%s MB" % _one_decimal(size / (1024 * 1024))
    return "%s GB" % _one_decimal(size / (1024 * 1024 * 1024))


def _one_decimal(value):
    """1 số thập phân, bỏ phần thập phân khi bằng 0, dấu phẩy kiểu
    Việt Nam (2,4 thay vì 2.4)."""
    text = ("%.1f" % value).replace(".", ",")
    if text.endswith(",0"):
        text = text[:-2]
    return text


def layout(title, body, csrf_token=None, show_topbar=True, body_class="",
           theme="light"):
    """Khung trang đầy đủ: CSS/JS từ /static (cùng nguồn).

    ``csrf_token`` (token của phiên) được gắn vào form đăng xuất;
    trang đăng nhập dùng ``show_topbar=False`` nên không cần.
    ``body_class="has-tabs"`` dành cho trang chính: chừa chỗ cho
    thanh tab cố định dưới màn hình điện thoại.
    ``theme`` (v0.6.0, T3k — phong cách C): "light" (Sáng Superhuman,
    mặc định) hoặc "dark" (Tối Hades) -> ``data-theme="light"`` hoặc
    ``data-theme="hades"`` trên thẻ <html>; giá trị lạ -> "light".
    Trang thiếu data-theme thì CSS mặc định về Superhuman (do :root
    là sáng) — lớp dự phòng, không phải đường chính.
    """
    data_theme = ui_data_theme(theme)
    topbar = ""
    if show_topbar:
        csrf_input = ""
        if csrf_token:
            csrf_input = (
                '<input type="hidden" name="csrf_token" value="%s">'
                % esc(csrf_token)
            )
        topbar = (
            '<div class="topbar">'
            '<span class="fresh" id="data-freshness"></span>'
            '<span class="fresh is-hidden" id="stale-warning">'
            '<span class="chip yellow">Dữ liệu có thể đã cũ</span></span>'
            '<button type="button" id="stale-reload" class="small">'
            "Tải lại</button>"
            '<form class="inline" method="post" action="/logout">'
            "%s"
            '<button class="small" type="submit">Đăng xuất</button>'
            "</form></div>"
        ) % csrf_input
    body_attr = ""
    if body_class:
        body_attr = ' class="%s"' % esc(body_class)
    return (
        "<!DOCTYPE html>\n<html lang=\"vi\" data-theme=\"%s\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, "
        "initial-scale=1\">\n"
        "<title>%s</title>\n"
        "<link rel=\"stylesheet\" href=\"/static/app.css\">\n"
        "<script src=\"/static/app.js\" defer></script>\n"
        "</head>\n<body%s>\n%s\n%s\n</body>\n</html>"
        % (esc(data_theme), esc(title), body_attr, topbar, body)
    )


def ui_data_theme(theme):
    """Ánh xạ khóa theme đã lưu sang giá trị data-theme.

    "light" -> "light" (Sáng Superhuman); "dark" -> "hades" (Tối
    Hades). Giá trị lạ/thiếu -> "light" (SPEC TT24; POST
    theme=hades KHÔNG hợp lệ, chỉ nhận "light"/"dark").
    """
    if theme == "dark":
        return "hades"
    return "light"


def login_page_html(error=None, login_csrf=""):
    """Trang đăng nhập (không topbar, không cần phiên)."""
    err = ""
    if error:
        err = '<div class="field-error">%s</div>' % esc(error)
    body = (
        "<h1>Đăng nhập</h1>"
        "<p>Nhập mật khẩu giao diện quản lý bot. Mật khẩu do chủ nhân "
        "đặt trên máy tính, không có mật khẩu mặc định.</p>"
        "%s"
        '<form method="post" action="/login">'
        '<input type="hidden" name="login_csrf" value="%s">'
        '<label for="password">Mật khẩu</label>'
        '<input type="password" id="password" name="password" '
        'autocomplete="current-password" required>'
        '<div class="mt-16">'
        '<button class="primary-action" type="submit">Đăng nhập</button>'
        "</div></form>"
        "<p class=\"hint\">Quá 5 lần sai trong 15 phút sẽ bị khóa thử "
        "tạm thời.</p>"
    ) % (err, esc(login_csrf))
    return layout("Đăng nhập — Bot của tôi", body, show_topbar=False)


def simple_page_html(title, body_html, csrf_token=None):
    """Trang thông báo chung (kết quả thao tác, lỗi...)."""
    return layout(title, body_html, csrf_token=csrf_token)


def _status_chip(summary):
    """Chip trạng thái chính của thẻ bot."""
    if summary["paused"]:
        return '<span class="chip yellow">TẠM DỪNG</span>'
    if not summary["enabled"]:
        return '<span class="chip gray">ĐANG TẮT</span>'
    if summary["flag_401"] or summary.get("token_state") == "WAIT_TOKEN":
        return '<span class="chip red">CHỜ TOKEN</span>'
    if summary["stale"] or summary.get("last_poll_ok") is False:
        return '<span class="chip red">KHÔNG LẤY ĐƯỢC TIN</span>'
    if summary["flag_429"]:
        return '<span class="chip yellow">ĐANG CHỜ (429)</span>'
    return '<span class="chip green">ĐANG NGHE</span>'


def _platform_label(platform):
    return {"zalo": "Zalo", "telegram": "Telegram"}.get(platform, esc(platform))


def _fmt_num(value):
    """Số có dấu phân cách hàng nghìn kiểu Việt Nam (1.240)."""
    try:
        return "{:,}".format(int(value)).replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def _platform_badge(platform):
    """Huy hiệu nền tảng màu rõ (phong cách C)."""
    if platform == "zalo":
        return '<span class="plat zalo">ZALO</span>'
    if platform == "telegram":
        return '<span class="plat telegram">TELEGRAM</span>'
    return '<span class="plat">%s</span>' % esc(str(platform).upper())


# Thứ tự tab cố định (V2 §1 + T3 v0.2.3 + T3i v0.6.0): Tất cả /
# Trung tâm / Chat / Zalo / Telegram / Cảnh báo. Cài đặt chung KHÔNG
# lên thanh tab (TT15). Tab Chat ngay sau tab Trung tâm (mô hình A,
# ĐỔI Q-T3-05).
TABS = (("all", "Tất cả"), ("center", "Trung tâm"), ("chat", "Chat"),
        ("zalo", "Zalo"), ("telegram", "Telegram"),
        ("alerts", "Cảnh báo"))


def tabs_nav_html(active_tab, open_alert_count=0, overdue_task_count=0,
                  center_unread_total=0, center_has_mention=False):
    """Thanh tab render phía máy chủ (link thường, không chuyển tab
    bằng JS). Huy hiệu trên tab Cảnh báo đếm cảnh báo đang mở chưa
    xác nhận — chỉ tín hiệu phía máy tính (UI5). Huy hiệu trên tab
    Trung tâm đếm việc quá hạn chưa xong — tính lại mỗi lần render
    fragment, theo mẫu huy hiệu tab Cảnh báo (T3-NS-11). Huy hiệu
    trên tab Chat (v0.6.0, T3i): tổng số tin chưa đọc mọi phòng +
    huy hiệu "@" khi có tin tag chủ nhân chưa đọc."""
    links = []
    for key, label in TABS:
        cls = "tab active" if key == active_tab else "tab"
        badge = ""
        if key == "alerts" and open_alert_count:
            badge = ' <span class="badge">%d</span>' % open_alert_count
        if key == "center" and overdue_task_count:
            badge = ' <span class="badge">%d</span>' % overdue_task_count
        if key == "chat":
            if center_unread_total:
                badge = (' <span class="badge" '
                         'data-chat-unread-badge>%d</span>'
                         % center_unread_total)
            if center_has_mention:
                badge += (' <span class="badge badge--mention" '
                          'data-chat-mention-badge>@</span>')
        links.append(
            '<a class="%s" data-tab="%s" href="/?tab=%s">%s%s</a>'
            % (cls, key, key, esc(label), badge))
    return '<nav class="tabs" aria-label="Tab">%s</nav>' % "".join(links)


def node_toggle_form_html(key, collapsed, csrf_token, next_url):
    """Form toggle 1 node (T3, DESIGN §8.2): form POST thường + redirect
    về trang đang xem. CẤM <details> làm toggle, CẤM handler inline."""
    target = "false" if collapsed else "true"
    label = "Mở" if collapsed else "Đóng"
    chevron = "▸" if collapsed else "▾"
    return (
        '<form class="inline" method="post" action="/api/ui-prefs">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<input type="hidden" name="node" value="%s">'
        '<input type="hidden" name="collapsed" value="%s">'
        '<input type="hidden" name="next" value="%s">'
        '<button class="node-toggle" type="submit" '
        'aria-label="%s node">%s %s</button>'
        "</form>"
    ) % (esc(csrf_token), esc(key), target, esc(next_url), esc(label),
         chevron, esc(label))


def open_close_all_html(tab, csrf_token, next_url):
    """Nút "Mở tất cả / Đóng tất cả" — CHỈ trong tab hiện tại (Q-T3-06)."""
    parts = []
    for action, label in (("open-all", "Mở tất cả"),
                          ("close-all", "Đóng tất cả")):
        parts.append(
            '<form class="inline" method="post" action="/api/ui-prefs">'
            '<input type="hidden" name="csrf_token" value="%s">'
            '<input type="hidden" name="action" value="%s">'
            '<input type="hidden" name="tab" value="%s">'
            '<input type="hidden" name="next" value="%s">'
            '<button type="submit">%s</button>'
            "</form>"
            % (esc(csrf_token), action, esc(tab), esc(next_url),
               esc(label)))
    return '<div class="node-actions">%s</div>' % "".join(parts)


def _node_badge_html(badge):
    """Huy hiệu số lượng (đợt mẫu rẻ Hades): chỉ render khi badge là
    số nguyên > 0; None/0 -> không huy hiệu (ẩn khi số đếm bằng 0)."""
    if isinstance(badge, int) and not isinstance(badge, bool) and badge > 0:
        return '<span class="node-badge">%d</span>' % badge
    return ""


def node_section_html(key, title, inner_html, prefs, csrf_token, next_url,
                      summary_html="", badge=None):
    """1 node bật/tắt (T3). ``prefs`` None -> hành vi cũ (luôn mở,
    không form toggle) để code gọi trực tiếp không kèm prefs vẫn chạy.
    Node đóng chỉ hiện tiêu đề + dòng tóm tắt (không render nội dung).
    ``title`` None -> lấy <h2> đầu tiên trong nội dung làm tiêu đề node
    (giữ nguyên chữ hiện có, không trùng tiêu đề). ``badge``: số đếm
    gắn lên tiêu đề (đợt mẫu rẻ Hades); None/0 -> không huy hiệu."""
    inner = inner_html
    head_title = title
    if inner.startswith("<h2>"):
        end = inner.find("</h2>")
        if end != -1:
            if head_title is None:
                head_title = inner[len("<h2>"):end]
            inner = inner[end + len("</h2>"):]
    if head_title is None:
        head_title = key
    if prefs is None:
        return "<h2>%s%s</h2>%s" % (esc(head_title),
                                    _node_badge_html(badge), inner)
    collapsed = bool(prefs.get(key))
    head = (
        '<div class="node-head"><h2>%s%s</h2>%s</div>'
        % (esc(head_title), _node_badge_html(badge),
           node_toggle_form_html(key, collapsed, csrf_token, next_url)))
    body = ""
    if collapsed:
        if summary_html:
            body = ('<div class="node-summary">%s</div>' % summary_html)
    else:
        body = inner
    return ('<section class="node" data-node="%s">%s%s</section>'
            % (esc(key), head, body))


def stats_strip_html(stats, platform=None):
    """Dải số tổng quan (V2 §1): số đếm cục bộ, KHÔNG phải thống kê
    token. ``platform`` tô viền trái ô số theo màu nền tảng."""
    boxes = [
        ("Tin đến hôm nay", stats.get("today_in", 0)),
        ("Tin đến 7 ngày", stats.get("week_in", 0)),
        ("Trả lời đã gửi", stats.get("sent", 0)),
        ("Đang tồn", stats.get("backlog", 0)),
        ("Thư chết", stats.get("dead", 0)),
    ]
    cls = "stats"
    if platform in ("zalo", "telegram"):
        cls += " " + platform
    parts = []
    for label, value in boxes:
        parts.append(
            '<div class="stat"><div class="num">%s</div>'
            '<div class="lab">%s</div></div>'
            % (_fmt_num(value), esc(label)))
    return '<div class="%s">%s</div>' % (cls, "".join(parts))


def bot_card_html(summary, csrf_token, compact=False):
    """1 thẻ bot 2 tầng (V2 §2, phong cách C): đủ 3 nhóm thông tin —
    tin hôm nay + số chat được phép, quota ước tính (chỉ Zalo), tin
    gần nhất + giờ lấy tin cuối. ``compact=True`` (tab Tất cả): bỏ
    tầng "Xem thêm", thêm link sang tab nền tảng của bot đó."""
    bot_id = summary["bot_id"]
    platform = summary.get("platform") or "?"
    last = summary.get("last_message") or {}
    last_poll = summary.get("last_poll_at")
    if summary.get("state_age_seconds") is not None:
        age_min = int(summary["state_age_seconds"] // 60)
        if age_min <= 0:
            poll_text = "Lấy tin lần cuối: dưới 1 phút trước"
        elif age_min == 1:
            poll_text = "Lấy tin lần cuối: 1 phút trước"
        else:
            poll_text = "Lấy tin lần cuối: %d phút trước" % age_min
    elif last_poll:
        poll_text = "Lấy tin lần cuối: %s" % esc(last_poll)
    else:
        poll_text = "Chưa lấy tin lần nào"
    preview = last.get("preview") or "—"
    error_text = ""
    if summary.get("last_error_class"):
        consec = summary.get("consec_err") or 0
        extra = ""
        if consec:
            extra = " · %d lỗi liên tiếp" % consec
        error_text = (
            '<div class="meta">Lỗi gần nhất: %s%s</div>'
            % (esc(summary["last_error_class"]), extra)
        )
    alerts = ""
    # N1 (doc 09 §5.4/R4): cảnh báo dual-fetch đang tạm ẩn theo luồng
    # chuyển thì không hiện chip trên thẻ bot — chỉ ẩn hiển thị, trạng
    # thái cảnh báo/since/lịch sử vẫn giữ nguyên.
    hidden_dual = summary_dual_fetch_hidden(summary)
    for alert in summary.get("alerts") or []:
        if alert.get("key") == "dual-fetch" and hidden_dual:
            continue
        # L7: cảnh báo "sync" không hiện chip trên từng thẻ nữa —
        # gom thành 1 banner chung trên node bot-cards (xem
        # sync_slow_banner_html); cảnh báo gốc trong tab Cảnh báo
        # vẫn giữ nguyên (đường render riêng, không qua đây).
        if alert.get("key") == "sync":
            continue
        alerts += (
            '<div><span class="chip %s">%s</span></div>'
            % (esc(alert["level"]), esc(alert["text"]))
        )
    quota_line = ""
    quota = summary.get("quota")
    if quota is not None:
        warn_chip = ""
        if quota.get("warn"):
            warn_chip = ' <span class="chip yellow">Sắp hết</span>'
        quota_line = (
            '<div class="kv">Quota ước tính: %s / %s (%s%%)%s</div>'
            % (_fmt_num(quota.get("used", 0)), _fmt_num(quota.get("limit", 0)),
               _fmt_num(quota.get("pct", 0)), warn_chip)
        )
    details = ""
    if not compact:
        sync = summary.get("sync") or {}
        if sync.get("mode") == "g3b":
            week_line = (
                '<div class="kv">7 ngày: %s tin đến · %s trả lời đã gửi · '
                "%s thư chết</div>"
            ) % (
                _fmt_num(summary.get("week_in", 0)),
                _fmt_num(summary.get("week_sent", 0)),
                _fmt_num(summary.get("week_dead", 0)),
            )
        else:
            # GĐ3a: chưa có dữ liệu trả lời — không hiện số trả lời
            # (hiện 0 sẽ nói sai thành "chưa trả lời tin nào").
            week_line = (
                '<div class="kv">7 ngày: %s tin đến</div>'
                % _fmt_num(summary.get("week_in", 0))
            )
        details = (
            "<details><summary>Xem thêm</summary>"
            '<div class="kv">Đồng bộ trả lời: %s</div>'
            "%s"
            "</details>"
        ) % (esc(sync.get("line") or "—"), week_line)
    # Nút điều khiển: khóa nút Bật khi chưa có allowlist (D5) hoặc
    # đang chờ đồng bộ về Muse (K3); lý do khóa hiện rõ trên thẻ.
    enable_disabled = ""
    enable_note = ""
    if not summary.get("has_allowlist"):
        enable_disabled = " disabled"
        enable_note = (
            '<div class="hint">Chưa bật được: bot chưa có danh sách '
            "chat được phép (D5). Thêm danh sách rồi mới bật.</div>"
        )
    elif summary.get("awaiting_muse_sync"):
        enable_disabled = " disabled"
        enable_note = (
            '<div class="hint">Chờ đồng bộ về Muse: nút Bật mở khi '
            "phía Muse lấy tin lần đầu thành công.</div>"
        )
    if summary["paused"]:
        pause_button = (
            '<button type="submit" name="action" value="resume">Bật lại</button>'
        )
    else:
        pause_button = (
            '<button type="submit" name="action" value="pause">Tạm dừng</button>'
        )
    if summary["enabled"]:
        power_button = (
            '<button type="submit" name="action" value="disable">Tắt</button>'
        )
    else:
        power_button = (
            '<button type="submit" name="action" value="enable"%s>Bật</button>'
            % enable_disabled
        )
    cardlink = ""
    if compact and platform in ("zalo", "telegram"):
        cardlink = (
            '<a class="cardlink" href="/?tab=%s">Xem trong tab %s →</a>'
            % (platform, _platform_label(platform))
        )
    # Nhãn chủ quản (MA2): 1 dòng trên thẻ, không để trống gây hiểu nhầm.
    owner_id = summary.get("owner_account")
    if owner_id:
        owner_name = summary.get("owner_display_name")
        if owner_name:
            account_line = (
                '<div class="kv">Tài khoản: %s</div>' % esc(owner_name))
        else:
            account_line = (
                '<div class="kv">Tài khoản: %s (chưa có trong danh sách '
                "tài khoản)</div>" % esc(owner_id))
    else:
        account_line = '<div class="kv">Chưa gắn tài khoản</div>'
    # Huy hiệu "Đang chuyển": chip cộng thêm, không đổi màu thẻ (N27).
    status_html = _status_chip(summary)
    transfer = summary.get("transfer")
    if transfer:
        try:
            step_no = int(transfer.get("current_step"))
        except (TypeError, ValueError):
            step_no = 0
        to_name = summary.get("transfer_to_display_name") or transfer.get(
            "to_account_id") or ""
        status_html += (
            ' <span class="chip yellow">Đang chuyển → %s (mốc %d trong '
            "0–9)</span>"
        ) % (esc(to_name), step_no)
    body = (
        '<div class="card" data-platform="%s">'
        "<h3>%s %s %s</h3>"
        '<div class="meta">%s</div>'
        "%s"
        '<div class="kv">Hôm nay: %s tin đến · %s chat được phép</div>'
        "%s"
        '<div class="kv">Tin gần nhất: %s</div>'
        '<div class="meta">%s · Tồn ước lượng: %s</div>'
        "%s%s%s"
        '<div class="actions">'
        '<form class="inline" method="post" action="/bots/%s/control">'
        '<input type="hidden" name="csrf_token" value="%s">'
        "%s%s"
        '<button type="submit" name="action" value="restart">Khởi động lại</button>'
        "</form>"
        '<a class="btn" href="/bots/%s">Chi tiết</a>'
        "</div>%s%s</div>"
    ) % (
        esc(platform),
        _platform_badge(platform),
        esc(summary["display_name"]),
        status_html,
        esc("tạm dừng" if summary["paused"] else (
            "đang bật" if summary["enabled"] else "đang tắt")),
        account_line,
        _fmt_num(summary.get("today_in", 0)),
        _fmt_num(summary.get("allowed_chat_count", 0)),
        quota_line,
        esc(preview),
        esc(poll_text),
        esc(summary["backlog_estimate"]),
        error_text,
        alerts,
        details,
        esc(bot_id),
        esc(csrf_token),
        power_button,
        pause_button,
        esc(bot_id),
        enable_note,
        cardlink,
    )
    return body


def _feed_row_html(item, names=None):
    """1 dòng hoạt động: giờ + bot + chat + tóm tắt + trạng thái."""
    when = format_day_time(item["ts"]) if item["ts"] else "--/-- --:--"
    chat = item.get("chat_title") or short_id(item["chat_id"])
    name = (names or {}).get(item["bot_id"], item["bot_id"])
    badge = ""
    platform = item.get("platform") or ""
    if platform in ("zalo", "telegram"):
        badge = " " + _platform_badge(platform)
    direction = "→ bot trả lời" if item["kind"] == "out" else "→ gửi tới bot"
    status = esc(item["status"])
    if item["kind"] == "in" and item.get("age_label"):
        status += " · %s" % esc(item["age_label"])
    if item.get("attention"):
        status += ' <span class="chip yellow">Cần chú ý</span>'
    return (
        '<div class="msg %s"><div class="head">%s · %s%s · %s %s</div>'
        '<div class="body">%s</div>'
        '<div class="status">%s</div></div>'
    ) % (
        "out" if item["kind"] == "out" else "",
        esc(when),
        esc(name),
        badge,
        esc(chat),
        esc(direction),
        esc(preview_text(item["text"], 120)),
        status,
    )


def _feed_rows_html(feed, names):
    rows = "".join(_feed_row_html(item, names) for item in feed)
    if not rows:
        rows = '<div class="notice">Chưa có hoạt động nào.</div>'
    return rows


def activity_filter_bar_html(tab, bots, selected_bot, direction, query,
                             accounts=None, selected_account=""):
    """Thanh lọc hoạt động (V2 §4): chip chiều + select bot + ô tìm
    kiếm. Form GET về "/": tab/dir giữ qua hidden input, bot/q do
    người dùng chọn/nhập. Ô tìm kiếm chỉ lọc trong danh sách đang
    hiện — ghi rõ ngay dưới thanh lọc. ``accounts`` (list
    (account_id, display_name)) bật thêm bộ lọc theo tài khoản (MA2):
    mặc định "Tất cả tài khoản"; lọc chỉ thu hẹp hiển thị."""
    chips = []
    for key, label in (("all", "Tất cả"), ("in", "Tin đến"),
                       ("out", "Trả lời"), ("attention", "Cần chú ý")):
        params = {"tab": tab, "dir": key}
        if selected_bot:
            params["bot"] = selected_bot
        if query:
            params["q"] = query
        if selected_account:
            params["account"] = selected_account
        cls = "chipbtn active" if key == direction else "chipbtn"
        chips.append(
            '<a class="%s" href="/?%s">%s</a>'
            % (cls, esc(urlencode(params)), esc(label)))
    options = ['<option value="">Tất cả bot</option>']
    for bot_id, name in bots:
        selected = " selected" if bot_id == selected_bot else ""
        options.append('<option value="%s"%s>%s</option>'
                       % (esc(bot_id), selected, esc(name)))
    account_select = ""
    if accounts is not None:
        account_options = ['<option value="">Tất cả tài khoản</option>']
        for account_id, name in accounts:
            selected = " selected" if account_id == selected_account else ""
            account_options.append('<option value="%s"%s>%s</option>'
                                   % (esc(account_id), selected, esc(name)))
        account_select = (
            '<label class="sr-only" for="account-filter">Tài khoản</label>'
            '<select id="account-filter" name="account">%s</select>'
        ) % "".join(account_options)
    return (
        '<div class="filters">%s'
        '<form class="inline" method="get" action="/">'
        '<input type="hidden" name="tab" value="%s">'
        '<input type="hidden" name="dir" value="%s">'
        "%s"
        '<label class="sr-only" for="bot-filter">Bot</label>'
        '<select id="bot-filter" name="bot">%s</select>'
        '<label class="sr-only" for="q-filter">Tìm trong danh sách</label>'
        '<input type="search" id="q-filter" name="q" value="%s" '
        'placeholder="Tìm trong danh sách đang hiện…">'
        '<button type="submit">Lọc</button>'
        "</form></div>"
        '<div class="hint">Ô tìm kiếm chỉ lọc trong danh sách đang hiện '
        "trên màn hình, không tìm toàn bộ lịch sử.</div>"
    ) % (
        "".join(chips),
        esc(tab),
        esc(direction),
        account_select,
        "".join(options),
        esc(query),
    )


def _tailscale_line(tailscale, summaries):
    total = len(summaries)
    alive = sum(
        1 for s in summaries
        if s["enabled"] and not s["stale"] and s.get("last_poll_ok")
    )
    if tailscale.get("up") is True:
        tailscale_text = "Tailscale: đang nối%s" % (
            " (%s)" % esc(tailscale["ip"]) if tailscale.get("ip") else "")
    elif tailscale.get("up") is False:
        tailscale_text = "Tailscale: chưa nối"
    else:
        tailscale_text = "Tailscale: không rõ"
    return (
        '<div class="meta">%s · Bot đang nghe: %d/%d</div>'
        % (esc(tailscale_text), alive, total)
    )


def sync_slow_banner_html(summaries):
    """L7: 1 banner chung cho các bot đồng bộ trả lời chậm (ngưỡng
    1800 s không đổi, nội dung giữ đúng cảnh báo gốc) — thay cho
    chip "sync" lặp trên từng thẻ bot. Trả "" khi không có bot nào
    chậm; đếm trên chính tab đang render (Tất cả: 5/5, Zalo/
    Telegram: chỉ bot của nền tảng đó)."""
    total = len(summaries or [])
    slow = 0
    for summary in summaries or []:
        alerts = summary.get("alerts") or []
        if any(isinstance(a, dict) and a.get("key") == "sync"
               for a in alerts):
            slow += 1
        elif (summary.get("sync") or {}).get("fallback"):
            slow += 1
    if not slow:
        return ""
    return (
        '<div class="banner">Đồng bộ trả lời chậm hơn 30 phút — '
        "%d/%d bot đang hiện giao diện rút gọn (chỉ tin đến, không "
        "hiện trả lời cũ như mới).</div>" % (slow, total)
    )


def overview_fragment_html(summaries, feed, unseen_alerts, tailscale,
                           csrf_token, stats, names, filters,
                           accounts=None, prefs=None, next_url=None,
                           node_summaries=None, sync_banner=""):
    """Fragment tab Tất cả (V2 §1): dải số + thẻ gọn + hoạt động.

    ``sync_banner``: banner chung L7 (do _dashboard_fragment tính
    từ cùng danh sách summaries), đặt ngay trên node bot-cards."""
    banner = ""
    if unseen_alerts:
        banner = (
            '<div class="banner red">⚠ %d cảnh báo chưa xem — bấm vào '
            "thẻ bot để xem chi tiết.</div>" % unseen_alerts
        )
    cards = "".join(bot_card_html(s, csrf_token, compact=True)
                    for s in summaries)
    if not cards:
        cards = (
            '<div class="notice">Chưa có bot nào. Bấm "Thêm bot" để '
            "tạo bot đầu tiên.</div>"
        )
    bots = [(s["bot_id"], s["display_name"]) for s in summaries]
    account_link = (
        '<div class="actions"><a class="btn" href="/accounts">Quản lý '
        'tài khoản</a><a class="btn" href="/settings">Cài đặt chung'
        "</a></div>"
    )
    summaries_map = node_summaries or {}
    all_actions = ""
    if prefs is not None:
        all_actions = open_close_all_html("all", csrf_token, next_url or "/")
    stats_node = node_section_html(
        "five-stats", "Dải 5 số", stats_strip_html(stats), prefs,
        csrf_token, next_url or "/",
        summaries_map.get("five-stats", ""))
    cards_node = node_section_html(
        "bot-cards", "Bot",
        '<div class="cards">%s</div>'
        '<div class="actions"><a class="btn" href="/bots/new">'
        "Thêm bot</a></div>" % cards,
        prefs, csrf_token, next_url or "/",
        summaries_map.get("bot-cards", ""), badge=len(summaries))
    activity_node = node_section_html(
        "activity", "Hoạt động mới nhất",
        "%s%s" % (
            activity_filter_bar_html(
                "all", bots, filters.get("bot", ""), filters.get("dir", "all"),
                filters.get("q", ""), accounts=accounts,
                selected_account=filters.get("account", "")),
            _feed_rows_html(feed, names)),
        prefs, csrf_token, next_url or "/",
        summaries_map.get("activity", ""),
        # Node đóng: lệnh đọc bị bỏ qua, feed=[] là 0 giả -> badge=None.
        badge=(len(feed) if prefs is None or not prefs.get("activity")
               else None))
    return (
        "%s%s%s"
        "%s"
        "%s%s%s%s"
    ) % (
        _tailscale_line(tailscale, summaries),
        banner,
        all_actions,
        stats_node,
        account_link,
        sync_banner,
        cards_node,
        activity_node,
    )


def platform_fragment_html(platform, summaries, feed, csrf_token, stats,
                           names, filters, accounts=None, prefs=None,
                           next_url=None, node_summaries=None,
                           sync_banner=""):
    """Fragment tab Zalo/Telegram (V2 §1): dải số riêng + thẻ đầy đủ
    + hoạt động của nền tảng đó. Bot của nền tảng khác không hiện.

    ``sync_banner``: banner chung L7 (do _dashboard_fragment tính
    từ cùng danh sách summaries), đặt ngay trên node bot-cards."""
    cards = "".join(bot_card_html(s, csrf_token) for s in summaries)
    if not cards:
        cards = (
            '<div class="notice">Chưa có bot %s nào.</div>'
            % esc(_platform_label(platform))
        )
    bots = [(s["bot_id"], s["display_name"]) for s in summaries]
    summaries_map = node_summaries or {}
    all_actions = ""
    if prefs is not None:
        all_actions = open_close_all_html(
            platform, csrf_token, next_url or "/")
    stats_node = node_section_html(
        "five-stats", "Dải 5 số", stats_strip_html(stats, platform=platform),
        prefs, csrf_token, next_url or "/",
        summaries_map.get("five-stats", ""))
    cards_node = node_section_html(
        "bot-cards", "Bot %s" % _platform_label(platform),
        '<div class="cards">%s</div>' % cards,
        prefs, csrf_token, next_url or "/",
        # NS-04: đếm đúng bot nền tảng này — tham số ``summaries`` ở đây
        # đã là danh sách bot của nền tảng (gọi với tab_summaries).
        summaries_map.get("bot-cards", ""), badge=len(summaries))
    activity_node = node_section_html(
        "activity", "Hoạt động mới nhất",
        "%s%s" % (
            activity_filter_bar_html(
                platform, bots, filters.get("bot", ""),
                filters.get("dir", "all"), filters.get("q", ""),
                accounts=accounts,
                selected_account=filters.get("account", "")),
            _feed_rows_html(feed, names)),
        prefs, csrf_token, next_url or "/",
        summaries_map.get("activity", ""),
        # Node đóng: lệnh đọc bị bỏ qua, feed=[] là 0 giả -> badge=None.
        badge=(len(feed) if prefs is None or not prefs.get("activity")
               else None))
    return (
        "%s%s%s%s%s%s"
    ) % (
        all_actions,
        stats_node,
        '<div class="hint">Quota ước tính hiện trên từng thẻ bot, '
        "không cộng gộp.</div>",
        sync_banner,
        cards_node,
        activity_node,
    )


def alerts_fragment_html(open_alerts_list, events, names, platforms,
                         csrf_token, filters, prefs=None, next_url=None,
                         node_summaries=None, center_stuck=None,
                         center_view_overdue=None,
                         center_discussion_stopped=None):
    """Fragment tab Cảnh báo (V2 §5): đang mở (kèm nút xác nhận) hoặc
    lịch sử 30 ngày; lọc theo bot + nền tảng.

    (v0.6.0, T3i — D4) ``center_stuck``: list phong chat tac
    {room, label, waiting_count, oldest_wait_seconds, level} — hien
    1 dong moi phong o muc trung tam rieng (chi o view "Đang mở",
    khong nut xac nhan, khong vao huy hieu tab).

    (v0.6.0, T3l — doc 10 §3.8 §4b) ``center_discussion_stopped``:
    list vòng thảo luận đã dừng {room, label, opener, participants,
    count, level} — hien 1 dong moi vong o muc trung tam rieng, muc
    "thông tin" (chi o view "Đang mở", khong nut xac nhan, khong vao
    huy hieu tab, khong am thanh; dung chung mau dong canh bao chat
    tac cua T3i, khong tao pipeline canh bao moi).

    (v0.6.0, T3j — Q-V060-26) ``center_view_overdue``: list tai
    khoan da xem ma qua 24 gio chua xoay {account_id, display_name,
    viewed_at, overdue_seconds} — hien 1 dong moi tai khoan o muc
    trung tam rieng, muc "nghiêm trọng", kem link xoay ma (chi o
    view "Đang mở", khong nut xac nhan, khong vao huy hieu tab)."""
    view = filters.get("view", "open")
    bots = sorted(names.items(), key=lambda pair: pair[1].lower())
    bot_options = ['<option value="">Tất cả bot</option>']
    for bot_id, name in bots:
        selected = " selected" if bot_id == filters.get("bot", "") else ""
        bot_options.append('<option value="%s"%s>%s</option>'
                           % (esc(bot_id), selected, esc(name)))
    platform = filters.get("platform", "")
    plat_options = ""
    for value, label in (("", "Tất cả nền tảng"), ("zalo", "Zalo"),
                         ("telegram", "Telegram")):
        selected = " selected" if value == platform else ""
        plat_options += '<option value="%s"%s>%s</option>' % (
            value, selected, esc(label))
    view_links = []
    for key, label in (("open", "Đang mở"), ("history", "Lịch sử")):
        params = {"tab": "alerts", "view": key}
        if filters.get("bot"):
            params["bot"] = filters["bot"]
        if filters.get("platform"):
            params["platform"] = filters["platform"]
        # NS-01: chỉ giữ need_owner khi đích là view "open"; link sang
        # lịch sử không mang tham số này (lịch sử không dùng nhãn).
        if key == "open" and filters.get("need_owner"):
            params["need_owner"] = filters["need_owner"]
        cls = "chipbtn active" if key == view else "chipbtn"
        view_links.append(
            '<a class="%s" href="/?%s">%s</a>'
            % (cls, esc(urlencode(params)), esc(label)))
    # NS-02: checkbox lọc "cần chủ nhân" (chỉ ở view open). N = số cảnh
    # báo cần chủ nhân trong danh sách đã lọc bot/nền tảng; khi lọc
    # need_owner đang bật thì danh sách chỉ còn các cảnh báo đó nên N
    # vẫn đúng (bằng số dòng đang hiện).
    need_owner_box = ""
    if view == "open":
        needs_owner_count = sum(
            1 for alert in open_alerts_list if alert_needs_owner(alert))
        checked = " checked" if filters.get("need_owner") == "1" else ""
        need_owner_box = (
            '<label class="need-owner"><input type="checkbox" '
            'name="need_owner" value="1"%s> Chỉ cảnh báo cần chủ nhân '
            "(%d)</label>" % (checked, needs_owner_count))
    bar = (
        '<div class="filters">%s'
        '<form class="inline" method="get" action="/">'
        '<input type="hidden" name="tab" value="alerts">'
        '<input type="hidden" name="view" value="%s">'
        '<label class="sr-only" for="alert-bot">Bot</label>'
        '<select id="alert-bot" name="bot">%s</select>'
        '<label class="sr-only" for="alert-platform">Nền tảng</label>'
        '<select id="alert-platform" name="platform">%s</select>'
        '%s<button type="submit">Lọc</button>'
        "</form></div>"
    ) % (
        "".join(view_links),
        esc(view),
        "".join(bot_options),
        plat_options,
        need_owner_box,
    )
    summaries_map = node_summaries or {}
    all_actions = ""
    if prefs is not None:
        all_actions = open_close_all_html(
            "alerts", csrf_token, next_url or "/?tab=alerts")
    if view == "history":
        rows = ""
        for event in events:
            rows += _alert_event_row_html(event, names, platforms)
        if not rows:
            rows = ('<div class="notice">Chưa có sự kiện cảnh báo nào '
                    "trong 30 ngày.</div>")
        body = node_section_html(
            "event-history-30d", "Lịch sử sự kiện 30 ngày", rows, prefs,
            csrf_token, next_url or "/?tab=alerts",
            summaries_map.get("event-history-30d", ""),
            # Node đóng: lệnh đọc bị bỏ qua, events=[] là 0 giả.
            badge=(len(events) if prefs is None
                   or not prefs.get("event-history-30d") else None))
        return "%s<h2>Cảnh báo</h2>%s%s" % (all_actions, bar, body)
    rows = ""
    for alert in open_alerts_list:
        rows += _open_alert_row_html(alert, csrf_token, filters)
    if not rows:
        # NS-03: lọc cần chủ nhân bật mà không khớp dòng nào -> text
        # riêng, phân biệt với "thật sự không có cảnh báo".
        if filters.get("need_owner") == "1":
            rows = ('<div class="notice">Không có cảnh báo nào cần chủ '
                    "nhân đang mở.</div>")
        else:
            rows = '<div class="notice">Không có cảnh báo nào đang mở.</div>'
    body = node_section_html(
        "open-alerts", "Cảnh báo đang mở", rows, prefs, csrf_token,
        next_url or "/?tab=alerts",
        summaries_map.get("open-alerts", ""), badge=len(open_alerts_list))
    center_body = ""
    if center_stuck:
        center_rows = ""
        for item in center_stuck:
            wait = item.get("oldest_wait_seconds")
            if isinstance(wait, int):
                wait_text = ("%d phút" % (wait // 60) if wait >= 60
                             else "%d giây" % wait)
            else:
                wait_text = "không rõ bao lâu"
            center_rows += (
                '<div class="alert">Chat tắc: phòng '
                '<a href="/?tab=chat&amp;room=%s">%s</a> — %d tin chờ '
                "trả lời, tin cũ nhất chờ %s (mức %s)</div>"
                % (esc(quote(item.get("room") or "")),
                   esc(item.get("label") or item.get("room") or ""),
                   item.get("waiting_count") or 0, esc(wait_text),
                   esc(item.get("level") or "cần xem")))
        center_body = (
            "<h2>Trung tâm — chat tắc</h2>%s" % center_rows)
    if center_view_overdue:
        overdue_rows = ""
        for item in center_view_overdue:
            overdue_rows += (
                '<div class="alert">Mã của tài khoản <strong>%s</strong> '
                "(%s) đã xem lúc %s — quá 24 giờ chưa xoay (mức nghiêm "
                "trọng). <a href=\"/center/accounts/%s/rotate-secret\">"
                "Xoay mã mới ngay</a>.</div>"
                % (esc(item.get("display_name")
                       or item.get("account_id") or ""),
                   esc(item.get("account_id") or ""),
                   esc(format_day_time_saigon(item.get("viewed_at"))),
                   esc(item.get("account_id") or "")))
        center_body += (
            "<h2>Trung tâm — mã đã xem quá hạn xoay</h2>%s"
            % overdue_rows)
    if center_discussion_stopped:
        stopped_rows = ""
        for item in center_discussion_stopped:
            stopped_rows += (
                '<div class="alert">Vòng thảo luận đã dừng: phòng '
                '<a href="/?tab=chat&amp;room=%s">%s</a> — %s '
                "(đủ %d lượt, mức %s).</div>"
                % (esc(quote(item.get("room") or "")),
                   esc(item.get("label") or item.get("room") or ""),
                   esc(item.get("names") or ""),
                   item.get("count") or 0,
                   esc(item.get("level") or "thông tin")))
        center_body += (
            "<h2>Trung tâm — vòng thảo luận đã dừng</h2>%s"
            % stopped_rows)
    return "%s<h2>Cảnh báo</h2>%s%s%s" % (
        all_actions, bar, center_body, body)


def _open_alert_row_html(alert, csrf_token, filters):
    """1 cảnh báo đang mở + nút xác nhận (POST /alerts/ack)."""
    severity = SEVERITY_LABELS.get(alert["level"], "Cảnh báo")
    name = esc(alert["display_name"])
    since = format_day_time(alert["since"])
    ack_hint = ""
    if alert["key"] == "dual-fetch":
        # Nhắc ack hết hạn 24h (doc 09 §5.4): trong 24h, trùng lấy mới
        # cũng bị ẩn.
        ack_hint = (
            '<div class="hint">Xác nhận cảnh báo này sẽ ẩn nó trong '
            "24 giờ — trong 24h, trùng lấy mới cũng bị ẩn. Quá 24 giờ, "
            "cảnh báo hiện lại nếu 2 nhãn vẫn cùng lấy.</div>"
        )
    hidden = (
        '<input type="hidden" name="bot_id" value="%s">'
        '<input type="hidden" name="key" value="%s">'
        '<input type="hidden" name="view" value="%s">'
        '<input type="hidden" name="bot" value="%s">'
        '<input type="hidden" name="platform" value="%s">'
        '<input type="hidden" name="need_owner" value="%s">'
    ) % (
        esc(alert["bot_id"]),
        esc(alert["key"]),
        esc(filters.get("view", "open")),
        esc(filters.get("bot", "")),
        esc(filters.get("platform", "")),
        esc(filters.get("need_owner", "")),
    )
    # Nhãn "cần chủ nhân" (đợt mẫu rẻ Hades): chỉ ở view Đang mở.
    owner_chip = ('<span class="chip red">CẦN CHỦ NHÂN</span> '
                  if alert_needs_owner(alert) else "")
    return (
        '<div class="alert" data-platform="%s">'
        '<span class="chip %s">%s</span> %s<strong>%s</strong>'
        "<div>%s</div>"
        '<div class="meta">Từ %s · %s</div>'
        "%s"
        '<form class="inline" method="post" action="/alerts/ack">'
        '<input type="hidden" name="csrf_token" value="%s">'
        "%s"
        '<button type="submit">Đã xem / Xác nhận</button>'
        "</form></div>"
    ) % (
        esc(alert["platform"]),
        esc(alert["level"]),
        esc(severity),
        owner_chip,
        name,
        esc(alert["text"]),
        esc(since),
        esc(_platform_label(alert["platform"])),
        ack_hint,
        esc(csrf_token),
        hidden,
    )


def _alert_event_row_html(event, names, platforms):
    """1 sự kiện trong lịch sử cảnh báo (không có nút — chỉ xem)."""
    kind = event["kind"]
    if kind == "raised":
        level = event.get("level") or "yellow"
        chip = '<span class="chip %s">%s</span>' % (
            esc(level), esc(SEVERITY_LABELS.get(level, "Cảnh báo")))
        text = esc(event["text"])
    elif kind == "cleared":
        chip = '<span class="chip blue">Thông tin</span>'
        text = "Đã hết cảnh báo: %s" % esc(event["text"])
    else:  # acked
        chip = '<span class="chip gray">Đã xác nhận</span>'
        text = "Đã xác nhận: %s" % esc(event["text"])
    name = names.get(event["bot_id"], event["bot_id"])
    platform = platforms.get(event["bot_id"], "?")
    when = format_day_time(event["ts"])
    return (
        '<div class="alert" data-platform="%s">%s <strong>%s</strong>'
        "<div>%s</div>"
        '<div class="meta">%s · %s</div></div>'
    ) % (
        esc(platform),
        chip,
        esc(name),
        text,
        esc(when),
        esc(_platform_label(platform)),
    )


def dashboard_page_html(fragment_html, csrf_token, fragment_url=None,
                        static_html="", notice_banner=""):
    """Trang chính đầy đủ, fragment tự làm tươi mỗi 5 giây.

    ``fragment_url`` mang đủ tham số tab/lọc hiện tại để polling giữ
    nguyên chế độ xem; mặc định là fragment tab Tất cả. ``static_html``
    render NGOÀI div fragment (không bị polling 5 giây ghi đè) — dùng
    cho khung chat của tab Chat + form tạo việc của tab Trung tâm
    (T3-CH-06).

    (L6, 21/11) ``notice_banner``: HTML banner da render san (lop
    ``.banner``), dat NGOAI div fragment — polling khong xoa; chi
    hien khi URL co ``notice`` hop le (query param 1 lan), tai lai
    trang khong co ``notice`` thi khong hien lai."""
    if fragment_url is None:
        fragment_url = "/fragments/dashboard"
    body = (
        "<h1>Bot của tôi</h1>"
        "%s"
        '<div data-fragment-url="%s" '
        'data-interval="5000">%s</div>'
        "%s"
    ) % (notice_banner, esc(fragment_url), fragment_html, static_html)
    return layout("Bot của tôi", body, csrf_token=csrf_token,
                  body_class="has-tabs")


# ---------------------------------------------------------------------------
# Tab Trung tâm (T3, v0.2.3 — SPEC TT11, WEB-UI.md §8.1).
# Thứ tự trong tab: tổng quan -> việc (+ lọc) -> tài khoản -> dòng dẫn
# "Mở khung chat →" -> form tạo việc. Các mục MỞ HẾT, không phải node
# bật/tắt (Q-T3-08). Khung chat đã rời sang tab Chat riêng (v0.6.0,
# T3i — ĐỔI Q-T3-05); form tạo việc render NGOÀI fragment 5 giây
# (T3-CH-06).
# ---------------------------------------------------------------------------

CENTER_TASK_STATUS_LABELS = {
    "moi": "Mới", "da-nhan": "Đã nhận", "dang-lam": "Đang làm",
    "da-bao-xong": "Đã báo xong", "da-xac-nhan": "Đã xác nhận",
    "huy": "Đã hủy",
}
CENTER_TASK_KINDS = (
    ("app", "App"), ("nghien-cuu", "Nghiên cứu"), ("viet", "Viết"),
    ("vanhanh", "Vận hành"), ("khac", "Khác"),
)
CENTER_TASK_KIND_LABELS = dict(CENTER_TASK_KINDS)
CENTER_MESSAGE_KIND_LABELS = {
    "giao-viec": "Giao việc", "hoi": "Hỏi", "bao-cao": "Báo cáo",
}
CENTER_TERMINAL_STATUSES = ("da-xac-nhan", "huy")

# Tên hiển thị 13 node (bảng TT11) — dùng ở trang Cài đặt chung.
UI_NODE_NAMES = (
    ("five-stats", "Dải 5 số (trang chính)"),
    ("bot-cards", "Thẻ bot (trang chính)"),
    ("activity", "Hoạt động mới nhất (30 dòng)"),
    ("open-alerts", "Cảnh báo đang mở (tab Cảnh báo)"),
    ("event-history-30d", "Lịch sử sự kiện 30 ngày (tab Cảnh báo)"),
    ("health", "Sức khỏe (trang chi tiết bot)"),
    ("controls", "Điều khiển (trang chi tiết bot)"),
    ("config", "Cấu hình (trang chi tiết bot)"),
    ("stats-7d", "Thống kê 7 ngày (trang chi tiết bot)"),
    ("alerts-history-detail",
     "Lịch sử cảnh báo 30 ngày (trang chi tiết bot)"),
    ("fetches", "Lượt lấy gần đây (trang chi tiết bot)"),
    ("message-history", "Lịch sử tin (50 mục/trang)"),
    ("ear-log", "Log tai nghe (50 dòng)"),
)


def _center_task_overdue(task, now):
    """Quá hạn = now > due_at và chưa ở trạng thái cuối (TT5)."""
    if task.get("status") in CENTER_TERMINAL_STATUSES:
        return False
    due = parse_iso_to_epoch(task.get("due_at"))
    return due is not None and now > due


def center_overview_html(health):
    """Dải tổng quan trung tâm: tài khoản, việc mở, việc quá hạn,
    tin mới nhất."""
    last = health.get("last_write_at")
    last_text = "—"
    if last:
        epoch = parse_iso_to_epoch(last)
        if epoch is not None:
            last_text = format_day_time(epoch)
    boxes = [
        ("Tài khoản tham gia", health.get("accounts", 0)),
        ("Việc đang mở", health.get("open_tasks", 0)),
        ("Việc quá hạn", health.get("overdue_tasks", 0)),
        ("Tin mới nhất", last_text),
    ]
    parts = []
    for label, value in boxes:
        if isinstance(value, int):
            value = _fmt_num(value)
        parts.append(
            '<div class="stat"><div class="num">%s</div>'
            '<div class="lab">%s</div></div>'
            % (esc(value), esc(label)))
    return '<div class="stats">%s</div>' % "".join(parts)


def center_task_row_html(task, account_names, now, orphan=False,
                         suspended_count=0):
    """1 dòng việc: tiêu đề cắt 40 ký tự + chip trạng thái/loại/
    người nhận + hạn + huy hiệu quá hạn. Việc mồ côi (Q-AM-06,
    ``orphan=True``) hiện nhắc "Cần chuyển tay" — chỉ là nhãn hiển thị,
    không tự chuyển việc. Việc chung (v0.4.0): người nhận hiện dạng
    danh sách + chip "x/y đã báo xong"; việc chưa có hạn hiện "chưa
    có hạn"; việc chỉ 1 phần người làm việc bị ngưng
    (``suspended_count`` > 0) hiện nhắc "N người đã ngưng — xem chi
    tiết"."""
    title = preview_text(task.get("title") or "", 40)
    status = task.get("status") or ""
    kind = task.get("kind") or ""
    assigned = task.get("assigned_to") or ""
    if isinstance(assigned, str):
        assigned = [assigned] if assigned else []
    assigned_names = ", ".join(
        account_names.get(a, a) for a in assigned)
    due = parse_iso_to_epoch(task.get("due_at"))
    due_text = format_day_time(due) if due is not None else "chưa có hạn"
    overdue = _center_task_overdue(task, now)
    overdue_chip = (
        ' <span class="chip red">Quá hạn</span>' if overdue else "")
    orphan_chip = (
        ' <span class="chip yellow">Cần chuyển tay</span>' if orphan else "")
    reported_chip = ""
    if len(assigned) > 1:
        reported = task.get("reported_by") or []
        reported_chip = (
            ' <span class="chip gray">%d/%d đã báo xong</span>'
            % (len([a for a in reported if a in assigned]),
               len(assigned)))
    suspended_note = ""
    if suspended_count:
        suspended_note = (
            ' <span class="chip yellow">%d người đã ngưng — xem chi '
            "tiết</span>" % suspended_count)
    return (
        '<div class="msg">'
        '<span class="chip %s">%s</span>'
        '<span class="chip gray">%s</span>%s%s%s%s'
        ' <a href="/center/tasks/%s"><strong>%s</strong></a>'
        '<div class="meta">Giao cho %s · Hạn %s</div>'
        "</div>"
    ) % (
        "green" if status == "da-xac-nhan" else
        "red" if overdue else "blue",
        esc(CENTER_TASK_STATUS_LABELS.get(status, status)),
        esc(CENTER_TASK_KIND_LABELS.get(kind, kind)),
        overdue_chip,
        orphan_chip,
        reported_chip,
        suspended_note,
        esc(task.get("task_id") or ""),
        esc(title),
        esc(assigned_names),
        esc(due_text),
    )


def center_filter_bar_html(filters, accounts):
    """Form lọc danh sách việc (GET, giữ tab=center). Chỉ lọc trong
    danh sách đang hiện — không tìm toàn văn lịch sử."""
    status_options = ['<option value="">Tất cả trạng thái</option>']
    for value, label in CENTER_TASK_STATUS_LABELS.items():
        selected = " selected" if filters.get("status") == value else ""
        status_options.append('<option value="%s"%s>%s</option>'
                              % (esc(value), selected, esc(label)))
    account_options = ['<option value="">Tất cả tài khoản</option>']
    for account_id, name in accounts:
        selected = " selected" if filters.get("account") == account_id \
            else ""
        account_options.append('<option value="%s"%s>%s</option>'
                               % (esc(account_id), selected, esc(name)))
    kind_options = ['<option value="">Tất cả loại</option>']
    for value, label in CENTER_TASK_KINDS:
        selected = " selected" if filters.get("kind") == value else ""
        kind_options.append('<option value="%s"%s>%s</option>'
                            % (esc(value), selected, esc(label)))
    overdue_checked = " checked" if filters.get("overdue") else ""
    return (
        '<form class="filters" method="get" action="/">'
        '<input type="hidden" name="tab" value="center">'
        '<label class="sr-only" for="center-status">Trạng thái</label>'
        '<select id="center-status" name="status">%s</select>'
        '<label class="sr-only" for="center-account">Tài khoản</label>'
        '<select id="center-account" name="account">%s</select>'
        '<label class="sr-only" for="center-kind">Loại</label>'
        '<select id="center-kind" name="kind">%s</select>'
        '<label><input type="checkbox" name="overdue" value="1"%s> '
        "Chỉ quá hạn</label>"
        '<label class="sr-only" for="center-q">Tìm trong danh sách</label>'
        '<input type="search" id="center-q" name="q" value="%s" '
        'placeholder="Tìm trong danh sách đang hiện">'
        '<button type="submit">Lọc</button>'
        "</form>"
    ) % ("".join(status_options), "".join(account_options),
         "".join(kind_options), overdue_checked,
         esc(filters.get("q", "")))


def _center_account_row_html(entry):
    """1 dòng tài khoản trên tab Trung tâm: đủ 4 mục (Q-AM-03) + mã bí
    mật (Q-AM-07). ``entry`` là dict (mới) hoặc tuple 4 phần tử cũ
    (account_id, tên, participating, seen_text) để code gọi trực tiếp
    kiểu cũ vẫn chạy.

    Dict mới: account_id, display_name, participating,
    last_fetch_epoch (None = chưa từng kéo), open_count, overdue_count,
    secret_has, secret_issued_epoch (None = chưa có mã). Mã bí mật CHỈ
    hiện "đã có"/"chưa có" + ngày cấp — KHÔNG hiện giá trị mã (TS3).
    Nút "Cấp lại mã mới" dẫn tới trang xác nhận xoay mã (2 bước).

    (v0.6.0, T3j — ngoại lệ F18(3)): nút "Xem mã" cạnh nút xoay —
    chỉ khi đã có mã + đã đặt mật khẩu xem mã + tài khoản đang tham
    gia hoặc tạm ngưng (không gỡ hẳn — khóa ``center_removed``);
    chưa đặt mật khẩu xem mã thì thay bằng dòng chữ nhỏ "Chưa đặt
    mật khẩu xem mã". ``viewed_at`` (epoch) + ``view_overdue``
    (bool): dòng nhắc "đã xem — chưa xoay" cho tới khi xoay mã mới;
    quá 24 giờ thì dòng nhắc chuyển màu đỏ (class alert)."""
    if isinstance(entry, dict):
        account_id = entry.get("account_id") or ""
        name = entry.get("display_name") or account_id
        participating = bool(entry.get("participating"))
        last_epoch = entry.get("last_fetch_epoch")
        open_count = entry.get("open_count") or 0
        overdue_count = entry.get("overdue_count") or 0
        secret_has = bool(entry.get("secret_has"))
        secret_issued = entry.get("secret_issued_epoch")
        view_password_set = bool(entry.get("view_password_set"))
        viewed_at = entry.get("viewed_at")
        view_overdue = bool(entry.get("view_overdue"))
        center_removed = bool(entry.get("center_removed"))
    else:
        account_id, name, participating, seen_text = entry
        return (
            '<div class="msg">%s <strong>%s</strong> '
            '<span class="meta">%s · %s</span> '
            '<a href="/accounts#%s">Xem thẻ tài khoản</a></div>'
        ) % (
            ('<span class="chip green">Tham gia</span>'
             if participating else
             '<span class="chip gray">Chưa tham gia</span>'),
            esc(name), esc(account_id), esc(seen_text),
            esc(account_id))
    chip = ('<span class="chip green">Tham gia</span>'
            if participating else
            '<span class="chip gray">Chưa tham gia</span>')
    _is_active, status_label = center_fetch_status(last_epoch)
    if last_epoch is not None:
        fetch_text = "Lần kéo cuối: %s · %s" % (
            format_day_time_saigon(last_epoch), status_label)
    else:
        fetch_text = "Chưa thấy kéo"
    counts_text = "Đang làm: %d việc · Quá hạn: %d việc" % (
        open_count, overdue_count)
    if secret_has:
        issued = format_date_saigon(secret_issued) \
            if secret_issued is not None else "không rõ ngày"
        secret_text = "Mã bí mật: đã có · Cấp ngày %s" % issued
        rotate_label = "Cấp lại mã mới"
    else:
        secret_text = "Mã bí mật: chưa có"
        rotate_label = "Cấp mã mới"
    # (T3j) Nút "Xem mã" / dòng "Chưa đặt mật khẩu xem mã" / dòng
    # nhắc "đã xem — chưa xoay" (chỉ khi đã có mã; tài khoản đã gỡ
    # hẳn thì không có nút — route cũng từ chối phía máy chủ).
    view_link = ""
    view_hint = ""
    if secret_has and not center_removed:
        if view_password_set:
            view_link = (
                '<a class="btn" href="/center/accounts/%s/view-secret">'
                "Xem mã</a> " % esc(account_id))
        else:
            view_hint = (
                '<div class="meta"><span class="hint">Chưa đặt mật khẩu '
                "xem mã</span></div>")
    viewed_html = ""
    if viewed_at is not None:
        viewed_text = ("Đã xem mã lúc %s — chưa xoay mã mới."
                       % format_day_time_saigon(viewed_at))
        if view_overdue:
            viewed_html = (
                '<div class="alert">%s Quá 24 giờ chưa xoay — xoay mã '
                "mới ngay.</div>" % esc(viewed_text))
        else:
            viewed_html = ('<div class="meta">%s</div>'
                           % esc(viewed_text))
    return (
        '<div class="msg">%s <strong>%s</strong>'
        '<div class="meta">%s</div>'
        '<div class="meta">%s</div>'
        '<div class="meta">%s</div>'
        '<div class="meta">%s</div>'
        '%s%s'
        '<div class="actions">'
        '<a class="btn" href="/center/accounts/%s/rotate-secret">'
        "%s</a> "
        '%s<a href="/accounts#%s">Xem thẻ tài khoản</a></div></div>'
    ) % (chip, esc(name), esc(account_id), esc(fetch_text),
         esc(counts_text), esc(secret_text), viewed_html, view_hint,
         esc(account_id), esc(rotate_label), view_link,
         esc(account_id))


def view_secret_password_page_html(account_id, display_name, csrf_token,
                                   confirm_token, paused=False,
                                   transfer_active=False, locked=False,
                                   overdue=False, viewed_at=None):
    """Trang nhập mật khẩu xem mã (v0.6.0, T3j — route 1, DESIGN
    §3.11 bước 2). Trang riêng, không phải hộp thoại nổi. Form
    urlencoded mang CSRF + confirm token 1 lần (5 phút)."""
    warnings = (
        # HM (21/11, C-01): cau cu "hien 1 lan duy nhat" sai voi hanh
        # vi moi — trang hien ma co nut "Hien lai ma" trong luc mo.
        "<p>Mã sẽ tự ẩn sau 30 giây — bấm “Hiện lại mã” để xem lại "
        "trong lúc trang còn mở; đóng trang hoặc tải lại thì không "
        "xem lại được nữa, phải xoay mã mới.</p>"
        "<p>Xem xong phải xoay mã mới ngay — mã cũ sẽ chết.</p>"
        "<p>Lần xem này được ghi vào nhật ký (chỉ ghi thời điểm, "
        "không ghi mã).</p>")
    extra = ""
    if paused:
        extra += ("<p>Tài khoản đang tạm ngưng — mã vẫn còn hiệu lực, "
                  "bật lại là chạy ngay.</p>")
    if transfer_active:
        extra += ("<p>Đang có luồng chuyển bot — kiểm tra đúng tài "
                  "khoản trước khi chép mã.</p>")
    if locked:
        extra += ("<p>Thử quá nhiều lần. Đợi 15 phút rồi thử lại.</p>")
    if overdue and viewed_at is not None:
        extra += (
            "<p>Đã xem mã lúc %s — quá 24 giờ chưa xoay, xoay mã mới "
            "ngay trước khi xem lại.</p>"
            % esc(format_day_time_saigon(viewed_at)))
    body = (
        "<h1>Xem mã bí mật — %s</h1>%s%s"
        '<form method="post" action="/center/accounts/%s/'
        'view-secret/execute">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<input type="hidden" name="confirm_token" value="%s">'
        '<label for="view-password">Mật khẩu xem mã</label>'
        '<input id="view-password" type="password" name="view_password" '
        'autocomplete="off" maxlength="2000">'
        '<div class="actions"><button class="primary-action" '
        'type="submit">Xem mã</button>'
        '<a class="btn" href="/?tab=center">Quay lại tab Trung tâm</a>'
        "</div></form>"
    ) % (esc(display_name or account_id), warnings, extra,
         esc(account_id), esc(csrf_token), esc(confirm_token))
    return layout("Xem mã bí mật — %s" % (display_name or account_id),
                  body, csrf_token=csrf_token)


def view_secret_page_html(account_id, display_name, fetch_secret,
                          push_secret, viewed_at, transfer_active=False,
                          issue_show=False, intro_html=None,
                          show_again=True):
    """Trang hiện mã (v0.6.0, T3j — response route 2 khi đúng mật
    khẩu, DESIGN §3.11 bước 4). HTML phía máy chủ, không JSON; mã
    hiện bằng text thường, không che; header no-store do handler
    gắn (không nằm trong HTML). Giá trị mã được esc khi render.
    JS đếm ngược + xóa mã khỏi trang nằm trong APP_JS chung (chỉ là
    tiện ích — rào chắn thật là cờ "đã xem" phía máy chủ).

    (HM 21/11 — hien ma khi cap, D-HM-01/D-HM-25) ``issue_show=True``:
    trang kết quả sau cấp/xoay (tái dùng hàm này, không viết hàm
    trang thứ 2): tiêu đề "Mã bí mật mới", KHÔNG link "Xoay mã mới
    ngay", dòng chữ theo chế độ cấp. ``viewed_at`` ở chế độ này là
    thời điểm cấp (``now`` lúc render), không phải "đã xem".
    ``intro_html``: câu §3.7(c)/(d), render TRÊN khối mã.
    ``show_again``: False thì không render nút "Hiện lại mã" (chỉ
    dùng ở đường dự phòng §3.3b khi chủ nhân không duyệt câu F18(3)
    mới — đường chính luôn True)."""
    extra = ""
    if transfer_active:
        extra = ("<p>Đang có luồng chuyển bot — kiểm tra đúng tài khoản "
                 "trước khi chép mã.</p>")
    if issue_show:
        heading = "Mã bí mật mới — %s" % (display_name or account_id)
        viewed_line = (
            "<p>Mã mới cấp lúc %s — chép mã rồi dán vào file cấu hình "
            "của đúng tài khoản.</p>"
            % esc(format_day_time_saigon(viewed_at)))
        rotate_link = ""
    else:
        heading = "Mã bí mật — %s" % (display_name or account_id)
        viewed_line = (
            "<p>Đã xem mã lúc %s — mã này chỉ hiện trong trang này; "
            "đóng trang hoặc tải lại thì không xem lại được nữa, phải "
            "xoay mã mới.</p>"
            % esc(format_day_time_saigon(viewed_at)))
        rotate_link = (
            '<a class="btn danger" href="/center/accounts/%s/'
            'rotate-secret">Xoay mã mới ngay</a> '
            % esc(account_id))
    show_again_btn = (
        '<button type="button" class="btn" '
        'id="view-secret-show-again" hidden>Hiện lại mã</button> '
        if show_again else "")
    intro = intro_html or ""
    body = (
        "<h1>%s</h1>%s"
        "<p>Mã sẽ tự ẩn sau "
        '<span id="view-secret-countdown">30</span> giây.</p>'
        '<div id="view-secret-values">'
        '<div class="msg"><div class="meta">Mã lấy tin (fetch):</div>'
        '<code class="view-secret-value" id="view-secret-fetch">%s</code> '
        '<button type="button" class="btn view-secret-copy" '
        'data-target="view-secret-fetch">Sao chép</button></div>'
        '<div class="msg"><div class="meta">Mã đẩy tin (push):</div>'
        '<code class="view-secret-value" id="view-secret-push">%s</code> '
        '<button type="button" class="btn view-secret-copy" '
        'data-target="view-secret-push">Sao chép</button></div>'
        "</div>"
        "<p>Xóa bộ nhớ tạm sau khi dán — mã đã sao chép vẫn nằm trong "
        "bộ nhớ tạm sau khi trang tự ẩn; xoay mã là biện pháp duy "
        "nhất.</p>"
        "<p>Chỉ dán mã vào file cấu hình của đúng tài khoản. Dán nhầm "
        "thì xoay mã mới ngay.</p>"
        "<p>Mã này chỉ hiện trong trang này — bấm “Hiện lại mã” để "
        "xem lại trong lúc trang còn mở, mỗi lần hiện lại tự ẩn sau "
        "30 giây; đóng trang hoặc tải lại thì không xem lại được "
        "nữa, phải xoay mã mới.</p>"
        "%s%s"
        '<div class="actions">'
        '<button type="button" class="btn" id="view-secret-hide-now">'
        "Đã chép xong, ẩn ngay</button> "
        "%s%s"
        '<a class="btn" href="/?tab=center">Về tab Trung tâm</a>'
        "</div>"
    ) % (esc(heading), intro, esc(fetch_secret),
         esc(push_secret), viewed_line, extra, show_again_btn,
         rotate_link)
    return layout(heading, body)


def center_fragment_html(tasks, accounts, health, filters,
                         account_names, now, orphan_task_ids=None,
                         partial_suspended=None, chat_unread_total=0,
                         chat_has_mention=False):
    """Fragment tab Trung tâm (trong polling 5 giây): tổng quan ->
    danh sách việc (+ lọc) -> danh sách tài khoản -> dòng dẫn "Mở
    khung chat →". Khung chat + form tạo việc KHÔNG ở đây: khung chat
    đã rời sang tab Chat riêng (v0.6.0, T3i — ĐỔI Q-T3-05), form tạo
    việc render tĩnh ngoài fragment.
    ``orphan_task_ids``: tập task_id việc mồ côi (Q-AM-06) để gắn nhắc
    "Cần chuyển tay" lên dòng việc. ``partial_suspended``: dict
    {task_id: số người làm việc đã ngưng} để gắn nhắc "N người đã
    ngưng — xem chi tiết"."""
    orphan_ids = orphan_task_ids or set()
    partial = partial_suspended or {}
    rows = "".join(
        center_task_row_html(
            task, account_names, now,
            orphan=task.get("task_id") in orphan_ids,
            suspended_count=partial.get(task.get("task_id") or "", 0))
        for task in tasks)
    if not rows:
        rows = ('<div class="notice">Chưa có việc nào khớp bộ lọc.</div>')
    account_rows = "".join(
        _center_account_row_html(entry) for entry in accounts)
    if not account_rows:
        account_rows = ('<div class="notice">Chưa có tài khoản nào.</div>')
    # Nut dan toi trang Tai khoan (Q-C6): them tai khoan moi o trang
    # Tai khoan rieng — tab Trung tam chi dan duong, khong nhap form.
    account_actions = (
        '<div class="actions">'
        '<a class="btn" href="/accounts/new">Thêm tài khoản</a> '
        '<a class="btn" href="/accounts">Quản lý tài khoản</a>'
        "</div>")
    # Chú thích bắt buộc (DESIGN §9): ngày cấp suy từ mtime chỉ là ước
    # lượng — nguồn chính xác là dòng xoay mã trong audit.jsonl.
    account_note = (
        '<div class="hint">Ngày cấp mã là ước lượng theo file trên máy; '
        "khi lệch với nhật ký xoay mã trong ui-config/audit.jsonl thì tin "
        "nhật ký audit (nguồn chính xác).</div>")
    # (v0.6.0, T3i — ĐỔI Q-T3-05) Tab Trung tâm kết thúc ở dòng dẫn
    # sang tab Chat riêng, kèm huy hiệu tổng số tin chưa đọc (+ "@"
    # khi có tin tag chủ nhân chưa đọc); không còn khung chat + ô gửi
    # tin trong tab Trung tâm.
    chat_badge = ""
    if chat_unread_total:
        chat_badge = (' <span class="room-badge">%d</span>'
                      % chat_unread_total)
    if chat_has_mention:
        chat_badge += (' <span class="room-badge room-badge--mention">'
                       "@</span>")
    chat_lead = (
        '<p class="center-chat-lead"><a href="/?tab=chat">'
        "Mở khung chat →</a>%s</p>" % chat_badge)
    return (
        "%s"
        "<h2>Việc%s</h2>%s%s"
        "<h2>Tài khoản%s</h2>%s%s%s"
        "%s"
    ) % (
        center_overview_html(health),
        _node_badge_html(len(tasks)),
        center_filter_bar_html(filters, [
            ((a.get("account_id") if isinstance(a, dict) else a[0]),
             (a.get("display_name") if isinstance(a, dict) else a[1]))
            for a in accounts]),
        rows,
        _node_badge_html(len(accounts)),
        account_actions,
        account_rows,
        account_note,
        chat_lead,
    )


# Anh xem truoc inline (phuong an A, 21/11 — SPEC TT16): chi 5
# duoi anh nay duoc xem truoc, quyet bang DUOI TEN FILE (ban ghi
# tin khong co MIME). Khong tin duoi la.
IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def image_content_type(file_name):
    """Content-Type ``image/*`` cua file anh da biet, ``None`` khi
    khong phai anh. Khong phan biet chu hoa/thuong o duoi."""
    name = (file_name or "").lower()
    for ext, mime in IMAGE_CONTENT_TYPES.items():
        if name.endswith(ext):
            return mime
    return None


def is_image_file_name(file_name):
    """True khi ten file co duoi anh da biet (xem truoc duoc)."""
    return image_content_type(file_name) is not None


def _center_file_preview_html(file_meta):
    """Anh thu nho 160x120 (phuong an A) cho file anh trong bong
    bóng. Tra "" khi khong phai anh. Anh boc trong link mo anh day
    du o tab moi (``target=_blank``, ``rel=noopener``). Khung co
    dinh 160x120 + chu "Dang tai anh…" phia sau (CSS): dang tai thi
    giu dung co, khong nhay bo cuc; tai loi thi JS (APP_JS) an ca
    khung, chi con chip. Ten file escape day du (XSS)."""
    if not isinstance(file_meta, dict):
        return ""
    file_id = file_meta.get("file_id") or ""
    name = file_meta.get("file_name") or ""
    if image_content_type(name) is None:
        return ""
    url = "/center/files/%s?xem=1" % esc(file_id)
    return (
        '<a class="center-file-preview" href="%s" target="_blank" '
        'rel="noopener" aria-label="Xem ảnh %s cỡ lớn">'
        '<span class="center-file-preview-loading">Đang tải ảnh…'
        "</span>"
        '<img class="center-file-preview-img" src="%s" alt="%s" '
        'loading="lazy" decoding="async"></a>'
    ) % (url, esc(name), url, esc(name))


def center_file_chip_html(file_meta):
    """Chip file dinh kem trong bong bóng chat (v0.2.5, Q-C7): ten
    file (cat 40 ky tu) + dung luong + link "Tải về" toi
    ``/center/files/<file_id>``. Ten file escape day du (XSS).

    (Xem truoc anh phuong an A, 21/11 — SPEC TT16) File anh
    (.jpg/.jpeg/.png/.webp/.gif, quyet bang duoi ten) thi THEM anh
    thu nho 160x120 phia tren chip (bam mo anh day du o tab moi);
    file khong phai anh giu nguyen chi chip nhu cu."""
    if not isinstance(file_meta, dict):
        return ""
    file_id = file_meta.get("file_id") or ""
    name = file_meta.get("file_name") or ""
    if len(name) > 40:
        name = name[:37] + "…"
    chip = (
        '<div class="center-file-chip">Tệp đính kèm: '
        '<a href="/center/files/%s">%s</a> · %s</div>'
    ) % (esc(file_id), esc(name),
         esc(human_file_size(file_meta.get("file_size"))))
    return _center_file_preview_html(file_meta) + chip


_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([a-z0-9][a-z0-9-]*)")


def _highlight_mentions(body_html, mentions):
    """Bao "@<account_id>" trong ``mentions`` bang span .msg-mention.

    ``body_html`` da escape (ky tu @ va account_id khong doi qua
    escape nen khop duoc); chi highlight id co trong ``mentions``
    (da kiem tra ton tai luc ghi) — "@..." khac giu nguyen chu
    thuong (Q-V030-1). (v0.3.0, T3d — SPEC TT18, DESIGN §8.3)."""
    wanted = set(mentions or [])
    if not wanted:
        return body_html

    def repl(match):
        name = match.group(1)
        if name in wanted:
            return '<span class="msg-mention">@%s</span>' % esc(name)
        return match.group(0)

    return _MENTION_RE.sub(repl, body_html)


def center_message_bubble_html(message, account_names, task_titles=None,
                               account_colors=None):
    """1 bong bóng chat (v0.2.5, Q-C3/Q-C5) — renderer CHUNG cho
    khung chat tab Trung tam va trang chi tiet viec.

    Chu nhan (from_account == "owner"): phai, nen tim nhat
    ``.msg--owner``. Tai khoan: trai, nen xam ``.msg--account``.
    Kem ten nguoi gui + gio + chip loai + (khi co ``task_titles``)
    dong ten viec + chip file. Tin chi co file thi khong co khoi
    body. Khong hien dong ten viec o trang chi tiet viec
    (``task_titles=None``).

    (v0.3.0, T3d) Tin co ``mentions`` thi highlight ten duoc tag
    (``.msg-mention``); tin tag chu nhan thi bong bóng co vien trai
    tim 4px (``.msg--mention-owner``).

    (v0.6.0, T3l — doc 10 §3.8 §6a, Q-V060-34) Bong bóng mang chấm
    màu của tài khoản gửi (``account_colors``: {account_id: hex};
    chủ nhân xám đậm #57534e; không rõ người gửi -> #475569) +
    thuộc tính ``data-from-account`` để ô lọc theo người gửi ẩn/hiện
    bằng JS. Chấm typing xanh lá của T3i KHÔNG đổi (nằm ở danh sách
    phòng, không phải bong bóng)."""
    kind = message.get("kind") or ""
    sender = message.get("from_account") or ""
    when = parse_iso_to_epoch(message.get("created_at"))
    when_text = format_day_time(when) if when is not None else "—"
    chip = ("green" if kind == "bao-cao" else
            "blue" if kind == "hoi" else "gray")
    side = "msg--owner" if sender == "owner" else "msg--account"
    task_line = ""
    lookup_key = message.get("task_id") or message.get("msg_id")
    if task_titles is not None and lookup_key:
        title = task_titles.get(lookup_key)
        if title:
            if len(title) > 40:
                title = title[:37] + "…"
            task_line = ('<div class="msg-task">Việc: %s</div>'
                         % esc(title))
        elif message.get("task_id"):
            # Viec da archive: hien task_id nhu cu (NS-08).
            task_id = message["task_id"]
            task_line = ('<div class="msg-task">Việc: '
                         '<a href="/center/tasks/%s">%s</a></div>'
                         % (esc(task_id), esc(task_id)))
    body = message.get("body") or ""
    body_html = ""
    if body:
        body_html = ('<div class="msg-body">%s</div>'
                     % _highlight_mentions(esc(body),
                                           message.get("mentions")))
    file_html = center_file_chip_html(message.get("file"))
    # (v0.3.0, T3d) Tin tag chu nhan: vien trai tim 4px.
    mention_owner = ""
    if "owner" in (message.get("mentions") or []):
        mention_owner = " msg--mention-owner"
    # (v0.6.0, T3i) data-seq de JS neo polling delta (after_seq) +
    # tai tin cu (before_seq) + gioi han ve ~300 bong bong.
    # (v0.6.0, T3l) data-from-account de o loc theo nguoi gui
    # an/hien bang JS; cham mau theo chat_color cua tai khoan gui.
    seq_attr = ""
    seq = message.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        seq_attr = ' data-seq="%d"' % seq
    colors = account_colors or {}
    color = colors.get(sender) or (
        "#57534e" if sender == "owner" else "#475569")
    color_dot = ('<span class="msg-color-dot" data-color="%s" '
                 'title="%s"></span>'
                 % (esc(color),
                    esc(account_names.get(sender, sender))))
    return (
        '<div class="msg %s%s"%s data-from-account="%s">'
        '<div class="msg-head">%s<span class="chip %s">%s</span> '
        "<strong>%s</strong>"
        ' <span class="meta">%s</span></div>'
        "%s%s%s"
        "</div>"
    ) % (side, mention_owner, seq_attr, esc(sender), color_dot, chip,
         esc(CENTER_MESSAGE_KIND_LABELS.get(kind, kind)),
         esc(account_names.get(sender, sender)),
         esc(when_text), task_line, body_html, file_html)


def center_chat_html(messages, account_names, task_titles=None,
                     account_colors=None, show_empty_notice=True):
    """Danh sach bong bóng chat (khong tieu de, khong link xem them)
    — dung trong fragment chat va lan render tinh ban dau.
    (SUA B1) ``show_empty_notice=False``: phong rong thi tra ve
    chuoi rong, khong render o "Chưa có tin nhắn nào." — dung cho
    fragment delta (after_seq/before_seq): o trong chi hien o lan
    render tinh ban dau, polling khong duoc them o moi chu ky."""
    rows = "".join(
        center_message_bubble_html(message, account_names, task_titles,
                                   account_colors)
        for message in messages)
    if not rows and show_empty_notice:
        rows = '<div class="notice">Chưa có tin nhắn nào.</div>'
    return rows


def center_room_list_html(rooms, current_room, unread_counts,
                          csrf_token, mention_rooms=None,
                          typing_rooms=None):
    """Danh sach phong o dau muc khung chat (v0.3.0, T3c — P1/P7/P8):
    khung chung (mac dinh) + 1 phong rieng/tai khoan da tham gia;
    phong luu tru gan nhan "lưu trữ". Moi hang phong la 1 nut submit
    trong form POST chuyen phong (kem CSRF + truong an ``from_room``
    + truong an ``draft`` — APP_JS chep noi dung textarea hien tai
    vao truong an truoc khi gui, CHANG-5; KHONG link GET thuan).
    Huy hieu so tin chua doc cua chu nhan tren moi hang (chi chu
    nhan thay; phong dang mo nen tim nhat).

    (v0.3.0, T3d — P7) ``mention_rooms``: tap phong co tin tag chu
    nhan chua doc — hien huy hieu "@" rieng ben canh huy hieu so
    (chi chu nhan thay; tai khoan khong co huy hieu).

    (v0.6.0, T3i) ``typing_rooms``: tap phong dang co worker tra
    loi — hien cham xanh tren ten phong. Huy hieu so/@ mang
    ``data-room-badge``/``data-room-mention`` de JS cap nhat theo
    nhịp polling 5 giay (khong doi tai lai trang)."""
    rows = []
    for entry in rooms:
        room = entry["room"]
        label = entry["display_name"]
        if entry.get("account_id") is None:
            label = "Khung chung"
        # (L5, 21/11 — quyet chu nhan) Danh sach phong khong con
        # tien to "Riêng:" — ten tai khoan dung mot minh da du
        # phan biet; tien to chi con o header phong dang mo.
        badge = ""
        count = unread_counts.get(room, 0)
        if count:
            badge = (' <span class="room-badge" data-room-badge="%s">'
                     "%d</span>" % (esc(room), count))
        else:
            badge = (' <span class="room-badge" data-room-badge="%s" '
                     'hidden></span>' % esc(room))
        if mention_rooms and room in mention_rooms:
            badge += (' <span class="room-badge room-badge--mention" '
                      'data-room-mention="%s">@</span>' % esc(room))
        else:
            badge += (' <span class="room-badge room-badge--mention" '
                      'data-room-mention="%s" hidden>@</span>'
                      % esc(room))
        typing_dot = ""
        if typing_rooms and room in typing_rooms:
            typing_dot = (' <span class="room-typing-dot" '
                          'data-room-typing="%s" title="Đang trả lời…">'
                          "</span>" % esc(room))
        archived = ""
        if entry.get("archived"):
            archived = ' <span class="meta">lưu trữ</span>'
        active = " room-row--active" if room == current_room else ""
        rows.append(
            '<form class="room-form" method="post" action="/center/room">'
            '<input type="hidden" name="csrf_token" value="%s">'
            '<input type="hidden" name="room" value="%s">'
            '<input type="hidden" name="from_room" value="%s">'
            '<input type="hidden" name="draft" value="">'
            '<input type="hidden" name="draft_copied" value="">'
            '<button type="submit" class="room-row%s">%s%s%s%s</button>'
            "</form>"
            % (esc(csrf_token), esc(room), esc(current_room), active,
               esc(label), typing_dot, badge, archived))
    return ('<div class="center-room-list">%s</div>' % "".join(rows))


def center_shared_links_html(links, state, csrf_token, room,
                             notice=None, error=None, url_value="",
                             title_value=""):
    """Muc "Link chung" trong cot trai tab Chat (21/11 — D-LC-01/
    D-LC-12/13/14/18/20, THIET-KE-LINK-CHUNG-21-11.md v0.2.1).

    ``links``: list link (moi nhat len dau); ``state``:
    "unset"|"open"|"closed" (trang thai thu gon nho phia may chu,
    D-LC-20). Khi "closed": may chu KHONG render danh sach/form —
    chi tieu de + so link + nut "Mở". Khi "unset": render du
    (CSS an danh sach + form tren mobile <= 720px). ``notice``:
    thong bao 1 lan sau xoa (flash session); ``error`` + ``url_value``
    /``title_value``: loi form them + du lieu da nhap de giu lai
    (co che (a), D-LC-06).

    (DIEN GIAI — ghi BUILD-NOTES; SUA THEO AUDIT A-NS-01, 21/11)
    Nut toggle khi "unset": may chu khong biet co man hinh nguoi
    xem, nen nut hien "Mở" + aria-expanded="false" theo anh mau
    mobile-dong (truong hop nut nay quan trong nhat la mobile, noi
    muc dang dong; tren desktop muc dang mo san nen bam chi ghi
    lai state=open, khong doi hien thi). Rieng aria-expanded thi
    APP_JS sua lai thanh "true" khi desktop (>= 721px) + unset
    (xem khoi matchMedia trong ui_static.py), cho dung trang thai
    hien thi that voi trinh doc man hinh."""
    from pc.ui.ui_store import (
        SHARED_LINK_KIND_LABELS,
        detect_shared_link_kind,
        shared_link_domain,
    )
    if state not in ("open", "closed"):
        state = "unset"
    items = list(links or [])
    if state == "open":
        toggle_state, toggle_label, expanded = "closed", "Đóng", "true"
    elif state == "closed":
        toggle_state, toggle_label, expanded = "open", "Mở", "false"
    else:
        toggle_state, toggle_label, expanded = "open", "Mở", "false"
    next_url = "/?tab=chat&room=%s#link-chung" % quote(room, safe="")
    head = (
        '<div class="shared-links-head">'
        '<h3 id="link-chung">Link chung</h3>'
        '<span class="shared-links-count">%d link</span>'
        '<form class="shared-links-toggle-form" method="post" '
        'action="/api/ui-prefs">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<input type="hidden" name="action" '
        'value="set-shared-links-state">'
        '<input type="hidden" name="state" value="%s">'
        '<input type="hidden" name="next" value="%s">'
        '<button type="submit" class="shared-links-toggle" '
        'aria-expanded="%s">%s</button>'
        "</form></div>"
        % (len(items), esc(csrf_token), toggle_state, esc(next_url),
           expanded, toggle_label))
    notice_html = ""
    if notice:
        notice_html = '<p class="notice">%s</p>' % esc(notice)
    if state == "closed":
        return ('<div class="shared-links" data-state="closed">%s%s'
                "</div>" % (head, notice_html))
    rows = []
    for item in items:
        url = item["url"]
        title = item["title"]
        kind = detect_shared_link_kind(url)
        kind_label = SHARED_LINK_KIND_LABELS.get(kind, "Link")
        rows.append(
            '<li class="shared-link-row">'
            '<span class="shared-link-kind">%s</span>'
            '<span class="shared-link-main">'
            '<a href="%s" target="_blank" rel="noopener">%s</a>'
            '<span class="shared-link-domain">%s</span>'
            "</span>"
            '<form class="shared-link-delete" method="post" '
            'action="/api/shared-links/delete">'
            '<input type="hidden" name="csrf_token" value="%s">'
            '<input type="hidden" name="link_id" value="%s">'
            '<input type="hidden" name="room" value="%s">'
            '<button type="submit" class="shared-link-delete-btn" '
            'aria-label="Xóa link %s">×</button>'
            "</form></li>"
            % (esc(kind_label), esc(url), esc(title),
               esc(shared_link_domain(url)), esc(csrf_token),
               esc(item["id"]), esc(room), esc(title)))
    error_html = ""
    url_attrs = ""
    if error:
        error_html = ('<p class="field-error" id="shared-link-error">'
                      "%s</p>" % esc(error))
        url_attrs = (' aria-invalid="true" '
                     'aria-describedby="shared-link-error"')
    add_form = (
        '<form class="shared-links-add" method="post" '
        'action="/api/shared-links/add">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<input type="hidden" name="room" value="%s">'
        '<label for="shared-link-url">Link</label>'
        '<input id="shared-link-url" name="url" value="%s"%s '
        'placeholder="https://docs.google.com/document/d/…/edit">'
        "%s"
        '<label for="shared-link-title">Tiêu đề (không bắt buộc)'
        "</label>"
        '<input id="shared-link-title" name="title" value="%s" '
        'placeholder="vd: Kế hoạch xuất clinker Q4">'
        '<div class="actions"><button type="submit">Thêm</button></div>'
        "</form>"
        % (esc(csrf_token), esc(room), esc(url_value), url_attrs,
           error_html, esc(title_value)))
    return ('<div class="shared-links" data-state="%s">%s%s'
            '<ul class="shared-links-list">%s</ul>%s</div>'
            % (state, head, notice_html, "".join(rows), add_form))


def center_chat_fragment_html(messages, account_names, task_titles,
                              chat_limit, has_more, room="chung",
                              oldest_seq=None, newest_seq=None,
                              account_colors=None,
                              show_empty_notice=True):
    """Fragment chat rieng (v0.2.5, Q-C1/Q-C2 — viet lai v0.6.0, T3i):
    chi danh sach bong bóng + 1 the meta an (``data-chat-meta`` mang
    ``data-has-more``/``data-oldest-seq``/``data-newest-seq``) de JS
    neo polling delta (``after_seq`` — chi lay tin moi hon) + tai tin
    cu (``before_seq`` — 50 tin/lan). O nhap KHONG o day — nam ngoai
    fragment de khong mat ban nhap khi polling (thay T3-CH-06).
    KHONG con nut/link "Xem thêm" (cuon len toi dau hop thi tu tai
    them). ``has_more`` = False nghia la da toi tin cu nhat con luu
    (JS hien dong "Đã tới tin cũ nhất còn lưu…").
    (SUA B1) Fragment delta (``show_empty_notice=False``) voi 0 tin
    moi chi tra the meta, KHONG tra o trong — polling khong duoc
    them 1 o trong moi 5 giay vao hop chat."""
    rows = center_chat_html(messages, account_names, task_titles,
                            account_colors,
                            show_empty_notice=show_empty_notice)
    meta = (
        '<div data-chat-meta data-has-more="%s" data-oldest-seq="%s" '
        'data-newest-seq="%s" hidden></div>'
        % ("true" if has_more else "false",
           "" if oldest_seq is None else oldest_seq,
           "" if newest_seq is None else newest_seq))
    return rows + meta


def center_chat_tab_html(rooms, current_room, room_label, messages,
                         account_names, task_titles, chat_limit,
                         has_more, oldest_seq, newest_seq, unread_counts,
                         mention_rooms, typing_rooms, typing_names,
                         csrf_token, draft, archived, client_key,
                         storage_warn="", recipients=None,
                         filter_senders=None, account_colors=None,
                         stopped_rounds=None, shared_links=None,
                         shared_links_state="unset",
                         shared_link_notice=None, shared_link_error=None,
                         shared_link_url="", shared_link_title=""):
    """Tab Chat rieng, mo hinh A (v0.6.0, T3i — DOI Q-T3-05): cot
    trai danh sach phong (300px) + cot phai khung chat; khoang cach
    2 cot 16px; dien thoai <= 720px xep doc (CSS). Hop chat tab Chat
    desktop cao calc(100vh - 266px), toi thieu 400px (B2, 21/11;
    dien thoai 40vh/320px — SUA L1 21/11, da duoc chu nhan chot);
    quy tac goc 65vh chi con cho hop TINH trang chi tiet viec
    (ngoai .center-chat-section), 1 co, khong nut phong to/thu nho.
    Vien "N tin moi" + nut "Ve cuoi" nam tren hop
    chat (JS dieu khien); o nhap + form tao viec KHONG o day (form
    tao viec o tab Trung tam). Phong luu tru: chi xem, khong o
    nhap. ``typing_names``: ten tai khoan dang tra loi trong phong
    dang mo (de hien dong "đang trả lời…" duoi ten phong).

    (v0.6.0, T3l — doc 10 §3.8 §6b/§4b) ``filter_senders``: list
    (account_id, tên) cho ô "Lọc theo người gửi" trên đầu khung chat
    (Tất cả / từng tài khoản / chủ nhân) — chỉ ẩn/hiện tin đang hiển
    thị bằng JS, KHÔNG tải lại, KHÔNG lọc theo phòng.
    ``stopped_rounds``: vòng thảo luận đã dừng của phòng đang mở —
    hiện 1 dòng "vòng đã dừng" trên đầu khung chat."""
    room_list = center_room_list_html(
        rooms, current_room, unread_counts, csrf_token,
        mention_rooms=mention_rooms, typing_rooms=typing_rooms)
    chat_rows = center_chat_html(messages, account_names, task_titles,
                                account_colors)
    filter_html = ""
    if filter_senders is not None:
        filter_options = ['<option value="">Tất cả</option>']
        for account_id, name in filter_senders:
            filter_options.append('<option value="%s">%s</option>'
                                  % (esc(account_id), esc(name)))
        filter_html = (
            '<div class="center-sender-filter">'
            '<label for="center-sender-filter">Lọc theo người gửi'
            "</label>"
            '<select id="center-sender-filter" name="sender_filter">'
            "%s</select></div>" % "".join(filter_options))
    stopped_html = ""
    if stopped_rounds:
        for item in stopped_rounds:
            names = ", ".join(
                account_names.get(account_id, account_id)
                for account_id in item.get("participants") or [])
            stopped_html += (
                '<div class="center-round-stopped">Vòng thảo luận đã '
                "dừng (đủ 5 lượt) — %s. Chỉ chủ nhân mở vòng mới bằng "
                "tin mới.</div>" % esc(names))
    oldest_line = ""
    if not has_more:
        oldest_line = ('<div class="center-oldest-line">Đã tới tin cũ '
                       "nhất còn lưu…</div>")
    typing_line = ""
    if typing_names:
        typing_line = ('<div class="center-typing-line" '
                       'id="center-typing-line">%s</div>'
                       % esc(", ".join(typing_names) + " đang trả lời…"))
    else:
        typing_line = ('<div class="center-typing-line" '
                       'id="center-typing-line"></div>')
    form = center_chat_form_html(
        csrf_token, room=current_room, draft=draft, archived=archived,
        client_key=client_key, recipients=recipients)
    shared_links_html = center_shared_links_html(
        shared_links or [], shared_links_state, csrf_token,
        current_room, notice=shared_link_notice,
        error=shared_link_error, url_value=shared_link_url,
        title_value=shared_link_title)
    return (
        '<section class="center-chat-section">'
        '<h2 id="center-chat">Chat</h2>'
        "%s"
        '<div class="center-chat-layout">'
        '<div class="center-chat-rooms">%s%s</div>'
        '<div class="center-chat-main">'
        '<div class="center-chat-room-head"><strong>%s</strong>%s</div>'
        "%s"
        '<div class="center-chat-box-wrap">'
        '<div class="center-chat-box" id="center-chat-box" '
        'data-center-chat-box data-room="%s" '
        'data-oldest-seq="%s" data-newest-seq="%s" '
        'data-has-more="%s" data-archived="%s">'
        "%s%s"
        "</div>"
        # (SUA B3/B4 + L2, 21/11 — THAY anchor sticky cu) Vien
        # "N tin moi" + nut "Ve cuoi" la con TRUC TIEP cua wrap
        # (position: relative, khong cuon) — neo absolute theo
        # wrap nen luon nam trong khung hien thi cua hop. Anchor
        # sticky cu (con cuoi cua hop cuon) DA TROI that (video
        # B3 + do L2) nen bo han; JS chen bong bóng bang
        # appendChild (khong can chen truoc wrapper nua).
        '<button type="button" class="center-new-pill" '
        'id="center-new-pill" hidden></button>'
        '<button type="button" class="center-scroll-bottom" '
        'id="center-scroll-bottom" title="Về cuối" '
        'aria-label="Về cuối">↓</button>'
        "</div>"
        "%s"
        "</div>"
        "</div>"
        "</section>"
    ) % (storage_warn, room_list, shared_links_html, esc(room_label),
         typing_line,
         filter_html + stopped_html,
         esc(current_room),
         "" if oldest_seq is None else oldest_seq,
         "" if newest_seq is None else newest_seq,
         "true" if has_more else "false",
         "1" if archived else "0",
         oldest_line, chat_rows, form)


def center_chat_form_html(csrf_token, room="chung", draft="",
                          archived=False, client_key="", recipients=None):
    """Ô gửi tin hoi của chủ nhân (ngoại lệ F18): nhãn "Gửi", đỏ rượu,
    kèm CSRF. Form multipart (kèm file dinh kem) — KHONG co route gửi
    giao-viec/bao-cao tay. Textarea KHONG required: chi co file cung
    gui duoc. 1 dong canh bao secret (quyet sau phan bien #5).

    (v0.3.0, T3c) Form mang truong an ``room`` (phong dang mo);
    textarea nap san ban nhap cua phong (kho nhap phia may chu,
    P9). Phong luu tru: KHONG render o nhap — chi dong nhac xem lai.
    Duoi o "Đính kèm file" co dong ghi chu mat file khi doi phong
    (NS-09). (v0.4.0, T3e) Form mang truong an ``client_key`` (sinh
    1 lan luc render) de dedupe gui lai form sau loi (§3.6).
    (v0.6.0, T3i — D8, da thay bang schema v4 o T3l) O chon "Gửi cho:":
    "Gửi cho tất cả tài khoản" (to_account=None) hoac NHIEU tai khoan
    cung luc (to_account la DANH SACH, toi da 5 nguoi, ke ca khi chi
    chon 1 nguoi). Gioi han D8-v3 (chi Tat ca hoac dung 1 tai khoan,
    chuoi v3) da bi T3l thay the.
    (v0.6.0, T3l — D8 + schema v4, C-18) O chon "Gửi cho:" nay cho
    chon NHIEU tai khoan cung luc: ``<select multiple>`` — giu phim
    Ctrl (Windows/Linux) hoac Cmd (Mac) roi bam them tung tai khoan
    de chon them; khong chon gi (hoac chi chon "Gửi cho tất cả tài
    khoản") = gui cho tat ca. May chu doc moi gia tri da chon, truyen
    ``to_account`` thanh DANH SACH (toi da 5 nguoi); "owner"/khong
    ton tai -> 400."""
    if archived:
        return (
            '<div class="notice">Phòng lưu trữ — chỉ xem lại tin cũ, '
            "không gửi thêm được.</div>")
    # (L5, 21/11 — quyet chu nhan) Nhan mac dinh cua o chon la
    # "Gửi cho tất cả tài khoản" (khong con "Tất cả" dung mot minh
    # de tranh lap nhan voi tab "Tất cả").
    options = ['<option value="">Gửi cho tất cả tài khoản</option>']
    for account_id, name in (recipients or []):
        options.append('<option value="%s">%s</option>'
                       % (esc(account_id), esc(name)))
    return (
        "<h2>Hỏi qua trung tâm</h2>"
        '<form class="center-chat-form" method="post" '
        'action="/center/messages" enctype="multipart/form-data">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<input type="hidden" name="room" value="%s">'
        '<input type="hidden" name="client_key" value="%s">'
        '<label for="center-hoi-body">Tin nhắn</label>'
        '<textarea id="center-hoi-body" name="body" '
        'placeholder="Hỏi tài khoản đang làm việc...">%s</textarea>'
        '<label for="center-hoi-to">Gửi cho:</label>'
        '<select id="center-hoi-to" name="to_account" multiple '
        'size="5">%s</select>'
        '<div class="hint">Chọn nhiều tài khoản: giữ Ctrl (Windows) '
        "hoặc Cmd (Mac) rồi bấm thêm từng tài khoản. Không chọn gì = "
        "gửi cho tất cả.</div>"
        '<div class="chat-form-row">'
        '<label class="file-label" for="center-hoi-file">'
        "Đính kèm file</label>"
        '<input type="file" id="center-hoi-file" name="file">'
        '<button class="primary-action" type="submit">Gửi</button>'
        "</div>"
        '<div class="hint hint--mobile-hide">Chỉ gửi tin hỏi (hoi). '
        "Không gửi giao việc hay báo cáo thay tài khoản (F18).</div>"
        '<div class="hint">Đừng tải lên file chứa mật khẩu/mã bí mật. '
        "Tối đa 50 MB/file.</div>"
        '<div class="hint hint--mobile-hide">Đổi phòng sẽ mất file đã '
        'chọn, chỉ giữ lại chữ đã gõ.</div>'
        "</form>"
    ) % (esc(csrf_token), esc(room), esc(client_key), esc(draft),
         "".join(options))


def center_create_form_html(assignable, csrf_token, error=None, values=None):
    """Form tạo việc (TT1): đủ trường bắt buộc, due_at bắt buộc."""
    values = values or {}
    err = ""
    if error:
        err = '<div class="field-error">%s</div>' % esc(error)
    if not assignable:
        return (
            "<h2>Tạo việc mới</h2>%s"
            '<div class="notice">Chưa có tài khoản tham gia trung tâm '
            "và có đủ secret — thêm tài khoản rồi thử lại.</div>"
        ) % err
    kind_options = ""
    for value, label in CENTER_TASK_KINDS:
        selected = " selected" if values.get("kind") == value else ""
        kind_options += '<option value="%s"%s>%s</option>' % (
            esc(value), selected, esc(label))
    selected_assignees = values.get("assigned_to") or []
    if isinstance(selected_assignees, str):
        selected_assignees = [selected_assignees]
    account_options = ""
    for account_id, name in assignable:
        selected = " selected" if account_id in selected_assignees \
            else ""
        account_options += '<option value="%s"%s>%s</option>' % (
            esc(account_id), selected, esc(name))
    return (
        "<h2>Tạo việc mới</h2>%s"
        '<form method="post" action="/center/tasks">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<label for="center-title">Tiêu đề (tối đa 200 ký tự)</label>'
        '<input type="text" id="center-title" name="title" required '
        'maxlength="200" value="%s">'
        '<label for="center-body">Nội dung</label>'
        '<textarea id="center-body" name="body" required>%s</textarea>'
        '<label for="center-kind">Loại việc</label>'
        '<select id="center-kind" name="kind">%s</select>'
        '<label for="center-assigned">Giao cho (chọn 1–5 tài khoản, '
        "giữ Ctrl/Cmd để chọn thêm)</label>"
        '<select id="center-assigned" name="assigned_to" multiple '
        'size="5">%s</select>'
        '<label for="center-due">Hạn xong (bắt buộc)</label>'
        '<input type="datetime-local" id="center-due" name="due_at" '
        'required value="%s">'
        '<div class="actions"><button class="primary-action" '
        'type="submit">Tạo việc</button></div>'
        "</form>"
    ) % (err, esc(csrf_token), esc(values.get("title", "")),
         esc(values.get("body", "")), kind_options, account_options,
         esc(values.get("due_at", "")))


def center_task_detail_html(task, messages, account_names, csrf_token,
                            assignable, now, report_match=None,
                            accounts=None, account_colors=None):
    """Trang chi tiết việc: toàn văn + history + tin gắn task_id
    (chỉ lịch sử chat, không ô gửi) + nút thao tác theo trạng thái.

    (v0.4.0) Việc chung: hiện trạng thái từng người làm việc
    (``report_match``: {account_id: {"reported", "matched"}} — co
    "bao cao chua khop" theo tung nguoi, §6.4; ``accounts``: dict
    tai khoan de danh dau nguoi da ngung) + chip "x/y da bao xong"
    + chuyen tay chon nhieu + nut "Dat han" khi chua co han."""
    status = task.get("status") or ""
    assigned = task.get("assigned_to") or ""
    if isinstance(assigned, str):
        assigned = [assigned] if assigned else []
    assigned_names = ", ".join(
        account_names.get(a, a) for a in assigned)
    due = parse_iso_to_epoch(task.get("due_at"))
    due_text = format_day_time(due) if due is not None else "chưa có hạn"
    seen = parse_iso_to_epoch(task.get("seen_at"))
    seen_text = format_day_time(seen) if seen is not None \
        else "Chưa thấy nhận"
    # Trang thai tung nguoi lam viec (v0.4.0, §7.4/TT11).
    match = report_match or {}
    account_map = accounts or {}
    person_rows = ""
    reported_by = task.get("reported_by") or []
    refused_by = task.get("refused_by") or []
    for account_id in assigned:
        info = match.get(account_id) or {}
        if info.get("reported"):
            state = "Đã báo xong"
            if not info.get("matched"):
                state += " — báo cáo chưa khớp"
        else:
            state = "Chưa báo xong"
        stopped = ""
        record = account_map.get(account_id)
        if isinstance(record, dict) \
                and not record.get("center_participating"):
            stopped = " (đã ngưng tham gia)"
        person_rows += (
            '<div class="msg"><div>%s%s — %s</div></div>'
            % (esc(account_names.get(account_id, account_id)),
               esc(stopped), esc(state)))
    for account_id in refused_by:
        if account_id in assigned:
            continue
        person_rows += (
            '<div class="msg"><div>%s — Đã từ chối</div></div>'
            % esc(account_names.get(account_id, account_id)))
    if person_rows:
        person_rows = ("<h2>Người làm việc</h2>%s" % person_rows)
    reported_chip = ""
    if len(assigned) > 1:
        reported_chip = (
            ' <span class="chip gray">%d/%d đã báo xong</span>'
            % (len([a for a in reported_by if a in assigned]),
               len(assigned)))
    overdue_chip = (
        ' <span class="chip red">Quá hạn</span>'
        if _center_task_overdue(task, now) else "")
    # GC-E05: nut "Doi han" canh dong han (neo toi form doi han o
    # muc Thao tac); chi hien khi viec da co han va chua xong/huy.
    due_action = ""
    if task.get("due_at") is not None \
            and status not in CENTER_TERMINAL_STATUSES:
        due_action = ' <a href="#center-due-change">Đổi hạn</a>'
    history_rows = ""
    for entry in task.get("history") or []:
        when = parse_iso_to_epoch(entry.get("at"))
        when_text = format_day_time(when) if when is not None else "—"
        note = entry.get("note")
        note_text = (" — Ghi chú: %s" % esc(note)) if note else ""
        history_rows += (
            '<div class="msg"><div class="head">%s</div>'
            "<div>%s → %s (bởi %s)%s</div></div>"
        ) % (esc(when_text),
             esc(CENTER_TASK_STATUS_LABELS.get(
                 entry.get("from_status") or "", entry.get("from_status"))),
             esc(CENTER_TASK_STATUS_LABELS.get(
                 entry.get("to_status") or "", entry.get("to_status"))),
             esc(entry.get("by") or ""), note_text)
    if not history_rows:
        history_rows = '<div class="notice">Chưa có lịch sử đổi trạng thái.</div>'
    chat_rows = "".join(
        center_message_bubble_html(message, account_names, None,
                                   account_colors)
        for message in messages)
    if not chat_rows:
        chat_rows = '<div class="notice">Chưa có tin gắn việc này.</div>'
    actions = ""
    if status not in CENTER_TERMINAL_STATUSES:
        task_id = esc(task.get("task_id") or "")
        forms = []
        if status == "da-bao-xong":
            forms.append(
                '<form class="inline" method="post" '
                'action="/center/tasks/%s/status">'
                '<input type="hidden" name="csrf_token" value="%s">'
                '<input type="hidden" name="to_status" '
                'value="da-xac-nhan">'
                '<button class="primary-action" type="submit">'
                "Xác nhận xong</button></form>"
                % (task_id, esc(csrf_token)))
            forms.append(
                '<form method="post" action="/center/tasks/%s/status">'
                '<input type="hidden" name="csrf_token" value="%s">'
                '<input type="hidden" name="to_status" value="dang-lam">'
                '<label for="center-note-return">Lý do trả lại '
                "(bắt buộc)</label>"
                '<textarea id="center-note-return" name="note" '
                'required></textarea>'
                '<div class="actions"><button type="submit">'
                "Trả lại</button></div></form>"
                % (task_id, esc(csrf_token)))
        if status == "moi":
            forms.append(
                '<form method="post" action="/center/tasks/%s/status">'
                '<input type="hidden" name="csrf_token" value="%s">'
                '<input type="hidden" name="to_status" value="huy">'
                '<label for="center-note-reject">Lý do từ chối '
                "(bắt buộc)</label>"
                '<textarea id="center-note-reject" name="note" '
                'required></textarea>'
                # Lech T3 (d): trang tu choi ghi ro viec con lai cho
                # nguoi khac hay huy han khi la nguoi cuoi.
                "<p>Từ chối: việc còn lại cho người khác; nếu không "
                "còn ai làm thì việc bị hủy hẳn.</p>"
                '<div class="actions"><button class="danger" '
                'type="submit">Từ chối việc</button></div></form>'
                % (task_id, esc(csrf_token)))
        elif status in ("da-nhan", "dang-lam", "da-bao-xong"):
            forms.append(
                '<form class="inline" method="post" '
                'action="/center/tasks/%s/status">'
                '<input type="hidden" name="csrf_token" value="%s">'
                '<input type="hidden" name="to_status" value="huy">'
                '<button class="danger" type="submit">'
                "Hủy việc</button></form>"
                % (task_id, esc(csrf_token)))
        account_options = ""
        for account_id, name in assignable:
            selected = " selected" if account_id in assigned else ""
            account_options += '<option value="%s"%s>%s</option>' % (
                esc(account_id), selected, esc(name))
        if account_options:
            forms.append(
                '<form method="post" action="/center/tasks/%s/reassign">'
                '<input type="hidden" name="csrf_token" value="%s">'
                '<label for="center-reassign">Chuyển cho tài khoản '
                "khác (chọn 1–5, giữ Ctrl/Cmd để chọn thêm — thay toàn "
                "bộ danh sách)</label>"
                '<select id="center-reassign" name="assigned_to" '
                'multiple size="5">%s</select>'
                '<div class="actions"><button type="submit">'
                "Chuyển tay</button></div></form>"
                % (task_id, esc(csrf_token), account_options))
        if task.get("due_at") is None:
            # Nut "Dat han" (Q-V040-3): chi hien khi viec chua co han.
            forms.append(
                '<form method="post" action="/center/tasks/%s/due">'
                '<input type="hidden" name="csrf_token" value="%s">'
                '<label for="center-due-set">Đặt hạn xong</label>'
                '<input type="datetime-local" id="center-due-set" '
                'name="due_at" required>'
                '<div class="actions"><button type="submit">'
                "Đặt hạn</button></div></form>"
                % (task_id, esc(csrf_token)))
        else:
            # Nut "Doi han" (GC-E05): chi hien khi viec DA co han;
            # form doi han ngay trong trang (o datetime-local +
            # "Luu han moi" + link "Huy" quay lai, khong doi gi).
            due_input = (task.get("due_at") or "")[:16]
            forms.append(
                '<form method="post" action="/center/tasks/%s/due">'
                '<input type="hidden" name="csrf_token" value="%s">'
                '<label for="center-due-change">Đổi hạn xong</label>'
                '<input type="datetime-local" id="center-due-change" '
                'name="due_at" value="%s" required>'
                '<div class="actions"><button type="submit">'
                "Lưu hạn mới</button>"
                ' <a href="/center/tasks/%s">Hủy</a></div></form>'
                % (task_id, esc(csrf_token), esc(due_input), task_id))
        actions = "<h2>Thao tác</h2>%s" % "".join(forms)
    body = (
        '<p><a href="/?tab=center">← Tab Trung tâm</a></p>'
        "<h1>%s</h1>"
        '<div class="meta">%s%s%s · Loại: %s · Giao cho %s · Hạn %s%s · %s</div>'
        '<div class="msg"><div class="body">%s</div></div>'
        "%s"
        "<h2>Lịch sử đổi trạng thái</h2>%s"
        "<h2>Chat gắn việc này</h2>"
        # (v0.6.0, T3i — Q-V060-09) Trang chi tiet viec la trang doc
        # lai TINH: lich su chat dat trong cung hop cuon nhu khung
        # chat chinh (65vh/400px) nhung KHONG polling, KHONG vien "N
        # tin moi", KHONG nut "Ve cuoi" (hop khong co
        # data-center-chat-box nen JS bo qua).
        '<div class="center-chat-box center-chat-box--static">%s</div>'
    ) % (
        esc(task.get("title") or ""),
        esc(CENTER_TASK_STATUS_LABELS.get(status, status)),
        overdue_chip,
        reported_chip,
        esc(CENTER_TASK_KIND_LABELS.get(
            task.get("kind") or "", task.get("kind") or "")),
        esc(assigned_names),
        esc(due_text),
        due_action,
        esc(seen_text),
        esc(task.get("body") or ""),
        person_rows + actions,
        history_rows,
        chat_rows,
    )
    return layout(task.get("title") or "Việc",
                  body, csrf_token=csrf_token)


# Nhan tieng Viet cho ngay chay don (center.json: prune_day).
PRUNE_DAY_LABELS = (
    ("mon", "Thứ hai"),
    ("tue", "Thứ ba"),
    ("wed", "Thứ tư"),
    ("thu", "Thứ năm"),
    ("fri", "Thứ sáu"),
    ("sat", "Thứ bảy"),
    ("sun", "Chủ nhật"),
)

# Nhan tieng Viet cho duong kich hoat lan don cuoi (prune_state.json).
PRUNE_TRIGGER_LABELS = {
    "schedule": "Theo lịch",
    "near_full": "Tự dọn khi kho gần đầy",
    "manual": "Dọn tay",
}


def ui_settings_page_html(prefs, csrf_token, center_config=None,
                          prune_state=None, file_used_bytes=None,
                          alert_settings=None, theme="light"):
    """Trang Cài đặt chung (TT15, v0.5.0): KHÔNG phải tab — mở bằng
    link từ trang chính. 7 nhóm theo đúng thứ tự: Dọn dẹp, File, Việc
    quá hạn, Cảnh báo, Giao diện, Đồng bộ, Link chung (nhóm 7,
    21/11 — D-LC-16).

    ``center_config``: dict load_center_config (None -> mặc định);
    ``prune_state``: dict read_prune_state (None -> chưa dọn lần
    nào); ``file_used_bytes``: tổng dung lượng kho file (None -> không
    hiện số liệu kho); ``alert_settings``: float ngưỡng cảnh báo quota
    (None -> 80%). Nhóm Cảnh báo (đợt 2, T3g) + Đồng bộ (chỉ hiển
    thị) đã làm thật — không còn khung "đợt 2".
    ``theme`` (v0.6.0, T3k): "light"/"dark" — lựa chọn đang dùng,
    đánh dấu chọn sẵn trong form công tắc theme ở nhóm Giao diện."""
    from pc.center.center_store import (
        MAX_FILE_BYTES,
        MAX_FILE_STORE_BYTES,
        PRUNE_RETENTION_DAYS_VALUES,
    )
    config = dict(center_config or {})
    prune_day = config.get("prune_day", "sun")
    retention = config.get("prune_retention_days", 30)
    fraction = config.get("prune_near_full_fraction", 0.8)
    try:
        near_full_pct = int(round(float(fraction) * 100))
    except (TypeError, ValueError):
        near_full_pct = 80
    severe = config.get("overdue_severe_hours", 24)
    try:
        alert_pct = int(round(float(alert_settings) * 100))
    except (TypeError, ValueError):
        alert_pct = 80

    day_options = "".join(
        '<option value="%s"%s>%s</option>'
        % (esc(value), " selected" if value == prune_day else "",
           esc(label))
        for value, label in PRUNE_DAY_LABELS)
    retention_options = "".join(
        '<option value="%d"%s>%d ngày</option>'
        % (days, " selected" if days == retention else "", days)
        for days in PRUNE_RETENTION_DAYS_VALUES)

    # -- Nhóm 1: Dọn dẹp -------------------------------------------------
    if isinstance(prune_state, dict):
        result = prune_state.get("last_prune_result") or {}
        last_at = parse_iso_to_epoch(prune_state.get("last_prune_at"))
        last_text = (
            "%s · %s · Đã xóa %s file, giải phóng %s, cắt %s sự kiện "
            "(đã lưu trữ %s việc đã xong/hủy)"
            % (format_day_time_saigon(last_at)
               if last_at else "không rõ giờ",
               esc(PRUNE_TRIGGER_LABELS.get(
                   prune_state.get("last_prune_trigger"), "?")),
               esc(result.get("files_deleted", 0)),
               esc(human_file_size(result.get("bytes_freed", 0))),
               esc(result.get("events_pruned", 0)),
               esc(result.get("tasks_archived", 0))))
    else:
        last_text = "Chưa dọn lần nào."
    prune_group = (
        '<h2 id="nhom-don-dep">1. Dọn dẹp</h2>'
        '<form method="post" action="/api/settings/center">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<label for="prune_day">Ngày chạy dọn</label>'
        '<select id="prune_day" name="prune_day">%s</select>'
        "<p>Giờ dọn cố định 21:00 (giờ Asia/Saigon). Không có ô đặt "
        "giờ — hẹn giờ hệ thống chạy mỗi ngày 21:00, lệnh bọc chỉ dọn "
        "vào ngày đã chọn (hoặc dọn bù khi mở máy lại nếu lỡ kỳ).</p>"
        '<label for="prune_retention_days">Thời gian giữ lại</label>'
        '<select id="prune_retention_days" '
        'name="prune_retention_days">%s</select>'
        "<p>Tính từ lúc bắt đầu việc hoặc lúc gửi tin.</p>"
        '<label for="prune_near_full_pct">Tự dọn khi kho gần đầy '
        "(%%)</label>"
        '<input type="number" id="prune_near_full_pct" '
        'name="prune_near_full_pct" min="50" max="95" step="1" '
        'value="%d">'
        "<p>Nhập số nguyên 50–95, ví dụ 80.</p>"
        "<p>Kho dùng từ mức này trở lên thì lần tải file lên sau sẽ "
        "dọn trước rồi mới quyết định có nhận file không.</p>"
        '<div class="actions"><button type="submit">Lưu dọn dẹp'
        "</button></div>"
        "</form>"
        '<form method="post" action="/api/prune-now">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<div class="actions"><button type="submit">Dọn ngay</button>'
        "</div></form>"
        '<p id="ket-qua-don">Lần dọn cuối: %s</p>'
    ) % (esc(csrf_token), day_options, retention_options, near_full_pct,
         esc(csrf_token), esc(last_text))

    # -- Nhóm 2: File (chỉ hiển thị, đợt 1 — Q4) ------------------------
    if isinstance(file_used_bytes, int) and file_used_bytes >= 0:
        used_text = human_file_size(file_used_bytes)
        free_text = human_file_size(
            max(0, MAX_FILE_STORE_BYTES - file_used_bytes))
        warn = ""
        if file_used_bytes >= fraction * MAX_FILE_STORE_BYTES:
            warn = (
                '<p class="error">Kho file đã dùng từ %d%% trở lên — '
                "lần tải file lên sau sẽ tự dọn trước. Nếu kho đầy "
                "toàn file đang dùng thì giảm thời gian giữ lại rồi "
                "bấm “Dọn ngay”.</p>" % near_full_pct)
        usage = ("<p>Đã dùng %s · Còn trống %s</p>%s"
                 % (esc(used_text), esc(free_text), warn))
    else:
        usage = ""
    file_group = (
        '<h2 id="nhom-file">2. File</h2>'
        "<p>Tối đa %s/file · Kho tổng %s.</p>%s"
        "<p>File nhận qua Telegram tối đa 20 MB do Telegram đặt. "
        "Bytes file không vào sao lưu hằng ngày — file mất thì gửi "
        "lại.</p>"
        "<p>Giới hạn file đợt này chỉ hiển thị, chưa cho sửa trên "
        "giao diện.</p>"
    ) % (esc(human_file_size(MAX_FILE_BYTES)),
         esc(human_file_size(MAX_FILE_STORE_BYTES)), usage)

    # -- Nhóm 3: Việc quá hạn -------------------------------------------
    overdue_group = (
        '<h2 id="nhom-qua-han">3. Việc quá hạn</h2>'
        '<form method="post" action="/api/settings/center">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<label for="overdue_severe_hours">Quá hạn bao lâu thì tính '
        "là nặng (giờ)</label>"
        '<input type="number" id="overdue_severe_hours" '
        'name="overdue_severe_hours" min="1" max="168" value="%s">'
        '<div class="actions"><button type="submit">Lưu</button></div>'
        "</form>"
    ) % (esc(csrf_token), esc(severe))

    # -- Nhóm 4: Cảnh báo (đợt 2 — Q9, SPEC TT20) ----------------------
    alerts_group = (
        '<h2 id="nhom-canh-bao">4. Cảnh báo</h2>'
        '<form method="post" action="/api/settings/alerts">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<label for="quota_warn_pct">Ngưỡng cảnh báo quota (%%)'
        "</label>"
        '<input type="number" id="quota_warn_pct" '
        'name="quota_warn_pct" min="50" max="95" step="1" value="%d">'
        "<p>Nhập số nguyên 50–95, ví dụ 80. Cảnh báo khi quota Zalo "
        "dùng hết bao nhiêu phần trăm.</p>"
        "<p>Áp cho ước lượng quota Zalo trên thẻ bot (nhãn “Sắp hết”). "
        "Con số là ước tính cục bộ, chưa kiểm chứng với Zalo.</p>"
        '<div class="actions"><button type="submit">Lưu cảnh báo'
        "</button></div>"
        "</form>"
    ) % (esc(csrf_token), alert_pct)

    # -- Nhóm 6: Đồng bộ (chỉ hiển thị — SPEC TT20) ---------------------
    sync_group = (
        '<h2 id="nhom-dong-bo">6. Đồng bộ</h2>'
        "<p>Lấy tin mới mỗi 10 giây · Quét việc quá hạn mỗi 60 giây.</p>"
        "<p>2 con số này nằm phía Muse, muốn đổi phải sửa phía Muse "
        "(không cho sửa trên giao diện).</p>"
    )

    # -- Nhóm 5: Giao diện (giữ nguyên, §7.7) ---------------------------
    # Công tắc theme (v0.6.0, T3k — phong cách C, frag-04 §3.3): 2 lựa
    # chọn radio + nút Lưu, đặt ngay đầu nhóm 5, trước bảng 13 node.
    # Form POST thường + redirect về /settings (không JS mới).
    theme_checked = {"light": "", "dark": ""}
    if theme == "dark":
        theme_checked["dark"] = " checked"
    else:
        theme_checked["light"] = " checked"
    theme_form = (
        '<form method="post" action="/api/ui-prefs">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<input type="hidden" name="action" value="set-theme">'
        '<input type="hidden" name="next" value="/settings">'
        '<fieldset><legend>Giao diện</legend>'
        '<label><input type="radio" name="theme" value="light"%s> '
        "Sáng Superhuman</label>"
        '<label><input type="radio" name="theme" value="dark"%s> '
        "Tối Hades</label>"
        "</fieldset>"
        '<div class="actions"><button class="primary-action" '
        'type="submit">Lưu</button></div>'
        "</form>"
    ) % (esc(csrf_token), theme_checked["light"], theme_checked["dark"])
    rows = ""
    for key, name in UI_NODE_NAMES:
        default_open = "Đóng" if key in UI_PREF_DEFAULT_COLLAPSED else "Mở"
        current = "Đóng" if prefs.get(key) else "Mở"
        rows += ("<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                 % (esc(name), esc(default_open), esc(current)))
    interface_group = (
        '<h2 id="nhom-giao-dien">5. Giao diện</h2>'
        "%s"
        "<p>Bảng trạng thái đóng/mở mặc định của 13 node. Đổi từng node "
        "bằng nút Mở/Đóng ở tiêu đề node trên trang chính và trang chi "
        "tiết bot.</p>"
        "<table><thead><tr><th>Node</th><th>Mặc định</th>"
        "<th>Hiện tại</th></tr></thead><tbody>%s</tbody></table>"
        "<h2>Về mặc định</h2>"
        '<form method="post" action="/api/ui-prefs">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<input type="hidden" name="action" value="reset-defaults">'
        '<div class="actions"><button type="submit">'
        "Về mặc định</button></div>"
        "</form>"
    ) % (theme_form, rows, esc(csrf_token))

    # -- Nhóm 7: Link chung (21/11 — D-LC-16) ------------------------
    # 2 radio "1"/"0" theo tien le discussion (ui_app.py:2699-2703):
    # KHONG dung checkbox tran gui "on" (se bi 400 "giá trị không
    # hợp lệ" o nhanh bool). Dong giai thich viet dung su that
    # §5.1 thiet ke.
    open_flag = bool(config.get("shared_links_open_to_all", False))
    link_chung_group = (
        '<h2 id="nhom-link-chung">7. Link chung</h2>'
        '<form method="post" action="/api/settings/center">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<fieldset><legend>Mở Link chung cho mọi tài khoản</legend>'
        '<label><input type="radio" name="shared_links_open_to_all" '
        'value="1"%s> Mở cho mọi tài khoản</label>'
        '<label><input type="radio" name="shared_links_open_to_all" '
        'value="0"%s> Chỉ chủ nhân</label>'
        "</fieldset>"
        "<p>Mọi người vào được giao diện này đều thấy nút thêm/xóa. "
        "Đây chỉ là công tắc hiển thị, không phải cổng bảo mật — "
        "giao diện chỉ có 1 mật khẩu chung.</p>"
        '<div class="actions"><button type="submit">Lưu</button></div>'
        "</form>"
    ) % (esc(csrf_token),
         " checked" if open_flag else "",
         "" if open_flag else " checked")

    # ĐX-05 (T3m): muc luc neo nhanh dau trang — 7 link thuong,
    # dung thu tu nhom; 360px cho xuong dong (flex-wrap cua CSS
    # chung), khong tran ngang.
    toc = (
        '<p class="toc">'
        '<a href="#nhom-don-dep">1. Dọn dẹp</a> · '
        '<a href="#nhom-file">2. File</a> · '
        '<a href="#nhom-qua-han">3. Việc quá hạn</a> · '
        '<a href="#nhom-canh-bao">4. Cảnh báo</a> · '
        '<a href="#nhom-giao-dien">5. Giao diện</a> · '
        '<a href="#nhom-dong-bo">6. Đồng bộ</a> · '
        '<a href="#nhom-link-chung">7. Link chung</a></p>'
    )
    body = (
        "<h1>Cài đặt chung</h1>"
        '<p><a href="/">← Về màn hình chính</a></p>'
        "%s%s%s%s%s%s%s%s"
    ) % (toc, prune_group, file_group, overdue_group, alerts_group,
         interface_group, sync_group, link_chung_group)
    return layout("Cài đặt chung", body, csrf_token=csrf_token,
                  theme=theme)


def history_item_html(item, sync_mode, allowlist_chats, csrf_token, bot_id):
    """1 mục lịch sử chi tiết (UI4: toàn văn)."""
    when = format_day_time(item["ts"]) if item["ts"] else "--/-- --:--"
    chat = item.get("chat_title") or short_id(item["chat_id"])
    if item["kind"] == "in":
        head = "%s · %s · %s" % (
            when, chat, item.get("sender_name") or short_id(item["sender_id"]))
        status_parts = [esc(item["fetch_label"])]
        if item.get("age_label"):
            status_parts.append(esc(item["age_label"]))
        if sync_mode == "g3b" and not item["paired"]:
            status_parts.append(esc(CHIP_NO_REPLY_SEEN))
        status = " · ".join(status_parts)
        add_chat = ""
        if item["chat_id"] and item["chat_id"] not in allowlist_chats:
            add_chat = (
                '<form class="inline" method="post" '
                'action="/bots/%s/add-chat">'
                '<input type="hidden" name="csrf_token" value="%s">'
                '<input type="hidden" name="seq" value="%d">'
                '<button class="small" type="submit">Thêm chat này vào '
                "danh sách cho phép</button></form>"
            ) % (esc(bot_id), esc(csrf_token), item["seq"] or 0)
        caption = ""
        if item.get("caption") and item["caption"] != item["text"]:
            caption = '<div class="body">Chú thích: %s</div>' % esc(
                item["caption"])
        return (
            '<div class="msg"><div class="head">%s</div>'
            '<div class="body">%s</div>%s'
            '<div class="status">%s</div>%s</div>'
        ) % (esc(head), esc(item["text"]), caption, status, add_chat)
    status = esc(item["status"])
    if item.get("error_class"):
        status += " (%s)" % esc(item["error_class"])
    caption = ""
    if item.get("caption") and item["caption"] != item["text"]:
        caption = '<div class="body">Chú thích: %s</div>' % esc(item["caption"])
    return (
        '<div class="msg out"><div class="head">%s · %s · Bot trả lời</div>'
        '<div class="body">%s</div>%s'
        '<div class="status">%s</div></div>'
    ) % (esc(when), esc(chat), esc(item["text"]), caption, status)


def history_fragment_html(items, sync, allowlist_chats, csrf_token, bot_id):
    """Fragment lịch sử 1 bot (phân trang ở trang đầy đủ)."""
    parts = []
    if sync["mode"] == "g3a":
        parts.append('<div class="notice">%s</div>' % esc(G3A_NOTICE))
    if sync.get("fallback"):
        parts.append(
            '<div class="banner">%s</div>' % esc(sync["line"]))
    for item in items:
        parts.append(history_item_html(
            item, sync["mode"], allowlist_chats, csrf_token, bot_id))
    if not items:
        parts.append('<div class="notice">Chưa có tin nhắn nào.</div>')
    return "".join(parts)


def dead_letters_section_html(bot_id, tips, total, page, pages,
                              csrf_token):
    """Mục "Thư chết" trên trang chi tiết bot (NS-02, T3m).

    Moi dong: gio gui (Asia/Saigon), ten chat, noi dung cat 200 ky
    tu + link "xem du" (mo lich su loc thu chet, noi hien toan van),
    ly do loi bang cau tieng Viet than thien, so lan da thu lai,
    nut "Gui lai". Dau muc: nut "Gui lai tat ca" (chi khi >=2 thu
    chet). Khong co thu chet thi hien "Không có thư chết."
    """
    if not total:
        inner = "<p>Không có thư chết.</p>"
    else:
        rows = ""
        for item in tips:
            when = format_day_time(item["ts"]) if item["ts"] \
                else "--/-- --:--"
            chat = item.get("chat_title") or short_id(item["chat_id"])
            text = item.get("text") or ""
            if len(text) > 200:
                body = '%s … <a href="/bots/%s?status=dead">xem đủ</a>' % (
                    esc(text[:200]), esc(bot_id))
            else:
                body = esc(text)
            reason = dead_error_text(item.get("error_class"))
            rows += (
                '<li><div class="meta">%s · %s · %s</div>'
                '<div class="body">%s</div>'
                '<div class="meta">Đã thử lại %d lần</div>'
                '<form class="inline" method="post" '
                'action="/api/bots/%s/retry-dead">'
                '<input type="hidden" name="csrf_token" value="%s">'
                '<input type="hidden" name="reply_key" value="%s">'
                '<button class="small" type="submit">Gửi lại</button>'
                "</form></li>"
            ) % (esc(when), esc(chat), esc(reason), body,
                 item.get("attempts", 0), esc(bot_id), esc(csrf_token),
                 esc(item.get("root_key") or ""))
        retry_all = ""
        if total >= 2:
            retry_all = (
                '<form class="inline" method="post" '
                'action="/api/bots/%s/retry-dead">'
                '<input type="hidden" name="csrf_token" value="%s">'
                '<input type="hidden" name="retry_all" value="1">'
                '<button type="submit">Gửi lại tất cả</button></form>'
            ) % (esc(bot_id), esc(csrf_token))
        pager = ""
        if pages > 1:
            prev_link = ""
            next_link = ""
            base = "/bots/%s?dead_page=" % esc(bot_id)
            if page > 1:
                prev_link = '<a class="btn" href="%s%d">← Trước</a>' % (
                    base, page - 1)
            if page < pages:
                next_link = '<a class="btn" href="%s%d">Sau →</a>' % (
                    base, page + 1)
            pager = (
                '<div class="pager">%s<span>Trang %d/%d · %d thư</span>%s'
                "</div>"
                % (prev_link, page, pages, total, next_link)
            )
        inner = "%s<ul class=\"history\">%s</ul>%s" % (
            retry_all, rows, pager)
    return "<section><h2>Thư chết</h2>%s</section>" % inner


def bot_detail_page_html(summary, history_fragment, total, page, pages,
                         sync, config, allowlist, log_fragment, csrf_token,
                         chat_filter, status_filter, daily=None,
                         alert_events=None, poll_samples=None, chats=None,
                         accounts=None, reminder=None, dual_hidden=False,
                         prefs=None, next_url=None, node_summaries=None,
                         dead_letters=None, retry_notice=None):
    """Trang chi tiết bot: điều khiển, cấu hình, thống kê ngày, lịch
    sử cảnh báo, lịch sử có lọc theo chat/trạng thái, log.

    ``daily``: thống kê 7 ngày; ``alert_events``: sự kiện cảnh báo
    30 ngày của bot; ``poll_samples``: mẫu sức khỏe; ``chats``: danh
    sách chat để lọc. ``chat_filter`` là khóa lọc mờ (không phải
    chat_id thô — xem ``chat_filter_key``). Tham số lọc được giữ
    nguyên qua phân trang và polling fragment. ``accounts``: dict
    {"accounts": {...}} để tra tên gọi; ``reminder``: dòng nhắc bền
    sau hủy/hết hạn (R5); ``dual_hidden``: cảnh báo trùng lấy đang
    tạm ẩn theo luồng chuyển.

    ``retry_notice`` (v0.6.0, NS-R1 — D17): số thư đã nhận yêu cầu
    gửi lại ở cú bấm vừa rồi (flash 1 lần từ session; None = không
    có cú bấm vừa rồi nên không hiện gì) — hiện câu xác nhận ngay
    trên mục Thư chết, kèm đúng số N.
    """
    bot_id = summary["bot_id"]
    controls = _detail_controls_html(summary, csrf_token)
    config_rows = ""
    for key in ("platform", "display_name", "bot_kind", "group_privacy",
                "parse_mode"):
        value = config.get(key)
        if value is not None:
            config_rows += "<dt>%s</dt><dd>%s</dd>" % (esc(key), esc(value))
    allowlist_text = "Chưa có"
    if isinstance(allowlist, dict):
        chats_allowed = allowlist.get("allowed_chats") or []
        owners = allowlist.get("owner_sender_ids") or []
        allowlist_text = "Chat: %d · Chủ: %d" % (len(chats_allowed),
                                                 len(owners))
    extra_params = {}
    if chat_filter:
        extra_params["chat"] = chat_filter
    if status_filter:
        extra_params["status"] = status_filter
    extra_qs = ""
    if extra_params:
        extra_qs = "&" + urlencode(extra_params)
    pager = ""
    if pages > 1:
        prev_link = ""
        next_link = ""
        base = "/bots/%s?page=" % esc(bot_id)
        if page > 1:
            prev_link = '<a class="btn" href="%s%d%s">← Trước</a>' % (
                base, page - 1, esc(extra_qs))
        if page < pages:
            next_link = '<a class="btn" href="%s%d%s">Sau →</a>' % (
                base, page + 1, esc(extra_qs))
        pager = (
            '<div class="pager">%s<span>Trang %d/%d · %d mục</span>%s</div>'
            % (prev_link, page, pages, total, next_link)
        )
    sync_line = (
        '<div class="notice">Đồng bộ trả lời: %s</div>' % esc(sync["line"])
    )
    health_section = _health_strip_html(poll_samples)
    daily_section = _daily_stats_html(daily, sync.get("mode"))
    alert_section = _bot_alert_history_html(
        summary, alert_events or [])
    account_section = _account_line_html(summary, accounts)
    transfer_section = _transfer_section_html(summary, csrf_token)
    reminder_section = ""
    if reminder:
        reminder_section = (
            '<div class="banner">%s</div>'
            '<form class="inline" method="post" action="/bots/%s/transfer/'
            'dismiss-reminder">'
            '<input type="hidden" name="csrf_token" value="%s">'
            '<button type="submit">Tôi đã dọn xong</button></form>'
        ) % (esc(reminder.get("text") or ""), esc(bot_id), esc(csrf_token))
    fetch_section = _fetch_labels_section_html(
        summary, accounts, dual_hidden)
    history_filter = _history_filter_html(bot_id, chats or [], chat_filter,
                                          status_filter, sync.get("mode"))
    summaries_map = node_summaries or {}
    detail_url = next_url or ("/bots/%s" % bot_id)
    all_actions = ""
    if prefs is not None:
        all_actions = open_close_all_html(
            "bot-detail", csrf_token, detail_url)

    def _node(key, title, inner):
        return node_section_html(
            key, title, inner, prefs, csrf_token, detail_url,
            summaries_map.get(key, ""))

    health_node = _node("health", "Sức khỏe", health_section)
    controls_node = _node("controls", "Điều khiển", controls)
    config_inner = (
        '<dl class="config">%s<dt>Danh sách cho phép</dt><dd>%s</dd></dl>'
        '<div class="actions"><a class="btn" href="/bots/%s/edit">Sửa bot</a>'
        '<a class="btn" href="/bots/%s/replace-token">Thay token đã lưu</a>'
        "</div>"
    ) % (config_rows, esc(allowlist_text), esc(bot_id), esc(bot_id))
    config_node = _node("config", "Cấu hình", config_inner)
    stats_node = _node("stats-7d", None, daily_section)
    alerts_node = _node("alerts-history-detail", None, alert_section)
    fetches_node = _node("fetches", None, fetch_section)
    history_inner = (
        "%s"
        '<div data-fragment-url="/fragments/history?bot=%s&amp;page=%d%s" '
        'data-interval="5000">%s</div>'
        "%s"
    ) % (history_filter, esc(bot_id), page, esc(extra_qs),
         history_fragment, pager)
    history_node = _node("message-history", "Lịch sử", history_inner)
    log_inner = (
        '<div data-fragment-url="/fragments/log?bot=%s" '
        'data-interval="15000">%s</div>'
    ) % (esc(bot_id), log_fragment)
    log_node = _node("ear-log", "Log tai nghe (đã lược)", log_inner)
    dead_section = ""
    if dead_letters is not None:
        tips, dead_total, dead_page, dead_pages = dead_letters
        dead_section = dead_letters_section_html(
            bot_id, tips, dead_total, dead_page, dead_pages, csrf_token)
    # (v0.6.0, NS-R1 — D17) Câu xác nhận sau cú bấm "Gửi lại" /
    # "Gửi lại tất cả" — dùng đúng class .notice sẵn có của UI.
    retry_notice_html = ""
    if retry_notice is not None:
        if retry_notice > 0:
            retry_text = (
                "Đã nhận yêu cầu gửi lại %d thư — sẽ gửi khi phía "
                "Muse kéo yêu cầu (chu kỳ sau)." % retry_notice)
        else:
            retry_text = "Không có thư chết nào cần gửi lại."
        retry_notice_html = '<div class="notice">%s</div>' % esc(retry_text)
    body = (
        '<p><a href="/">← Tất cả bot</a></p>'
        "<h1>%s %s</h1>"
        '<div class="meta">%s · %s</div>'
        "%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s"
    ) % (
        esc(summary["display_name"]),
        _status_chip(summary),
        _platform_label(summary["platform"]),
        esc(bot_id),
        all_actions,
        health_node,
        account_section,
        reminder_section,
        transfer_section,
        controls_node,
        config_node,
        stats_node,
        alerts_node,
        sync_line,
        fetches_node,
        history_node,
        retry_notice_html,
        dead_section,
        log_node,
    )
    return layout(summary["display_name"], body, csrf_token=csrf_token)


def _account_line_html(summary, accounts):
    """Dòng tài khoản quản lý trên trang chi tiết (MA2/doc 08 §4.2):
    tên tài khoản + trạng thái gắn + link tới thẻ tài khoản."""
    bot_id = summary["bot_id"]
    owner_id = summary.get("owner_account")
    if owner_id:
        name = summary.get("owner_display_name") or owner_id
        return (
            '<div class="kv">Tài khoản quản lý: %s (đã gắn) · '
            '<a href="/accounts#%s">Xem thẻ tài khoản</a></div>'
        ) % (esc(name), esc(owner_id))
    return (
        '<div class="kv">Tài khoản quản lý: Chưa gắn tài khoản · '
        '<a href="/accounts">Quản lý tài khoản</a></div>'
    )


def _transfer_section_html(summary, csrf_token):
    """Mục chuyển bot trên trang chi tiết (doc 09 §4.1/§4.5): 1 điểm
    vào duy nhất. Đang chuyển dở: thanh tiến độ 10 mốc + nút tiếp tục;
    chưa có luồng: nút mở wizard."""
    bot_id = summary["bot_id"]
    transfer = summary.get("transfer")
    if not transfer:
        # G1 (D-G1-2): bot chua gan thi thay link "Chuyen bot" bang nut
        # "Gan bot vao tai khoan" dan toi form Sua bot — CHI 1 vi tri
        # nay (khong dat them nut canh nut "Sua bot" o muc Cau hinh).
        # summary["owner_account"] la owner_account_of(entry).
        if not summary.get("owner_account"):
            return (
                '<div class="actions"><a class="btn" href="/bots/%s/edit">'
                "Gắn bot vào tài khoản</a></div>"
            ) % esc(bot_id)
        return (
            '<div class="actions"><a class="btn" href="/bots/%s/transfer">'
            "Chuyển bot sang tài khoản khác</a></div>"
        ) % esc(bot_id)
    try:
        current_step = int(transfer.get("current_step"))
    except (TypeError, ValueError):
        current_step = 0
    steps = transfer.get("steps") if isinstance(
        transfer.get("steps"), dict) else {}
    dots = []
    for step_no in range(10):
        entry = steps.get(str(step_no))
        status = entry.get("status") if isinstance(entry, dict) else "locked"
        if status == "done":
            mark = "✓"
        elif step_no == current_step:
            mark = "●"
        else:
            mark = "○"
        dots.append('<span title="%s">%d%s</span>'
                    % (esc(TRANSFER_STEP_TITLES[step_no]), step_no, mark))
    to_name = summary.get("transfer_to_display_name") or transfer.get(
        "to_account_id") or ""
    return (
        '<div class="notice">Đang chuyển sang tài khoản %s — mốc %d '
        "trong 0–9: %s</div>"
        '<div class="actions"><a class="btn" href="/bots/%s/'
        'transfer">Tiếp tục chuyển</a></div>'
    ) % (esc(to_name), current_step, " ".join(dots), esc(bot_id))


def _fetch_labels_section_html(summary, accounts, dual_hidden):
    """Mục "Lượt lấy gần đây" (doc 09 §5.5), đặt cạnh trạng thái đồng
    bộ: từng nhãn + tên gọi + lần thấy cuối + tổng lượt + dòng lượt
    không nhãn. 3 trạng thái hiển thị riêng (S11); dòng 1 nhãn lệch
    chủ quản (S6) là thông tin, không phải cảnh báo đỏ; dòng "đang
    tạm ẩn" khi cảnh báo bị ẩn theo luồng chuyển (R4)."""
    account_map = (accounts or {}).get("accounts") or {}
    state = label_display_state(summary.get("fetch_entry"))
    parts = ["<h2>Lượt lấy gần đây</h2>"]
    if state["state"] == "no_data":
        parts.append(
            '<div class="notice">chưa có dữ liệu nhãn (gateway chưa '
            "nâng cấp)</div>"
        )
    elif state["state"] == "unlabelled":
        parts.append('<div class="notice">lấy không kèm nhãn</div>')
    else:
        label_texts = []
        for label in state["labels"]:
            name = None
            record = account_map.get(label["id"])
            if isinstance(record, dict):
                name = record.get("display_name")
            when = format_day_time(label["last_seen_at"]) \
                if label["last_seen_at"] else "không rõ"
            if name:
                label_texts.append("%s (%s) — lần thấy cuối %s · %d lượt"
                                   % (label["id"], name, when,
                                      label["fetch_count"]))
            else:
                label_texts.append(
                    "%s (chưa có trong danh sách tài khoản) — lần thấy "
                    "cuối %s · %d lượt"
                    % (label["id"], when, label["fetch_count"]))
        parts.append('<div class="notice">nhãn: %s</div>'
                     % esc("; ".join(label_texts)))
    if state["has_unlabelled_field"] and state["unlabelled"] is not None:
        unlab = state["unlabelled"]
        when = format_day_time(unlab["last_seen_at"]) \
            if unlab["last_seen_at"] else "không rõ"
        parts.append(
            '<div class="hint">Lượt không nhãn: %d lượt · lần thấy cuối '
            "%s</div>" % (unlab["fetch_count"], esc(when))
        )
    # Dòng thông tin 1 nhãn lệch chủ quản (S6).
    active = summary.get("dual_fetch_labels") or []
    owner_id = summary.get("owner_account")
    if len(active) == 1 and owner_id and active[0] != owner_id:
        owner_name = summary.get("owner_display_name") or owner_id
        parts.append(
            '<div class="hint">Nhãn lấy tin gần nhất: %s — khác tài '
            "khoản quản lý ghi trên nhãn (%s). Nhãn tự khai, chỉ để "
            "đối chiếu.</div>"
            % (esc(active[0]), esc(owner_name))
        )
    if dual_hidden:
        parts.append(
            '<div class="hint">Cảnh báo trùng lấy đang tạm ẩn trong lúc '
            "chuyển bot. Bằng chứng nhãn vẫn được ghi lại.</div>"
        )
    has_dual_alert = any(
        alert.get("key") == "dual-fetch"
        for alert in summary.get("alerts") or [])
    if has_dual_alert or dual_hidden:
        parts.append(
            '<div class="hint">Cơ chế này chỉ phát hiện giao nhầm vô '
            "tình. Nếu 2 tài khoản khai cùng 1 nhãn, hoặc 1 bên không "
            "gửi nhãn, hệ thống không phát hiện được. Nhãn là lời tự "
            "khai, không xác định được ai thật sự đang lấy — đối chiếu "
            "bằng fetcher-config trên từng tài khoản, không kết luận "
            "chỉ từ nhãn. Đang chuyển bot thì cảnh báo tạm ẩn tới khi "
            "luồng chuyển kết thúc (bước 8 xong hoặc luồng kết thúc, "
            "cái nào trước) — xem nhật ký chuyển để đối chiếu.</div>"
        )
    return "".join(parts)


def _health_strip_html(poll_samples):
    """Dải chấm sức khỏe: xanh = lấy tin tốt, vàng = chậm, đỏ = chết,
    xám = chưa rõ. Ghi rõ mẫu chỉ ghi khi giao diện đang mở."""
    if not poll_samples:
        return (
            '<div class="hint">Chưa có mẫu sức khỏe nào — mở giao diện '
            "một lúc để ghi lại lần lấy tin.</div>"
        )
    dots = []
    for sample in poll_samples:
        dots.append(
            '<span class="dot %s" title="%s"></span>'
            % (esc(sample["status"]), esc(format_day_time(sample["ts"])))
        )
    return (
        '<div class="health" aria-label="Lịch sử lần lấy tin">%s</div>'
        '<div class="hint">Mỗi chấm là một lần ghi khi giao diện đang mở '
        "(tối đa 5 phút/chấm). Khoảng trống khi không mở giao diện là "
        "bình thường, không có nghĩa là bot chết.</div>"
    ) % "".join(dots)


def _daily_stats_html(daily, sync_mode):
    """Bảng thống kê theo ngày (7 ngày, mới nhất trước).

    GĐ3a: chỉ cột "Tin đến" — số trả lời/thư chết chưa biết (file
    trả lời chưa đồng bộ về), hiện 0 sẽ nói sai thành "chưa trả
    lời tin nào".
    """
    if not daily:
        return ""
    show_replies = sync_mode == "g3b"
    rows = []
    for day in daily:
        if show_replies:
            rows.append(
                "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (esc(day["label"]), _fmt_num(day["incoming"]),
                   _fmt_num(day["sent"]), _fmt_num(day["dead"])))
        else:
            rows.append(
                "<tr><td>%s</td><td>%s</td></tr>"
                % (esc(day["label"]), _fmt_num(day["incoming"])))
    if show_replies:
        head = ("<tr><th>Ngày</th><th>Tin đến</th>"
                "<th>Trả lời đã gửi</th><th>Thư chết</th></tr>")
    else:
        head = "<tr><th>Ngày</th><th>Tin đến</th></tr>"
    return (
        "<h2>Thống kê theo ngày</h2>"
        '<table class="daily"><thead>%s</thead>'
        "<tbody>%s</tbody></table>"
    ) % (head, "".join(rows))


def _bot_alert_history_html(summary, alert_events):
    """Lịch sử cảnh báo của riêng bot này (30 ngày, mới nhất trước)."""
    names = {summary["bot_id"]: summary.get("display_name") or summary["bot_id"]}
    platforms = {summary["bot_id"]: summary.get("platform") or "?"}
    rows = "".join(_alert_event_row_html(event, names, platforms)
                   for event in alert_events)
    if not rows:
        rows = ('<div class="notice">Không có sự kiện cảnh báo nào '
                "trong 30 ngày.</div>")
    return "<h2>Lịch sử cảnh báo</h2>%s" % rows


def _history_filter_html(bot_id, chats, chat_filter, status_filter,
                         sync_mode):
    """Form lọc lịch sử theo chat + trạng thái (GET, về trang 1).

    GĐ3a: chỉ 2 trạng thái tin đến (Mới nhận / Muse đã lấy) — không
    có lựa chọn trạng thái trả lời vì dữ liệu trả lời chưa về.
    """
    chat_options = ['<option value="">Tất cả chat</option>']
    for chat in chats:
        selected = " selected" if chat["key"] == chat_filter else ""
        chat_options.append('<option value="%s"%s>%s</option>' % (
            esc(chat["key"]), selected, esc(chat["title"])))
    status_choices = [("", "Tất cả trạng thái"),
                      ("new", "Mới nhận — chưa lấy"),
                      ("fetched", "Muse đã lấy")]
    if sync_mode == "g3b":
        status_choices += [("sent", "Đã gửi"),
                           ("failed", "Gửi lỗi"),
                           ("dead", "Thư chết")]
    status_options = ""
    for value, label in status_choices:
        selected = " selected" if value == status_filter else ""
        status_options += '<option value="%s"%s>%s</option>' % (
            value, selected, esc(label))
    return (
        '<form class="filters" method="get" action="/bots/%s">'
        '<label class="sr-only" for="chat-filter">Chat</label>'
        '<select id="chat-filter" name="chat">%s</select>'
        '<label class="sr-only" for="status-filter">Trạng thái</label>'
        '<select id="status-filter" name="status">%s</select>'
        '<button type="submit">Lọc</button>'
        "</form>"
    ) % (esc(bot_id), "".join(chat_options), status_options)


def _detail_controls_html(summary, csrf_token):
    """Nút điều khiển trên trang chi tiết (cùng luật khóa như thẻ)."""
    bot_id = summary["bot_id"]
    enable_disabled = ""
    note = ""
    if not summary.get("has_allowlist"):
        enable_disabled = " disabled"
        note = ('<div class="hint">Chưa bật được: bot chưa có danh sách '
                "chat được phép (D5).</div>")
    elif summary.get("awaiting_muse_sync"):
        enable_disabled = " disabled"
        note = ('<div class="hint">Chờ đồng bộ về Muse: nút Bật mở khi '
                "phía Muse lấy tin lần đầu thành công.</div>")
    if summary["paused"]:
        pause_button = (
            '<button type="submit" name="action" value="resume">Bật lại</button>'
        )
    else:
        pause_button = (
            '<button type="submit" name="action" value="pause">Tạm dừng</button>'
        )
    if summary["enabled"]:
        power_button = (
            '<button type="submit" name="action" value="disable">Tắt</button>'
        )
    else:
        power_button = (
            '<button type="submit" name="action" value="enable"%s>Bật</button>'
            % enable_disabled
        )
    return (
        '<form class="inline" method="post" action="/bots/%s/control">'
        '<input type="hidden" name="csrf_token" value="%s">'
        "%s%s"
        '<button type="submit" name="action" value="restart">Khởi động lại</button>'
        "</form>%s"
    ) % (esc(bot_id), esc(csrf_token), power_button, pause_button, note)


def log_fragment_html(lines):
    """Fragment log tai nghe đã lược (mỗi dòng đã redact ở tầng app)."""
    if not lines:
        return '<div class="notice">Không đọc được log.</div>'
    return "<pre class=\"log\">%s</pre>" % esc("\n".join(lines))


def confirm_page_html(title, intro_html, form_action, hidden_fields,
                      csrf_token, confirm_label, danger=False, back_url="/"):
    """Trang xác nhận bước 2: ghi rõ hậu quả, nút xác nhận duy nhất."""
    hidden = "".join(
        '<input type="hidden" name="%s" value="%s">'
        % (esc(key), esc(value)) for key, value in hidden_fields.items()
    )
    button_class = "danger" if danger else "primary-action"
    body = (
        "<h1>%s</h1>%s"
        '<form method="post" action="%s">'
        '<input type="hidden" name="csrf_token" value="%s">'
        "%s"
        '<div class="actions"><button class="%s" type="submit">%s</button>'
        '<a class="btn" href="%s">Quay lại</a></div>'
        "</form>"
    ) % (
        esc(title),
        intro_html,
        esc(form_action),
        esc(csrf_token),
        hidden,
        button_class,
        esc(confirm_label),
        esc(back_url),
    )
    return layout(title, body, csrf_token=csrf_token)


def bot_form_html(mode, values, errors, csrf_token, bot_id=None,
                  accounts=None, transfer_locked=False):
    """Biểu mẫu thêm/sửa bot (UI3). ``mode``: "new" hoặc "edit".

    Token KHÔNG bao giờ render lại: ô token luôn rỗng, chỉ có ở form
    thêm mới; form sửa không có ô token (thay token qua luồng riêng).
    ``accounts``: list (account_id, display_name) cho trường "Tài
    khoản quản lý"; ``transfer_locked``: khóa trường đó trong lúc bot
    đang chuyển (doc 09 §4.5).
    """
    title = "Thêm bot mới" if mode == "new" else "Sửa bot"
    action = "/bots/new" if mode == "new" else "/bots/%s/edit" % esc(bot_id)
    err_html = ""
    if errors:
        err_html = '<div class="field-error">%s</div>' % "<br>".join(
            esc(e) for e in errors)
    platform = values.get("platform", "zalo")
    zalo_sel = " selected" if platform == "zalo" else ""
    tele_sel = " selected" if platform == "telegram" else ""
    bot_kind = values.get("bot_kind", "one_to_one")
    kind_dm = " selected" if bot_kind == "one_to_one" else ""
    kind_group = " selected" if bot_kind == "group" else ""
    policy = values.get("default_sender_policy", "deny")
    policy_deny = " selected" if policy == "deny" else ""
    policy_allow = " selected" if policy == "allow" else ""
    privacy = values.get("group_privacy", "on")
    privacy_on = " selected" if privacy == "on" else ""
    privacy_off = " selected" if privacy == "off" else ""
    token_field = ""
    if mode == "new":
        token_field = (
            '<label for="token">Token của bot (dán 1 lần, không hiện '
            "lại sau khi lưu)</label>"
            '<input type="password" id="token" name="token" '
            'autocomplete="off" required>'
            '<div class="hint">Lấy token từ trang quản lý của nền tảng '
            "(Zalo: trang quản lý bot; Telegram: BotFather). Hệ thống "
            "kiểm tra token bằng getMe trước khi lưu; token sai thì "
            "không lưu gì.</div>"
        )
    # Trường "Tài khoản quản lý" (doc 08 §4.2 / 05 §3): chỉ đổi nhãn.
    selected_owner = values.get("owner_account") or ""
    owner_options = ['<option value=""%s>Chưa gắn</option>'
                     % (" selected" if not selected_owner else "")]
    for account_id, name in (accounts or []):
        selected = " selected" if account_id == selected_owner else ""
        owner_options.append('<option value="%s"%s>%s</option>'
                             % (esc(account_id), selected, esc(name)))
    owner_disabled = " disabled" if transfer_locked else ""
    owner_locked_note = ""
    if transfer_locked:
        owner_locked_note = (
            '<div class="hint">đang chuyển — đổi nhãn ở bước 6 của '
            "luồng chuyển.</div>"
        )
    owner_field = (
        '<label for="owner_account">Tài khoản quản lý</label>'
        '<select id="owner_account" name="owner_account"%s>%s</select>'
        '<div class="hint">Đổi nhãn không tự chuyển bot. Chuyển bot '
        "thật: dùng nút Chuyển bot trên trang chi tiết (quy trình §6).</div>"
        "%s"
    ) % (owner_disabled, "".join(owner_options), owner_locked_note)
    body = (
        "<h1>%s</h1>%s"
        '<form method="post" action="%s">'
        '<input type="hidden" name="csrf_token" value="%s">'
    ) % (esc(title), err_html, action, esc(csrf_token))
    if mode == "new":
        body += (
            '<label for="bot_id">ID bot (chữ thường, số, gạch ngang, '
            "tối đa 32 ký tự)</label>"
            '<input type="text" id="bot_id" name="bot_id" '
            'value="%s" required>'
        ) % esc(values.get("bot_id", ""))
    body += (
        '<label for="display_name">Tên hiển thị</label>'
        '<input type="text" id="display_name" name="display_name" '
        'value="%s" required>'
        '<label for="platform">Nền tảng</label>'
        '<select id="platform" name="platform">'
        '<option value="zalo"%s>Zalo</option>'
        '<option value="telegram"%s>Telegram</option>'
        "</select>"
        "%s"
        "%s"
        '<label for="bot_kind">Vai trò</label>'
        '<select id="bot_kind" name="bot_kind">'
        '<option value="one_to_one"%s>Chat riêng (1-1)</option>'
        '<option value="group"%s>Nhóm</option>'
        "</select>"
        '<label for="allowed_chats">Danh sách chat được phép '
        "(mỗi dòng 1 chat_id) — có thể để trống, nhưng bot chưa bật "
        "được cho tới khi có ít nhất 1 chat hoặc 1 chủ</label>"
        '<textarea id="allowed_chats" name="allowed_chats">%s</textarea>'
        '<div class="hint">Chưa biết chat_id? Lưu bot trước, gửi 1 tin '
        "cho bot, rồi bấm \"Thêm chat này vào danh sách cho phép\" ở "
        "trang chi tiết của bot.</div>"
        '<label for="owner_sender_ids">ID người chủ (mỗi dòng 1 id)'
        "</label>"
        '<textarea id="owner_sender_ids" name="owner_sender_ids">%s</textarea>'
        '<label for="default_sender_policy">Người lạ chưa có trong '
        "danh sách</label>"
        '<select id="default_sender_policy" name="default_sender_policy">'
        '<option value="deny"%s>Không trả lời (chặn)</option>'
        '<option value="allow"%s>Trả lời</option>'
        "</select>"
        '<label for="group_privacy">Privacy nhóm Telegram (chỉ áp dụng '
        "cho bot Telegram)</label>"
        '<select id="group_privacy" name="group_privacy">'
        '<option value="on"%s>BẬT — chỉ nghe lệnh/reply (mặc định)</option>'
        '<option value="off"%s>TẮT — nghe mọi tin trong nhóm</option>'
        "</select>"
        '<div class="hint">Tắt privacy: phải xóa bot khỏi nhóm và thêm '
        "lại mới có hiệu lực; chỉ tắt khi có lý do rõ và nhóm biết (D4).</div>"
        '<div class="mt-16">'
        '<button class="primary-action" type="submit">%s</button>'
        ' <a class="btn" href="/">Hủy</a></div>'
        "</form>"
    ) % (
        esc(values.get("display_name", "")),
        zalo_sel, tele_sel,
        token_field,
        owner_field,
        kind_dm, kind_group,
        esc(values.get("allowed_chats", "")),
        esc(values.get("owner_sender_ids", "")),
        policy_deny, policy_allow,
        privacy_on, privacy_off,
        "Lưu bot" if mode == "new" else "Lưu thay đổi",
    )
    return layout(title, body, csrf_token=csrf_token)


def handoff_page_html(bot_id, gateway_endpoint, receiver_endpoint,
                      queue_secret, sync_secret, csrf_token,
                      account_id=None, account_name=None):
    """Khối bàn giao 1 lần (K3): chỉ render trong response ngay sau
    khi tạo bot, không có đường nào xem lại (UI không đọc secret).

    Khối thống nhất 5 dòng (N14): bot_id + endpoint gateway + queue
    secret + sync secret + account_id. Dòng địa chỉ receiver giữ lại
    để cấu hình syncer (ngoài 5 dòng K3). Bot chưa gắn thì khối KHÔNG
    có dòng nhãn + kèm chữ nhắc gắn tài khoản trước (S12)."""
    if account_id:
        label_line = (
            "<p><strong>Nhãn tài khoản (account_id) — ghi vào "
            "fetcher-config:</strong></p><code>%s</code>"
        ) % esc(account_id)
        label_note = (
            "<p>Khối này giao cho tài khoản %s. Chỉ giao cho đúng tài "
            "khoản đó qua kênh an toàn.</p>" % esc(account_name or account_id)
        )
    else:
        label_line = ""
        label_note = (
            "<p>Bot chưa gắn tài khoản — gắn tài khoản trước khi bàn giao "
            "để nhãn lấy tin đúng. Fetcher khi chưa có nhãn vẫn lấy tin "
            "bình thường nhưng không gửi kèm nhãn.</p>"
        )
    body = (
        "<h1>Bot đã tạo — bàn giao 1 lần</h1>"
        '<div class="banner">Bot ở trạng thái TẮT. Copy khối dưới đây '
        "NGAY và gửi RIÊNG cho Muse (chat riêng, không gửi vào nhóm). "
        "Khối này chỉ hiện 1 lần duy nhất — rời khỏi trang là mất, "
        "muốn xem lại phải đọc file trên máy tính.</div>"
        '<div class="secret-box">'
        "<p><strong>ID bot:</strong> %s</p>"
        "<p><strong>Địa chỉ gateway (lấy tin):</strong></p><code>%s</code>"
        "<p><strong>Queue secret:</strong></p><code>%s</code>"
        "<p><strong>Địa chỉ receiver (đồng bộ trả lời):</strong></p>"
        "<code>%s</code>"
        "<p><strong>Sync secret:</strong></p><code>%s</code>"
        "%s"
        "</div>"
        "%s"
        "<p>Sau khi Muse nhận đủ, phía Muse lấy tin lần đầu thành công "
        "thì nút Bật mới mở. Bot chỉ chạy sau khi chủ nhân bật tay.</p>"
        '<div class="actions"><a class="btn" href="/bots/%s">'
        "Tới trang bot</a></div>"
    ) % (
        esc(bot_id),
        esc(gateway_endpoint),
        esc(queue_secret),
        esc(receiver_endpoint),
        esc(sync_secret),
        label_line,
        label_note,
        esc(bot_id),
    )
    return layout("Bot đã tạo", body, csrf_token=csrf_token)


def replace_token_form_html(bot_id, display_name, errors, csrf_token):
    """Biểu mẫu thay token đã lưu (luồng 6 bước, bước 1–2)."""
    err_html = ""
    if errors:
        err_html = '<div class="field-error">%s</div>' % "<br>".join(
            esc(e) for e in errors)
    body = (
        "<h1>Thay token đã lưu — %s</h1>"
        "%s"
        "<p><strong>Bước 1:</strong> lấy token MỚI từ trang quản lý của "
        "nền tảng trước (Zalo: trang quản lý bot; Telegram: BotFather). "
        "Giao diện này chỉ ghi đè token đã lưu trên máy tính, KHÔNG tự "
        "thu hồi hay tạo token ở phía nền tảng.</p>"
        "<p><strong>Bước 2:</strong> dán token mới 2 lần khớp nhau, rồi "
        "xác nhận. Token mới sẽ được kiểm tra bằng getMe TRƯỚC khi lưu; "
        "token sai thì không lưu gì. Sau khi lưu, bot vào trạng thái "
        "\"chờ xác minh 2 đầu\" cho tới khi phía Muse cũng cập nhật "
        "xong.</p>"
        '<form method="post" action="/bots/%s/replace-token">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<label for="token1">Token mới</label>'
        '<input type="password" id="token1" name="token1" '
        'autocomplete="off" required>'
        '<label for="token2">Dán lại token mới</label>'
        '<input type="password" id="token2" name="token2" '
        'autocomplete="off" required>'
        "<p><label>"
        '<input type="checkbox" name="confirm" value="yes" required> '
        "Tôi đã lấy token mới từ trang quản lý và hiểu rằng token cũ "
        "trên máy này sẽ bị ghi đè (không giữ bản sao)."
        "</label></p>"
        '<div class="actions">'
        '<button class="primary-action" type="submit">Kiểm tra và lưu token mới'
        "</button>"
        ' <a class="btn" href="/bots/%s">Hủy</a></div>'
        "</form>"
    ) % (
        esc(display_name),
        err_html,
        esc(bot_id),
        esc(csrf_token),
        esc(bot_id),
    )
    return layout("Thay token đã lưu", body, csrf_token=csrf_token)


def pending_verification_html(bot_id, display_name, csrf_token):
    """Checklist chờ xác minh 2 đầu (bước 5–6 của luồng thay token)."""
    body = (
        "<h1>Chờ xác minh 2 đầu — %s</h1>"
        "<p>Token mới đã lưu trên máy tính này. Bot sẽ dùng token mới "
        "để nghe tin; phía Muse vẫn dùng token cũ để gửi tin cho tới "
        "khi chủ nhân cập nhật Vault.</p>"
        "<ul>"
        "<li>✅ Máy tính (nghe tin): <strong>xong</strong></li>"
        "<li>⏳ Phía Muse (gửi tin — Vault): <strong>chưa xong</strong></li>"
        "</ul>"
        "<p>Sau khi phía Muse cập nhật Vault xong, bấm nút dưới đây để "
        "đưa bot về trạng thái bình thường.</p>"
        '<form method="post" action="/bots/%s/confirm-vault">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<button class="primary-action" type="submit">Tôi đã cập nhật Vault '
        "phía Muse</button>"
        "</form>"
        '<div class="actions"><a class="btn" href="/bots/%s">Về trang bot'
        "</a></div>"
    ) % (
        esc(display_name),
        esc(bot_id),
        esc(csrf_token),
        esc(bot_id),
    )
    return layout("Chờ xác minh 2 đầu", body, csrf_token=csrf_token)


# ---------------------------------------------------------------------------
# Khu vực quản lý tài khoản (doc 08 §4, MA6): danh sách quản lý, KHÔNG
# phải đăng nhập nhiều người dùng. CẤM ô nhập mật khẩu/token của tài
# khoản Muse; CẤM hiện secret dưới mọi hình thức.
# ---------------------------------------------------------------------------

def _account_center_section_html(account_id, info, csrf_token):
    """Mục Trung tâm trong thẻ tài khoản (doc 10 §5.6, v0.2.4): 2 mức
    dừng tham gia + tham gia lại. ``info`` None -> không hiện mục (gọi
    cũ không kèm dữ liệu trung tâm vẫn chạy).

    - Đang tham gia: "Tạm ngưng tham gia" (POST trực tiếp — chỉ tắt cờ,
      giữ mã, bật lại là chạy ngay) + "Gỡ hẳn khỏi trung tâm" (link tới
      trang xác nhận 2 bước — tắt cờ + xoay mã, mã cũ chết).
    - Chưa tham gia + đã có mã: "Bật lại tham gia" (POST trực tiếp —
      chỉ bật cờ, giữ mã cũ; chỉ dùng khi trước đó chỉ tạm ngưng) +
      "Tham gia lại — cấp mã mới" (trang xác nhận — cấp mã mới, làm
      lại như tham gia mới; dùng khi đã gỡ hẳn hoặc mã cũ mất/lộ).
    - Chưa tham gia + chưa có mã: chỉ "Tham gia trung tâm" (cấp mã
      mới rồi bật cờ — cấp mã trước, bật cờ sau, Q-AM-01)."""
    if info is None:
        return ""
    participating = bool(info.get("participating"))
    secret_has = bool(info.get("secret_has"))
    issued_text = info.get("secret_issued_text")
    if secret_has:
        secret_line = "Mã bí mật: đã có"
        if issued_text:
            secret_line += " · Cấp ngày %s" % issued_text
    else:
        secret_line = "Mã bí mật: chưa có"
    status_line = "Trung tâm: Đang tham gia" if participating \
        else "Trung tâm: Chưa tham gia"
    parts = ['<div class="meta">%s · %s</div>'
             % (esc(status_line), esc(secret_line))]
    # (v0.6.0, T3l — doc 10 §3.8 §3c, quyết D10) Công tắc tắt thảo
    # luận từng tài khoản + màu chat riêng. Chỉ chủ nhân bật/tắt
    # (giao diện chỉ có 1 mật khẩu chủ nhân, không có người dùng
    # khác); tắt thì hub từ chối ngay 403 discussion_disabled
    # (phần receiver do thành viên schema làm — UI chỉ bật/tắt cờ +
    # ghi audit). Màu chat hiện kèm để chủ nhân đối chiếu với khung
    # chat.
    color = info.get("chat_color") or ""
    discussion_on = info.get("discussion_enabled", True)
    if not isinstance(discussion_on, bool):
        discussion_on = True
    discussion_line = (
        '<div class="meta">Thảo luận: %s · Màu chat: '
        '<span class="msg-color-dot" data-color="%s"></span>%s'
        "</div>"
        % ("Đang bật" if discussion_on else "Đã tắt",
           esc(color or "#475569"), esc(color or "—")))
    parts.append(discussion_line)
    toggle_label = ("Tắt thảo luận" if discussion_on
                    else "Bật thảo luận")
    toggle_value = "0" if discussion_on else "1"
    toggle_hint = (
        "Tắt: tài khoản này không còn được gọi vào thảo luận — hub từ "
        "chối ngay 403 discussion_disabled (không im lặng). Bật lại: "
        "tham gia thảo luận như thường."
        if discussion_on else
        "Đang tắt: tài khoản này không được gọi vào thảo luận. Bật "
        "lại để tham gia thảo luận như thường.")
    parts.append(
        '<div class="actions">'
        '<form class="inline" method="post" action="/accounts/%s/'
        'center/discussion">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<input type="hidden" name="enabled" value="%s">'
        '<button type="submit">%s</button></form></div>'
        '<div class="hint">%s</div>'
        % (esc(account_id), esc(csrf_token), toggle_value,
           esc(toggle_label), esc(toggle_hint)))
    if participating:
        parts.append(
            '<div class="actions">'
            '<form class="inline" method="post" action="/accounts/%s/'
            'center/pause">'
            '<input type="hidden" name="csrf_token" value="%s">'
            '<button type="submit">Tạm ngưng tham gia</button></form>'
            '<a class="btn" href="/accounts/%s/center/remove">'
            "Gỡ hẳn khỏi trung tâm</a></div>"
            '<div class="hint">Tạm ngưng: chỉ tắt cờ, giữ mã — bật lại '
            "là chạy ngay. Gỡ hẳn: tắt cờ + xoay mã, mã cũ chết.</div>"
            % (esc(account_id), esc(csrf_token), esc(account_id)))
    else:
        actions = []
        if secret_has:
            actions.append(
                '<form class="inline" method="post" action="/accounts/%s/'
                'center/resume">'
                '<input type="hidden" name="csrf_token" value="%s">'
                '<button type="submit">Bật lại tham gia</button></form>'
                % (esc(account_id), esc(csrf_token)))
        actions.append(
            '<a class="btn" href="/accounts/%s/center/join">%s</a>'
            % (esc(account_id),
               "Tham gia lại — cấp mã mới" if secret_has
               else "Tham gia trung tâm"))
        parts.append('<div class="actions">%s</div>' % "".join(actions))
        if secret_has:
            parts.append(
                '<div class="hint">Bật lại: giữ mã cũ — chỉ dùng khi '
                "trước đó chỉ tạm ngưng. Tham gia lại: cấp mã mới, làm "
                "lại như tham gia mới — dùng khi đã gỡ hẳn hoặc mã cũ đã "
                "mất/lộ.</div>")
        else:
            parts.append(
                '<div class="hint">Tham gia trung tâm: cấp mã mới rồi '
                "bật cờ (cấp mã trước, bật cờ sau).</div>")
    return "".join(parts)


def accounts_page_html(accounts, grouped, bot_names, csrf_token,
                       transfers_by_bot=None, center_info=None):
    """Trang danh sách tài khoản: mỗi tài khoản 1 thẻ (tên gọi,
    account_id, số bot gắn, danh sách bot -> trang chi tiết), form đổi
    tên inline, nút gỡ, mục Trung tâm (tạm ngưng/gỡ hẳn/tham gia lại).
    ``grouped``: {account_id|None: [bot_id]};
    ``bot_names``: {bot_id: display_name}; ``transfers_by_bot``:
    {bot_id: transfer} để ghi chú bot đang chuyển đi/đến;
    ``center_info``: {account_id: {"participating", "secret_has",
    "secret_issued_text", "discussion_enabled", "chat_color"}}
    (None = không hiện mục Trung tâm)."""
    transfers_by_bot = transfers_by_bot or {}
    cards = []
    account_map = (accounts or {}).get("accounts") or {}
    for account_id in sorted(account_map,
                             key=lambda aid: account_map[aid][
                                 "display_name"].lower()):
        record = account_map[account_id]
        bot_ids = grouped.get(account_id) or []
        bot_links = []
        for bot_id in bot_ids:
            note = ""
            transfer = transfers_by_bot.get(bot_id)
            if transfer:
                if transfer.get("to_account_id") == account_id:
                    note = " (đang chuyển đến)"
                elif transfer.get("from_account_id") == account_id:
                    note = " (đang chuyển đi)"
                else:
                    note = " (đang chuyển)"
            bot_links.append(
                '<li><a href="/bots/%s">%s</a>%s</li>'
                % (esc(bot_id), esc(bot_names.get(bot_id, bot_id)),
                   esc(note))
            )
        bot_list = ("<ul>%s</ul>" % "".join(bot_links)) if bot_links \
            else '<div class="hint">Chưa có bot nào gắn.</div>'
        center_section = _account_center_section_html(
            account_id,
            (center_info or {}).get(account_id)
            if isinstance(center_info, dict) else None,
            csrf_token)
        cards.append(
            '<div class="card" id="%s">'
            "<h3>%s</h3>"
            '<div class="meta">ID tài khoản: %s · %d bot</div>'
            "%s"
            '<div class="hint">%s</div>'
            "%s"
            '<form class="inline" method="post" action="/accounts/%s/rename">'
            '<input type="hidden" name="csrf_token" value="%s">'
            '<label class="sr-only" for="rename-%s">Tên gọi mới</label>'
            '<input type="text" id="rename-%s" name="display_name" '
            'value="%s" maxlength="100" required>'
            '<button type="submit">Đổi tên</button>'
            "</form>"
            '<form class="inline" method="post" action="/accounts/%s/delete">'
            '<input type="hidden" name="csrf_token" value="%s">'
            '<button type="submit">Gỡ tài khoản</button>'
            "</form></div>"
            % (
                esc(account_id),
                esc(record["display_name"]),
                esc(account_id),
                len(bot_ids),
                bot_list,
                esc(record.get("notes") or ""),
                center_section,
                esc(account_id),
                esc(csrf_token),
                esc(account_id),
                esc(account_id),
                esc(record["display_name"]),
                esc(account_id),
                esc(csrf_token),
            )
        )
    unassigned = grouped.get(None) or []
    unassigned_section = ""
    if unassigned:
        links = "".join(
            '<li><a href="/bots/%s">%s</a></li>'
            % (esc(bot_id), esc(bot_names.get(bot_id, bot_id)))
            for bot_id in unassigned)
        unassigned_section = (
            '<div class="card"><h3>Chưa gắn tài khoản</h3>'
            '<div class="meta">%d bot</div><ul>%s</ul></div>'
            % (len(unassigned), links)
        )
    if not cards and not unassigned_section:
        cards_html = (
            '<div class="notice">Chưa có tài khoản nào. Thêm tài khoản '
            "đầu tiên để gắn bot.</div>"
        )
    else:
        cards_html = "".join(cards) + unassigned_section
    body = (
        '<p><a href="/">← Màn hình chính</a></p>'
        "<h1>Tài khoản</h1>"
        "<p>Danh sách các tài khoản Muse đang quản lý bot. Đây chỉ là "
        "danh sách quản lý — giao diện vẫn dùng 1 mật khẩu chung, "
        "không thêm tài khoản đăng nhập hay phân quyền.</p>"
        '<div class="actions"><a class="btn" href="/accounts/new">'
        "Thêm tài khoản</a></div>"
        '<div class="cards">%s</div>'
    ) % cards_html
    return layout("Tài khoản", body, csrf_token=csrf_token)


def account_form_html(values, errors, csrf_token, preview_id=None):
    """Biểu mẫu thêm tài khoản: CHỈ tên gọi + ghi chú. CẤM ô nhập mật
    khẩu/token của tài khoản Muse. ``preview_id``: account_id xem
    trước khi lưu (hiện sau khi bấm "Xem trước ID")."""
    err_html = ""
    if errors:
        err_html = '<div class="field-error">%s</div>' % "<br>".join(
            esc(e) for e in errors)
    preview_html = ""
    previewed_input = ""
    if preview_id:
        preview_html = (
            '<div class="notice">ID tài khoản sẽ lưu: '
            "<strong>%s</strong> (tự sinh từ tên gọi, không đổi được "
            "sau khi tạo). Kiểm tra kỹ rồi bấm \"Lưu tài khoản\".</div>"
        ) % esc(preview_id)
        previewed_input = (
            '<input type="hidden" name="previewed_id" value="%s">'
            % esc(preview_id)
        )
    body = (
        "<h1>Thêm tài khoản</h1>%s%s"
        '<form method="post" action="/accounts/new">'
        '<input type="hidden" name="csrf_token" value="%s">'
        "%s"
        '<label for="display_name">Tên gọi (để phân biệt các tài khoản '
        "Muse)</label>"
        '<input type="text" id="display_name" name="display_name" '
        'value="%s" maxlength="100" required>'
        '<label for="notes">Ghi chú (tùy ý)</label>'
        '<input type="text" id="notes" name="notes" value="%s" '
        'maxlength="500">'
        '<div class="hint">Không cần mật khẩu hay token của tài khoản '
        "Muse. ID tài khoản tự sinh từ tên gọi và không đổi sau khi "
        "tạo; đổi tên gọi không đổi ID.</div>"
        '<div class="actions">'
        '<button type="submit" name="action" value="preview">Xem trước '
        "ID</button>"
        '<button class="primary-action" type="submit" name="action" '
        'value="create">Lưu tài khoản</button>'
        ' <a class="btn" href="/accounts">Hủy</a></div>'
        "</form>"
    ) % (
        err_html,
        preview_html,
        esc(csrf_token),
        previewed_input,
        esc(values.get("display_name", "")),
        esc(values.get("notes", "")),
    )
    return layout("Thêm tài khoản", body, csrf_token=csrf_token)


# ---------------------------------------------------------------------------
# Wizard chuyển bot (doc 09 §4) — 10 mốc, 1 điểm vào duy nhất ở trang
# chi tiết bot. CẤM hiện secret; CẤM hiện số getMe kỳ vọng ở bước 3.
# ---------------------------------------------------------------------------

def _transfer_progress_html(transfer):
    """Thanh tiến độ 10 mốc: tên mốc + trạng thái từng mốc."""
    steps = transfer.get("steps") or {}
    items = []
    for step_no in range(10):
        info = steps.get(str(step_no)) or {}
        status = info.get("status") or "locked"
        if status == "done":
            mark = "Đã xong"
        elif status == "current":
            mark = "Đang làm"
        elif status == "failed":
            mark = "Chưa đạt"
        else:
            mark = "Chưa mở"
        items.append(
            '<li class="step-%s"><span class="step-no">%d</span> %s '
            "<em>%s</em></li>"
            % (esc(status), step_no, esc(TRANSFER_STEP_TITLES[step_no]),
               esc(mark))
        )
    return '<ol class="transfer-progress">%s</ol>' % "".join(items)


def _transfer_hidden_fields(transfer, csrf_token):
    return (
        '<input type="hidden" name="csrf_token" value="%s">'
        '<input type="hidden" name="transfer_id" value="%s">'
        '<input type="hidden" name="revision" value="%s">'
        % (esc(csrf_token), esc(transfer.get("transfer_id")),
           esc(transfer.get("revision")))
    )


def transfer_unlabelled_guide_html(bot_id, display_name, csrf_token):
    """Trang hướng dẫn khi bot chưa gắn tài khoản (G1): không mở
    wizard chuyển bot, dẫn sang form Sửa bot. Không có form POST nào
    trên trang này (không còn đường nào tạo luồng)."""
    body = (
        "<h1>Chuyển bot %s</h1>"
        "<p>Bot chưa gắn tài khoản nào — không cần chuyển bot. Để gắn "
        "bot vào một tài khoản, mở form Sửa bot và chọn Tài khoản quản "
        "lý.</p>"
        '<div class="actions"><a class="btn" href="/bots/%s/edit">'
        "Mở form Sửa bot</a>"
        ' <a class="btn" href="/bots/%s">Về trang bot</a></div>'
    ) % (esc(display_name), esc(bot_id), esc(bot_id))
    return layout("Chuyển bot", body, csrf_token=csrf_token)


def transfer_start_page_html(bot_id, display_name, accounts, csrf_token,
                             current_owner, errors=None):
    """Form bắt đầu luồng chuyển: chọn tài khoản cũ + đích trong 1 POST
    (không trạng thái nháp — N18). Tài khoản đích phải có sẵn trong
    danh sách; tài khoản cũ điền sẵn từ nhãn hiện tại, sửa được."""
    err_html = ""
    if errors:
        err_html = '<div class="field-error">%s</div>' % "<br>".join(
            esc(e) for e in errors)
    from_options = []
    for account_id, name in accounts:
        selected = " selected" if account_id == current_owner else ""
        from_options.append(
            '<option value="%s"%s>%s (%s)</option>'
            % (esc(account_id), selected, esc(name), esc(account_id)))
    unlabelled_sel = " selected" if not current_owner else ""
    to_options = []
    for account_id, name in accounts:
        to_options.append(
            '<option value="%s">%s (%s)</option>'
            % (esc(account_id), esc(name), esc(account_id)))
    body = (
        "<h1>Chuyển bot %s sang tài khoản khác</h1>%s"
        '<div class="notice">Trong lúc chuyển, bot có thể chậm trả lời '
        "hoặc không trả lời. Không gộp việc đổi token hay cập nhật code "
        "vào lúc đang chuyển.</div>"
        '<form method="post" action="/bots/%s/transfer/start">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<label for="from_account_id">Tài khoản đang quản lý bot '
        "(nhãn lúc bắt đầu)</label>"
        '<select id="from_account_id" name="from_account_id">'
        '<option value=""%s>Chưa gắn tài khoản</option>%s</select>'
        '<label for="to_account_id">Tài khoản đích (phải có sẵn trong '
        "danh sách tài khoản)</label>"
        '<select id="to_account_id" name="to_account_id" required>%s'
        "</select>"
        '<div class="hint">Luồng chuyển có 10 mốc. Nhãn lúc bắt đầu và '
        "tài khoản cũ do chủ nhân khai đều được ghi lại để đối chiếu.</div>"
        '<div class="actions"><button class="secondary" type="submit">'
        "Bắt đầu chuyển</button>"
        ' <a class="btn" href="/bots/%s">Hủy</a></div>'
        "</form>"
    ) % (
        esc(display_name),
        err_html,
        esc(bot_id),
        esc(csrf_token),
        unlabelled_sel,
        "".join(from_options),
        "".join(to_options),
        esc(bot_id),
    )
    return layout("Chuyển bot", body, csrf_token=csrf_token)


def transfer_page_html(bot_id, display_name, transfer, step, context,
                       csrf_token, accounts, errors=None, eval_result=None):
    """Trang 1 mốc của wizard. ``context`` mang dữ liệu chỉ-đọc từng
    mốc (gateway_status, quiet, latest_seq, capability_present...).
    CẤM hiện secret dưới mọi hình thức."""
    err_html = ""
    if errors:
        err_html = '<div class="field-error">%s</div>' % "<br>".join(
            esc(e) for e in errors)
    names = dict(accounts)
    from_name = names.get(transfer.get("from_account_id"),
                          transfer.get("from_account_id") or "chưa gắn")
    to_name = names.get(transfer.get("to_account_id"),
                        transfer.get("to_account_id"))
    header = (
        "<h1>Chuyển bot %s</h1>"
        "<p>Từ <strong>%s</strong> sang <strong>%s</strong> — mốc %d: %s"
        "</p>%s"
        % (esc(display_name), esc(from_name), esc(to_name), step,
           esc(TRANSFER_STEP_TITLES[step]), _transfer_progress_html(transfer))
    )
    step_html = _TRANSFER_STEP_RENDERERS[step](
        bot_id, transfer, context, csrf_token, eval_result)
    rollback_html = ""
    if not _transfer_step_done_local(transfer, 8):
        rollback_html = (
            ' <a class="btn" href="/bots/%s/transfer/rollback">Quay lại '
            "tài khoản cũ</a>" % esc(bot_id)
        )
    footer = (
        '<div class="actions">%s'
        ' <a class="btn" href="/bots/%s/transfer/cancel">Hủy luồng '
        "chuyển</a>"
        ' <a class="btn" href="/bots/%s">Về trang bot</a></div>'
        % (rollback_html, esc(bot_id), esc(bot_id))
    )
    return layout("Chuyển bot — mốc %d" % step,
                  header + err_html + step_html + footer,
                  csrf_token=csrf_token)


def _transfer_step_done_local(transfer, step_no):
    steps = transfer.get("steps") or {}
    info = steps.get(str(step_no)) or {}
    return info.get("status") == "done"


def _step_form_open(bot_id, transfer, csrf_token, step):
    return (
        '<form method="post" action="/bots/%s/transfer/step">'
        "%s"
        '<input type="hidden" name="step" value="%d">'
        % (esc(bot_id), _transfer_hidden_fields(transfer, csrf_token),
           step)
    )


def _step_submit(label="Xong mốc này, sang mốc tiếp"):
    return (
        '<div class="actions"><button class="secondary" type="submit">%s'
        "</button></div></form>" % esc(label)
    )


def _render_step1(bot_id, transfer, context, csrf_token, eval_result):
    quiet, detail = context.get("quiet", (True, {}))
    if not isinstance(detail, dict):
        detail = {}
    gateway_status = context.get("gateway_status", "unknown")
    now = time.time()
    # 2.11 (D-2.11-4): dong lang viet lai tu `detail` cua
    # fetch_quiet_info_by_label (nhan cu + gio lay cuoi + luot khong
    # nhan), khong dung moc chung 90 giay nua.
    if detail.get("no_data"):
        quiet_line = ("Không đo được nhãn lấy tin (gateway chưa ghi nhãn) "
                      "— chỉ dựa vào xác nhận của chủ nhân.")
    elif quiet:
        quiet_line = ("Không còn nhãn cũ nào lấy tin trong 5 phút gần "
                      "nhất — đã lặng.")
    else:
        blocking = detail.get("blocking_labels") or []
        last_old = detail.get("last_old_seen")
        if blocking:
            if last_old is not None:
                when = time.strftime("%H:%M:%S",
                                     time.localtime(last_old))
                ago = max(0, int(now - last_old))
                quiet_line = (
                    "Nhãn %s vẫn đang lấy tin — lần cuối lúc %s (cách "
                    "đây %d giây)." % (", ".join(blocking), when, ago))
            else:
                quiet_line = "Nhãn %s vẫn đang lấy tin." % (
                    ", ".join(blocking))
            if detail.get("unlabelled_recent"):
                quiet_line += (
                    " Ngoài ra còn thấy lượt lấy tin không ghi nhãn "
                    "trong 5 phút gần nhất — kiểm tra fetcher chưa nâng "
                    "cấp (chưa ghi nhãn) rồi dừng nó.")
        else:
            quiet_line = (
                "Thấy lượt lấy tin không ghi nhãn trong 5 phút gần "
                "nhất — kiểm tra fetcher chưa nâng cấp (chưa ghi nhãn) "
                "rồi dừng nó.")
    if gateway_status == "active":
        gateway_line = (
            '<p>Trạng thái đo được: unit gateway đang active.</p>')
        gate_note = ""
    elif gateway_status == "inactive":
        gateway_line = (
            '<p class="notice"><strong>Unit gateway không active</strong> '
            "— fetcher không lấy được tin khi gateway chết. Không hoàn "
            "tất được mốc này khi gateway không active — bật lại "
            "gateway rồi thử lại.</p>")
        gate_note = ""
    else:
        gateway_line = (
            '<p class="notice"><strong>Không đo được trạng thái unit '
            "gateway</strong> — kết quả kiểm tra lặng-fetch có thể không "
            "đáng tin. Chỉ dựa vào xác nhận của chủ nhân ở dưới.</p>")
        gate_note = ""
    from_label = transfer.get("from_account_id")
    if from_label:
        confirm_text = (
            "Tôi đã dừng lấy tin ở tài khoản cũ và xác nhận không còn "
            "lấy tin.")
        # 2.11 (D-2.11-5): chen ten nhan cu vao cau huong dan.
        guide_line = (
            "Việc này cần làm ở phía tài khoản cũ (%s): dừng fetcher "
            "(tắt service hoặc xóa fetcher-config). Sau đó tick xác nhận."
            % from_label)
    else:
        confirm_text = (
            "Tôi xác nhận không có tài khoản nào đang lấy tin bot này.")
        guide_line = (
            "Việc này cần làm ở phía tài khoản cũ: dừng fetcher (tắt "
            "service hoặc xóa fetcher-config). Sau đó tick xác nhận.")
    return (
        "<h2>Mốc 1: Dừng lấy tin ở tài khoản cũ</h2>"
        "<p>%s</p>%s%s"
        "<p>%s</p>"
        "%s"
        '<label><input type="checkbox" name="confirm" value="yes"> %s'
        "</label>"
        "%s"
        % (esc(quiet_line), gateway_line, gate_note, esc(guide_line),
           _step_form_open(bot_id, transfer, csrf_token, 1),
           esc(confirm_text), _step_submit())
    )


def _render_step2(bot_id, transfer, context, csrf_token, eval_result):
    latest_seq = context.get("latest_seq")
    sample_at = context.get("latest_sample_at", "")
    seq_line = "Số thứ tự tin mới nhất trên PC: %s (lấy mẫu lúc %s)." % (
        esc(latest_seq) if latest_seq is not None else "không đo được",
        esc(sample_at))
    return (
        "<h2>Mốc 2: Kiểm tra xả hết ở tài khoản cũ</h2>"
        "<p>%s</p>"
        '<p class="hint">Kiểm tra số học trên số do chủ nhân nhập — '
        "không phải đối chiếu với sổ phía Muse.</p>"
        "%s"
        '<label for="cursor">Cursor cuối cùng phía Muse đã xử lý '
        "(số nguyên ≥ 0)</label>"
        '<input type="number" id="cursor" name="cursor" min="0" '
        'required>'
        "<p>Tin tồn chưa trả lời:</p>"
        '<label><input type="radio" name="pending_choice" '
        'value="all_done" checked> (i) Mọi tin đã xả hết — không còn tin '
        "tồn.</label><br>"
        '<label><input type="radio" name="pending_choice" '
        'value="to_new_account"> (ii) Còn tin tồn, để tài khoản mới trả '
        "lời.</label>"
        '<label for="pending_count">Số tin tồn chưa trả lời (bắt buộc '
        "khi chọn (ii))</label>"
        '<input type="number" id="pending_count" name="pending_count" '
        'min="0" value="0">'
        "%s"
        % (seq_line, _step_form_open(bot_id, transfer, csrf_token, 2),
           _step_submit())
    )


def _render_step3(bot_id, transfer, context, csrf_token, eval_result):
    note = ""
    if not context.get("capability_present", True):
        note = (
            '<p class="notice">Phía PC không có ảnh chụp mã số bot — mốc '
            "này sẽ qua bằng xác nhận, không đối chiếu được getMe.</p>")
    return (
        "<h2>Mốc 3: Chuyển credential</h2>"
        "<p>Gọi getMe ở tài khoản mới bằng token mới, rồi nhập mã số bot "
        "mà getMe trả về. Mã này được đối chiếu với ảnh chụp phía PC.</p>"
        "%s%s"
        '<label for="bot_code">Mã số bot từ getMe của tài khoản mới'
        "</label>"
        '<input type="text" id="bot_code" name="bot_code" '
        'autocomplete="off" required>'
        "%s"
        % (note, _step_form_open(bot_id, transfer, csrf_token, 3),
           _step_submit())
    )


def _render_step4(bot_id, transfer, context, csrf_token, eval_result):
    return (
        "<h2>Mốc 4: Chuyển secret</h2>"
        "<p>Giao queue secret và sync secret cho tài khoản mới qua kênh "
        "an toàn, và ghi account_id của tài khoản mới vào fetcher-config. "
        "Web UI không hiện secret và không có ô dán secret — không gửi "
        "secret qua đây.</p>"
        "%s"
        '<label><input type="checkbox" name="confirm" value="yes"> Tôi '
        "đã giao secret qua kênh an toàn và cấu hình kèm account_id "
        "đúng.</label>"
        "%s"
        % (_step_form_open(bot_id, transfer, csrf_token, 4),
           _step_submit())
    )


def _render_step5(bot_id, transfer, context, csrf_token, eval_result):
    cross_blocked = transfer.get("pending_reply_choice") == "to_new_account"
    branch_b_note = ""
    branch_b_disabled = ""
    if cross_blocked:
        branch_b_disabled = " disabled"
        branch_b_note = (
            '<p class="notice">Mốc 2 đã chọn "để tài khoản mới trả lời" '
            "nên mốc này không được chọn \"không chuyển lịch sử\" — tin "
            "dở phải nằm trong phần lịch sử chuyển.</p>")
    return (
        "<h2>Mốc 5: Chuyển lịch sử/trí nhớ</h2>"
        "%s%s"
        '<label><input type="radio" name="history_choice" '
        'value="with_history" checked> (a) Chuyển lịch sử/trí nhớ sang '
        "tài khoản mới.</label>"
        '<div class="hint">Tick đủ các mục đã chép:'
        "<br><label><input type=\"checkbox\" name=\"copy_cursor\" "
        "value=\"yes\"> Cursor</label>"
        "<br><label><input type=\"checkbox\" name=\"copy_ledger\" "
        "value=\"yes\"> Sổ vòng đời + sổ giao hàng + journal</label>"
        "<br><label><input type=\"checkbox\" name=\"copy_history\" "
        "value=\"yes\"> Lịch sử chat</label>"
        "<br><label><input type=\"checkbox\" name=\"copy_memory\" "
        "value=\"yes\"> Trí nhớ 3 lớp</label>"
        "<br><label><input type=\"checkbox\" name=\"copy_allowlist\" "
        "value=\"yes\"> Bản sao allowlist cuối</label>"
        "<br><label><input type=\"checkbox\" name=\"copy_prompt\" "
        "value=\"yes\"> Thẻ prompt của bot (prompts/cards/&lt;bot_id&gt;"
        ".md) — bắt buộc</label>"
        "<br>Kế toán của bot (accounting/&lt;bot_id&gt;/): "
        "<label><input type=\"radio\" name=\"accounting_choice\" "
        "value=\"copied\"> Đã chép</label> "
        "<label><input type=\"radio\" name=\"accounting_choice\" "
        "value=\"remeasure_d2\"> Ghi rõ D2 đo lại từ đầu</label></div>"
        '<label for="cursor_step5">Cursor của tài khoản mới sau khi chép '
        "(số nguyên ≥ 0)</label>"
        '<input type="number" id="cursor_step5" name="cursor_step5" '
        'min="0">'
        '<p class="hint">Cursor tài khoản mới bằng hoặc cũ hơn cursor '
        "mốc 2 thì qua (cũ hơn: fetcher lấy lại từ chỗ cũ, chống lặp "
        "chặn trùng). Mới hơn thì bị chặn.</p>"
        '<label><input type="radio" name="history_choice" '
        'value="without_history"%s> (b) Không chuyển lịch sử.</label>'
        "%s"
        "<div>"
        '<label><input type="checkbox" name="consequence" value="yes"> '
        "Bot sẽ quên chuyện cũ — tài khoản mới không có lịch sử và trí "
        "nhớ cũ.</label>"
        "<br><label><input type=\"checkbox\" name=\"new_prompt\" "
        "value=\"yes\"> Tài khoản mới đã tạo thẻ prompt mới cho bot "
        "(bắt buộc trước mốc 7).</label>"
        "<br>Điểm bắt đầu cursor của tài khoản mới: "
        "<label><input type=\"radio\" name=\"cursor_start_choice\" "
        "value=\"latest\"> Từ tin mới nhất</label> "
        "<label><input type=\"radio\" name=\"cursor_start_choice\" "
        "value=\"zero\"> Từ đầu (0)</label></div>"
        "%s"
        % (branch_b_note,
           _step_form_open(bot_id, transfer, csrf_token, 5),
           branch_b_disabled, "", _step_submit())
    )


def _render_step6(bot_id, transfer, context, csrf_token, eval_result):
    return (
        "<h2>Mốc 6: Đổi nhãn chủ quản</h2>"
        "<p>Web UI sẽ tự ghi nhãn chủ quản của bot thành tài khoản đích. "
        "Nhãn chỉ dùng để hiển thị và đối chiếu — đường xử lý tin không "
        "đọc nhãn này.</p>"
        "%s"
        "<p>Nhãn hiện tại phải đúng nhãn lúc bắt đầu luồng, nếu không "
        "mốc này bị chặn và phải hủy luồng, bắt đầu lại.</p>"
        "%s"
        % (_step_form_open(bot_id, transfer, csrf_token, 6),
           _step_submit("Đổi nhãn chủ quản sang tài khoản đích"))
    )


def _render_step7(bot_id, transfer, context, csrf_token, eval_result):
    marker = transfer.get("step7_marker") or ""
    attempt = transfer.get("step7_attempt") or 1
    failed = _transfer_step_status(transfer, 7) == "failed"
    failed_html = ""
    if failed:
        failed_html = (
            '<p class="notice"><strong>Lần thử trước chưa đạt.</strong> '
            "Bấm \"Thử lại\" để lấy mã test mới, hoặc quay lại tài khoản "
            "cũ.</p>")
    checks_html = ""
    if eval_result is not None:
        rows = []
        for label, check in (
                ("Lấy tin + nhãn đích", eval_result["check1"]),
                ("Không còn nhãn cũ", eval_result["check2"]),
                ("Tin test DM + trả lời", eval_result["check3"]),
                ("Bot đang bật, không tạm dừng", eval_result["check4"])):
            state = "Đạt" if check["pass"] else "Chưa đạt"
            rows.append(
                "<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (esc(label), esc(state), esc(check["message"])))
        checks_html = (
            '<table class="checks"><tr><th>Kiểm tra</th><th>Kết quả</th>'
            "<th>Chi tiết</th></tr>%s</table>" % "".join(rows))
        if eval_result.get("timed_out"):
            checks_html += (
                '<p class="notice">Đã quá 15 phút chờ của lần thử này — '
                "bấm \"Thử lại\" để lấy mã mới.</p>")
    check3 = (eval_result or {}).get("check3") or {}
    dm_tick_html = ""
    if check3.get("marker_found") and check3.get("dm_path") is None \
            and not check3.get("pass"):
        dm_tick_html = (
            '<label><input type="checkbox" name="dm_confirm" '
            "value=\"yes\"> Tôi đã gửi tin test từ DM (tin nhắn riêng) "
            "với bot.</label><br>")
    adjudication_html = ""
    if check3.get("needs_adjudication"):
        adjudication_html = (
            "<p>Phân xử 2 nhóm trả lời:</p>"
            '<label><input type="radio" name="adjudication" '
            'value="single_multi_part"> 1 trả lời nhiều phần, không '
            "trùng</label><br>"
            '<label><input type="radio" name="adjudication" '
            'value="duplicate_rollback"> Có trùng — quay lại tài khoản '
            "cũ</label><br>")
    attested_html = ""
    if check3.get("attested_open") and not check3.get("pass"):
        attested_html = (
            '<label><input type="checkbox" name="attested" value="yes"> '
            "Tôi đã thấy bot trả lời trong cuộc trò chuyện DM (xác nhận "
            "thủ công — chỉ dùng khi đã có nhãn đích được kiểm chứng)."
            "</label><br>")
    return (
        "<h2>Mốc 7: Bật lấy tin ở tài khoản mới + kiểm chứng</h2>"
        "<p>Bật fetcher ở tài khoản mới, rồi gửi 1 tin nhắn chữ (DM) cho "
        "bot chứa đúng mã test dưới đây. Tin test sẽ ở lại trong lịch "
        "sử chat.</p>"
        "%s"
        '<p>Mã test (lần thử %d): <strong class="marker">%s</strong></p>'
        "%s"
        '<form method="post" action="/bots/%s/transfer/step">'
        "%s"
        '<input type="hidden" name="step" value="7">'
        "%s%s%s"
        '<div class="actions"><button class="secondary" type="submit">'
        "Hoàn tất mốc 7</button></div></form>"
        '<form method="post" action="/bots/%s/transfer/retry-step7">'
        "%s"
        '<div class="actions"><button type="submit">Thử lại (mã mới)'
        "</button></div></form>"
        '<div class="actions"><a class="btn" href="/bots/%s/transfer">'
        "Làm tươi kết quả kiểm tra</a></div>"
        % (failed_html, attempt, esc(marker), checks_html,
           esc(bot_id),
           _transfer_hidden_fields(transfer, csrf_token),
           dm_tick_html, adjudication_html, attested_html,
           esc(bot_id),
           _transfer_hidden_fields(transfer, csrf_token),
           esc(bot_id))
    )


def _transfer_step_status(transfer, step_no):
    steps = transfer.get("steps") or {}
    info = steps.get(str(step_no)) or {}
    return info.get("status") or "locked"


def _render_step8(bot_id, transfer, context, csrf_token, eval_result):
    return (
        "<h2>Mốc 8: Dọn dẹp ở tài khoản cũ</h2>"
        "<p>Dọn từng mục ở tài khoản cũ, tick riêng từng mục:</p>"
        "%s"
        '<label><input type="checkbox" name="cleanup_fetcher" '
        "value=\"yes\"> Đã xóa fetcher-config của bot ở tài khoản cũ."
        "</label><br>"
        '<label><input type="checkbox" name="cleanup_syncer" '
        "value=\"yes\"> Đã xóa cấu hình syncer của bot ở tài khoản cũ."
        "</label><br>"
        '<label><input type="checkbox" name="cleanup_vault" '
        "value=\"yes\"> <strong>Đã gỡ credential khỏi Vault ở tài khoản "
        "cũ — mục này bắt buộc, không được bỏ qua.</strong></label>"
        "<p>Nơi giữ lịch sử cũ ở tài khoản cũ (bắt buộc chọn):</p>"
        '<label><input type="radio" name="history_keep_decision" '
        'value="kept_at_X"> Giữ nguyên ở tài khoản cũ</label><br>'
        '<label><input type="radio" name="history_keep_decision" '
        'value="deleted_at_X"> Xóa ở tài khoản cũ</label><br>'
        '<label><input type="radio" name="history_keep_decision" '
        'value="archived_at"> Lưu trữ ở tài khoản cũ</label>'
        '<p class="notice">Cảnh báo trùng lấy chỉ bắt được tài khoản cũ '
        "còn lấy tin; gỡ credential khỏi Vault không có cơ chế kiểm "
        "chứng — tự kiểm tra kỹ trước khi tick.</p>"
        "<p>Sau khi mốc 8 xong, chế độ tạm ẩn cảnh báo trùng lấy kết "
        "thúc — nếu cả 2 tài khoản còn lấy tin thì cảnh báo hiện lại "
        "ngay.</p>"
        "%s"
        % (_step_form_open(bot_id, transfer, csrf_token, 8),
           _step_submit())
    )


def _render_step9(bot_id, transfer, context, csrf_token, eval_result):
    reply_tier = transfer.get("step7_reply_tier")
    if reply_tier == "sync":
        reply_line = "Tin test đã đi đủ tuyến, trả lời đã đồng bộ về."
    elif reply_tier == "attested":
        reply_line = ("Chủ nhân đã xác nhận thấy bot trả lời trong cuộc "
                      "trò chuyện DM.")
    else:
        reply_line = "Chưa có bằng chứng trả lời."
    label_tier = transfer.get("step7_label_tier")
    if label_tier == "verified":
        label_line = "Nhãn đích đã được kiểm chứng."
    else:
        label_line = "Nhãn đích chưa được kiểm chứng."
    return (
        "<h2>Mốc 9: Ghi nhật ký chuyển</h2>"
        "<p>Tóm tắt luồng chuyển:</p>"
        "<ul>"
        "<li>%s</li>"
        "<li>%s</li>"
        "<li>Lịch sử: %s</li>"
        "<li>Cursor mốc 2: %s; cursor mốc 5: %s</li>"
        "</ul>"
        "<p>Bấm \"Hoàn tất chuyển\" để đóng luồng. Nhật ký chuyển được "
        "giữ lại để đối chiếu.</p>"
        "%s"
        "%s"
        % (esc(reply_line), esc(label_line),
           esc(transfer.get("history_choice") or "—"),
           esc(transfer.get("cursor_step2")),
           esc(transfer.get("cursor_step5")),
           _step_form_open(bot_id, transfer, csrf_token, 9),
           _step_submit("Hoàn tất chuyển"))
    )


_TRANSFER_STEP_RENDERERS = {
    1: _render_step1,
    2: _render_step2,
    3: _render_step3,
    4: _render_step4,
    5: _render_step5,
    6: _render_step6,
    7: _render_step7,
    8: _render_step8,
    9: _render_step9,
}


def transfer_rollback_page_html(bot_id, display_name, transfer, csrf_token,
                                reason_presets, errors=None):
    """Form quay lại tài khoản cũ: checklist đối xứng cho Y + quyết
    giữ/xóa cấu hình + lý do bắt buộc (1 POST nguyên tử ở tầng app)."""
    err_html = ""
    if errors:
        err_html = '<div class="field-error">%s</div>' % "<br>".join(
            esc(e) for e in errors)
    reason_options = "".join(
        '<option value="%s">%s</option>' % (esc(p), esc(p))
        for p in reason_presets)
    # N8: nhắc rõ bản sao lịch sử tại Y vẫn còn sau khi quay lại (chỉ
    # khi bước 5 đã chọn có-chuyển).
    history_note = ""
    if transfer.get("history_choice") == "with_history":
        history_note = (
            '<p class="notice">Bản sao lịch sử tại tài khoản mới vẫn còn '
            "sau khi quay lại — chủ nhân tự quyết giữ/xóa; nếu chuyển "
            "lại sau thì bước 5 phải làm lại từ đầu vì cursor ở tài "
            "khoản cũ có thể đã tiến.</p>")
    return layout(
        "Quay lại tài khoản cũ",
        "<h1>Quay lại tài khoản cũ — bot %s</h1>%s"
        "<p>Quay lại là 1 thao tác nguyên tử: dừng lấy tin ở tài khoản "
        "mới, trả nhãn về tài khoản cũ (nếu mốc 6 đã xong), bật lại lấy "
        "tin ở tài khoản cũ. Không xoay secret trong lúc quay lại.</p>"
        "%s"
        '<form method="post" action="/bots/%s/transfer/rollback">'
        "%s"
        '<label><input type="checkbox" name="stop_fetcher_y" '
        "value=\"yes\"> Đã dừng fetcher ở tài khoản mới.</label><br>"
        '<label><input type="checkbox" name="no_pending_y" '
        "value=\"yes\"> Không còn tin tồn ở tài khoản mới.</label><br>"
        '<label><input type="checkbox" name="reenable_x" '
        "value=\"yes\"> Đã bật lại lấy tin ở tài khoản cũ.</label>"
        "<p>Cấu hình ở tài khoản mới:</p>"
        '<label><input type="radio" name="vault_decision" '
        'value="removed"> Đã gỡ cấu hình/credential ở tài khoản mới.'
        "</label><br>"
        '<label><input type="radio" name="vault_decision" '
        'value="kept"> Giữ cấu hình ở tài khoản mới — Y vẫn giữ secret '
        "— nợ dọn.</label>"
        '<p class="hint">Không ghi secret vào lý do.</p>'
        '<label for="reason_choice">Lý do (bắt buộc)</label>'
        '<select id="reason_choice" name="reason_choice">%s</select>'
        '<label for="reason_note">Ghi chú thêm (tổng lý do tối đa 200 '
        "ký tự)</label>"
        '<input type="text" id="reason_note" name="reason_note" '
        'maxlength="200">'
        '<div class="actions"><button class="danger" type="submit">'
        "Quay lại tài khoản cũ</button>"
        ' <a class="btn" href="/bots/%s/transfer">Hủy</a></div>'
        "</form>"
        % (esc(display_name), err_html, history_note, esc(bot_id),
           _transfer_hidden_fields(transfer, csrf_token),
           reason_options, esc(bot_id)),
        csrf_token=csrf_token)


def transfer_cancel_page_html(bot_id, display_name, transfer, step6_done,
                              step8_done, csrf_token, reason_presets,
                              errors=None):
    """Form hủy luồng chuyển (bước 1 của xác nhận 2 bước): lý do bắt
    buộc + câu xác nhận mở rộng sau mốc 6 (S7)."""
    err_html = ""
    if errors:
        err_html = '<div class="field-error">%s</div>' % "<br>".join(
            esc(e) for e in errors)
    extended = ""
    if step6_done:
        extended = (
            '<p class="notice"><strong>Nhãn chủ quản đã đổi sang tài '
            "khoản mới và sẽ GIỮ NGUYÊN sau khi hủy.</strong> Muốn trả "
            "nhãn về tài khoản cũ thì dùng \"Quay lại\" thay vì hủy.")
        if not step8_done:
            extended += (
                " Mốc 8 CHƯA làm — tài khoản cũ vẫn giữ secret và cấu "
                "hình, cần dọn thủ công sau khi hủy.</p>")
        else:
            extended += "</p>"
    reason_options = "".join(
        '<option value="%s">%s</option>' % (esc(p), esc(p))
        for p in reason_presets)
    return layout(
        "Hủy luồng chuyển",
        "<h1>Hủy luồng chuyển — bot %s</h1>%s%s"
        "<p>Hủy luồng không tự dọn dẹp gì ở 2 tài khoản. Lý do là bắt "
        "buộc.</p>"
        '<form method="post" action="/bots/%s/transfer/cancel">'
        "%s"
        '<p class="hint">Không ghi secret vào lý do.</p>'
        '<label for="reason_choice">Lý do (bắt buộc)</label>'
        '<select id="reason_choice" name="reason_choice">%s</select>'
        '<label for="reason_note">Ghi chú thêm (tổng lý do tối đa 200 '
        "ký tự)</label>"
        '<input type="text" id="reason_note" name="reason_note" '
        'maxlength="200">'
        '<div class="actions"><button class="danger" type="submit">'
        "Tiếp tục</button>"
        ' <a class="btn" href="/bots/%s/transfer">Hủy</a></div>'
        "</form>"
        % (esc(display_name), err_html, extended, esc(bot_id),
           _transfer_hidden_fields(transfer, csrf_token),
           reason_options, esc(bot_id)),
        csrf_token=csrf_token)
