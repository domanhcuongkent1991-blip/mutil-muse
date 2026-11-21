"""Tài nguyên tĩnh của Web UI: CSS + JS, phục vụ qua ``/static/``.

CSP bắt buộc ``script-src 'self'; style-src 'self'`` (05 §6) nên
trang KHÔNG dùng <script>/<style> inline và KHÔNG dùng handler inline
(``onclick=``...): mọi JS/CSS đều từ 2 file này, cùng nguồn với trang.

JS chỉ làm tươi dữ liệu: lấy fragment HTML đã escape ở phía server
về thay vào trang (polling 5 giây, log 15 giây, dừng khi tab ẩn,
backoff khi mất kết nối, bỏ qua lượt làm tươi khi người dùng đang
gõ/chọn trong fragment). Fragment chat trung tâm tự cuộn xuống cuối
khi đang ở gần cuối (T3b). Mọi thao tác đổi trạng thái đều là form
POST thường — tắt JS vẫn dùng được đầy đủ.

Hình thức theo phong cách C đã chọn (UI-STYLE-C.html): màu nền tảng
rõ (Zalo xanh dương, Telegram xanh ngọc), huy hiệu nền tảng, thẻ và
nút to, mobile-first.
"""

APP_CSS = """
:root {
  --zalo: #0068ff; --telegram: #0097a7;
  --green: #137333; --green-bg: #ceead6;
  --yellow: #7a4a00; --yellow-bg: #feefc3;
  --red: #a50e0e; --red-bg: #f9dedc;
  --gray: #5f6368; --gray-bg: #f1f3f4;
  --blue: #1a56db; --blue-bg: #d3e3fd;
  --border: #e3e3e2; --text: #292827; --page: #f2f0eb;
  --wine: #421d24; --purple: #714cb6; --purple-bg: #ede7f6;
  --muted: var(--gray);
  --surface: #ffffff; --surface2: #faf9f6;
  --accent: #714cb6;
  --purple-bg-strong: #d6c8ef;
  --primary-bg: #421d24; --primary-text: #ffffff;
  --secondary-bg: #ede7f6; --secondary-text: #714cb6;
  --msg-owner-bg: #ede7f6; --msg-account-bg: #e3e3e2;
  --invert-bg: #292827; --invert-text: #ffffff;
  --log-bg: #0d1117; --log-text: #e6edf3;
  --danger-text: #ffffff;
  --dot-ok: #34a853; --dot-stale: #fbbc04; --dot-dead: #ea4335;
  --typing: #16a34a;
}
/* Theme toi Hades (v0.6.0, T3k — phong cach C): chi ghi token DOI gia
   tri; token giu nguyen ca 2 theme khong ghi lai (frag-04 §1.1).
   Tint toi D-Q-V060-40: --gray-bg rgba(127,147,196,.14),
   --blue-bg rgba(56,189,248,.14). */
html[data-theme="hades"] {
  --green: #34d399; --green-bg: rgba(52,211,153,.14);
  --yellow: #f59e0b; --yellow-bg: rgba(245,158,11,.14);
  --red: #f87171; --red-bg: rgba(248,113,113,.14);
  --gray: #7f93c4; --gray-bg: rgba(127,147,196,.14);
  --blue: #38bdf8; --blue-bg: rgba(56,189,248,.14);
  --border: #1e2a52; --text: #dbe7ff; --page: #04060f;
  --surface: #0d1530; --surface2: #111a38;
  --accent: #38bdf8;
  --purple-bg: #12204a;
  --primary-bg: #38bdf8; --primary-text: #0b1220;
  --secondary-bg: #12204a; --secondary-text: #a5d8ff;
  --msg-owner-bg: #2a2140; --msg-account-bg: #1b2740;
  --invert-bg: #dbe7ff; --invert-text: #0b1220;
  --danger-text: #0b1220;
}
/* Ghi de rieng trong theme toi (frag-04 §2.7 — chu trang tren nen mau
   giu nguyen khong dat o theme toi; khong giai quyet duoc bang doi
   gia tri token). Them 1 quy tac thiet ke §2.7 bo sot: chu tab Canh
   bao active (trang tren --red toi chi 2,77:1) cung phai thanh chu
   toi (6,77:1) — cung mau §2.7, ghi ro o BAN-GIAO-T3k. */
html[data-theme="hades"] .tab .badge { color: #0b1220; }
html[data-theme="hades"] .tab.active[data-tab="alerts"] .badge {
  background: #0b1220; color: var(--red);
}
html[data-theme="hades"] .tab.active[data-tab="alerts"] {
  color: #0b1220;
}
html[data-theme="hades"] .plat { color: #0b1220; }
html[data-theme="hades"] .plat.zalo { color: #fff; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 12px 40px; color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  font-size: 18px; line-height: 1.55; background: var(--page);
}
@media (min-width: 700px) {
  body { max-width: 1100px; margin: 0 auto; }
}
@media (min-width: 1000px) {
  body { max-width: 1600px; }
}
a { color: var(--accent); }
h1 { font-size: 28px; margin: 16px 0 8px; font-weight: 500; }
h2 { font-size: 23px; margin: 24px 0 8px; font-weight: 500; }
h3 { font-size: 21px; margin: 16px 0 6px; font-weight: 500; }
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
}
.topbar {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline;
  border-bottom: 1px solid var(--border); padding: 12px 0;
}
@media (min-width: 700px) {
  .topbar {
    position: sticky; top: 0; background: var(--page); z-index: 5;
  }
}
.topbar .fresh { color: var(--gray); font-size: 15px; }
/* An mac dinh chip canh bao du lieu cu (P0 CSP — thay cho
   thuoc tinh style display:none inline; JS an/hien bang CSSOM, xem
   refreshOne trong APP_JS). */
.is-hidden { display: none; }
/* (SUA B4 + L2, 21/11) Thuoc tinh hidden PHAI an phan tu: luat
   tac gia `button { display: inline-block; }` (ben duoi) thang
   luat `[hidden] { display: none }` cua UA stylesheet (author >
   UA bat ke specificity), lai them `position: absolute` bien
   display thanh block — vien "N tin moi" co hidden=true van
   hien (do that tren PC: computed display:block), bam khong
   bien mat (B4). Quy tac nay (chuan cua cac bo reset) khoi phuc
   ngu nghia hidden cho MOI phan tu, ke ca nut va badge phong
   (JS bat/tat bang el.hidden). */
[hidden] { display: none !important; }
/* Khoang cach 16px tren nut submit (P0 CSP — thay cho
   thuoc tinh style margin-top:16px inline o trang dang nhap + form bot). */
.mt-16 { margin-top: 16px; }
.banner {
  border: 1px solid var(--border); border-left: 8px solid var(--yellow);
  background: var(--yellow-bg); padding: 12px; margin: 12px 0;
  border-radius: 12px;
}
.banner.red { border-left-color: var(--red); background: var(--red-bg); }
.banner.green { border-left-color: var(--green); background: var(--green-bg); }
.banner.gray { border-left-color: var(--gray); background: var(--gray-bg); }
.notice {
  border: 1px dashed var(--border); padding: 12px; margin: 12px 0;
  border-radius: 12px; background: var(--surface); font-size: 16px;
}
/* --- Tab (V2 §1): link render phía máy chủ; dưới màn hình điện thoại,
   trên màn hình máy tính. --- */
.tabs {
  display: flex; gap: 6px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px; padding: 6px; margin: 12px 0;
}
.tab {
  flex: 1; display: flex; align-items: center; justify-content: center;
  gap: 6px; min-height: 54px; padding: 8px 6px; border-radius: 12px;
  font-size: 18px; font-weight: 700; color: var(--text);
  text-decoration: none; text-align: center;
}
.tab.active[data-tab="all"] { background: var(--invert-bg); color: var(--invert-text); }
.tab.active[data-tab="zalo"] { background: var(--zalo); color: #fff; }
.tab.active[data-tab="telegram"] { background: var(--telegram); color: #0b1220; }
.tab.active[data-tab="alerts"] { background: var(--red); color: #fff; }
.tab.active[data-tab="center"] { background: var(--purple); color: #fff; }
.tab .badge {
  background: var(--red); color: #fff; border-radius: 999px;
  font-size: 14px; font-weight: 800; padding: 2px 9px; line-height: 1.4;
}
.tab.active[data-tab="alerts"] .badge { background: #fff; color: var(--red); }
@media (max-width: 699px) {
  body.has-tabs { padding-bottom: 130px; }
  .tabs {
    position: fixed; left: 10px; right: 10px; bottom: 10px; z-index: 10;
    margin: 0; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
    gap: 4px; padding: 6px 4px;
  }
  /* 6 tab tren man 360px (T3i v0.6.0 — them tab Chat): chu 12px,
     nhan dai ("Trung tam", "Telegram") co the xuong 2 dong trong
     tab; thanh tab van nam gon trong dem chan 130px, khong che
     noi dung (nhu quy tac cu o 5 tab). */
  .tab {
    min-width: 0; padding: 8px 2px; font-size: 12px; font-weight: 700;
  }
  .tab .badge { font-size: 12px; padding: 1px 6px; }
}
/* --- Node bat/tat (T3, v0.2.3): tieu de node + nut chevron (form
   POST thuong) + dong tom tat khi node dong. --- */
.node { margin: 12px 0; }
@media (min-width: 1000px) {
  .node, .stats { margin: 16px 0; }
  .cards { gap: 16px; }
}
.node-head {
  display: flex; align-items: center; gap: 8px; margin: 16px 0 8px;
}
.node-head h2 { margin: 0; flex: 1; }
.node-toggle { min-height: 44px; min-width: 44px; padding: 6px 12px; }
.node-summary { color: var(--gray); font-size: 16px; margin: 4px 0 12px; }
/* --- Huy hiệu số lượng trên tiêu đề node (đợt mẫu rẻ Hades) --- */
.node-badge {
  background: var(--secondary-bg); color: var(--secondary-text);
  border-radius: 999px; font-size: 13px; font-weight: 700;
  padding: 1px 8px; line-height: 1.5; white-space: nowrap;
  margin-left: 6px;
}
.node-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
/* --- Dải số (V2 §1) --- */
.stats {
  display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px;
  margin: 12px 0;
}
@media (min-width: 700px) {
  .stats { grid-template-columns: repeat(5, 1fr); }
}
.stat {
  background: var(--surface2); border: 1px solid var(--border); border-radius: 14px;
  padding: 12px; text-align: center;
}
.stats.zalo .stat { border-left: 6px solid var(--zalo); }
.stats.telegram .stat { border-left: 6px solid var(--telegram); }
.stat .num { font-size: 32px; font-weight: 800; line-height: 1.2; }
.stat .lab { font-size: 15px; color: var(--gray); }
/* --- Thẻ bot (V2 §2, phong cách C) --- */
.cards { display: grid; grid-template-columns: 1fr; gap: 12px; }
@media (min-width: 700px) {
  .cards { grid-template-columns: repeat(2, 1fr); }
}
@media (min-width: 1000px) {
  .cards { grid-template-columns: repeat(3, 1fr); }
}
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px; padding: 16px;
}
.card[data-platform="zalo"] { border-left: 8px solid var(--zalo); }
.card[data-platform="telegram"] { border-left: 8px solid var(--telegram); }
.card h3 { margin-top: 0; display: flex; flex-wrap: wrap; gap: 8px;
  align-items: center; }
.card .meta { color: var(--gray); font-size: 16px; }
.card .kv { font-size: 17px; margin: 6px 0; word-break: break-word; }
.cardlink { display: inline-block; margin-top: 8px; font-weight: 700; padding: 7px 0; min-height: 44px; }
.plat {
  display: inline-block; padding: 3px 10px; border-radius: 6px;
  font-size: 13px; font-weight: 800; letter-spacing: 0.06em; color: #fff;
  background: var(--gray); vertical-align: middle;
}
.plat.zalo { background: var(--zalo); }
.plat.telegram { background: var(--telegram); color: #0b1220; }
details { margin-top: 8px; }
details summary { cursor: pointer; font-weight: 700; color: var(--accent);
  min-height: 44px; }
.chip {
  display: inline-block; padding: 3px 12px; border-radius: 999px;
  font-size: 15px; font-weight: 700; margin-right: 6px;
}
.chip.green { background: var(--green-bg); color: var(--green); }
.chip.yellow { background: var(--yellow-bg); color: var(--yellow); }
.chip.red { background: var(--red-bg); color: var(--red); }
.chip.gray { background: var(--gray-bg); color: var(--gray); }
.chip.blue { background: var(--blue-bg); color: var(--blue); }
/* --- Hàng chip trạng thái gateway/receiver (đợt mẫu rẻ Hades) --- */
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
button, .btn {
  min-height: 52px; min-width: 52px; padding: 12px 20px; font-size: 17px;
  font-weight: 700; border: 1px solid var(--border); border-radius: 12px;
  background: var(--surface); cursor: pointer; text-decoration: none; color: var(--text);
  display: inline-block; line-height: 1.4; text-align: center;
}
button.primary-action, .btn.primary-action {
  background: var(--primary-bg); border-color: var(--primary-bg);
  color: var(--primary-text);
}
button.secondary, .btn.secondary {
  background: var(--secondary-bg); border-color: var(--secondary-bg);
  color: var(--secondary-text);
}
button.danger, .btn.danger {
  background: var(--red); border-color: var(--red);
  color: var(--danger-text);
}
button:disabled, .btn.disabled {
  opacity: 0.5; cursor: not-allowed;
}
button.small { min-height: 44px; padding: 6px 14px; font-size: 16px; }
form.inline { display: inline; }
label { display: block; font-weight: 600; margin: 12px 0 4px; }
input[type="text"], input[type="password"], input[type="search"],
textarea, select {
  width: 100%; padding: 12px; font-size: 17px;
  border: 1px solid var(--border); border-radius: 12px; background: var(--surface);
  color: var(--text);
}
textarea { min-height: 90px; font-family: inherit; }
.hint { color: var(--gray); font-size: 15px; margin-top: 4px; }
.error { color: var(--red); font-weight: 600; }
.field-error {
  border: 2px solid var(--red); background: var(--red-bg);
  padding: 12px; border-radius: 12px; margin: 12px 0;
}
/* --- Lọc hoạt động / cảnh báo (V2 §4, §5) --- */
.filters {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin: 12px 0;
}
.filters form {
  display: flex; flex-wrap: wrap; gap: 8px; flex: 1; align-items: center;
}
.filters select { width: auto; min-width: 160px; flex: 0 1 auto; }
.filters input[type="search"] { flex: 1; min-width: 180px; }
/* Checkbox lọc "cần chủ nhân" (đợt mẫu rẻ Hades): đích chạm 44px. */
.filters label.need-owner {
  display: inline-flex; align-items: center; gap: 8px;
  min-height: 44px; font-weight: 700; margin: 0;
}
.filters input[type="checkbox"] { width: 22px; height: 22px; }
.chipbtn {
  display: inline-flex; align-items: center; min-height: 48px;
  padding: 8px 16px; border: 1px solid var(--border); border-radius: 999px;
  background: var(--surface); font-size: 16px; font-weight: 700; color: var(--text);
  text-decoration: none;
}
.chipbtn.active { background: var(--invert-bg);
  border-color: var(--invert-bg); color: var(--invert-text); }
/* --- Hoạt động / lịch sử --- */
.msg {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 10px 12px; margin: 8px 0;
}
.msg .head { color: var(--gray); font-size: 15px; margin-bottom: 4px; }
.msg .body { white-space: pre-wrap; word-break: break-word; }
.msg.out { background: var(--surface2); }
.msg .status { font-size: 15px; margin-top: 4px; }
/* --- Trang hiện mã bí mật (v0.6.0, T3j — ngoại lệ F18(3)) --- */
/* Mã dài ~43 ký tự không dấu cách: bắt xuống dòng ở mọi chỗ để
   không tràn màn 360px (kiểm 360px — PLAN T3j tiêu chí 11). */
.view-secret-value { overflow-wrap: anywhere; word-break: break-all;
  font-size: 15px; }
/* --- Chat trung tâm kiểu Telegram (v0.2.5, T3b — Q-C1…Q-C5) --- */
/* Bong bóng 2 bên: chủ nhân phải/nền tím nhạt, tài khoản trái/nền
   xám; tối đa 85% chiều rộng, bo 16px (phong cách Superhuman). */
.msg--owner, .msg--account { max-width: 85%; border-radius: 16px; }
@media (min-width: 1000px) {
  .msg--owner, .msg--account { max-width: 75%; }
}
.msg--owner {
  margin-left: auto; background: var(--msg-owner-bg);
  border-color: var(--msg-owner-bg);
}
.msg--account {
  margin-right: auto; background: var(--msg-account-bg);
  border-color: var(--msg-account-bg);
}
.msg .msg-head { color: var(--gray); font-size: 15px; margin-bottom: 4px; }
.msg .msg-head strong { color: var(--text); }
.msg--owner .msg-head strong { color: var(--accent); }
.msg .msg-task { color: var(--gray); font-size: 15px; margin-bottom: 4px; }
.msg .msg-body { white-space: pre-wrap; word-break: break-word; }
/* Chip file đính kèm: nền trắng, viền 1px, bo 16px. */
.center-file-chip {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px; padding: 8px 10px; margin-top: 6px; font-size: 15px;
  word-break: break-all;
}
/* Anh thu nho xem truoc (phuong an A, 21/11 — SPEC TT16): khung
   co dinh 160x120, cat 4:3 o giua (object-fit: cover), bo 12px;
   bam mo anh day du o tab moi. Chu "Dang tai anh…" nam SAU anh
   (absolute, inset 0): dang tai thi anh trong suot nen thay chu;
   anh ve thi che chu — khung khong doi co, khong nhay bo cuc.
   Tai loi: JS (APP_JS) an ca khung, chi con chip. Dien thoai
   <= 720px: 120x90. */
.center-file-preview {
  position: relative; display: block; width: 160px; height: 120px;
  margin-top: 6px; border: 1px solid var(--border); border-radius: 12px;
  overflow: hidden; background: var(--surface2);
}
.center-file-preview-loading {
  position: absolute; inset: 0; display: flex; align-items: center;
  justify-content: center; color: var(--muted); font-size: 13px;
  text-align: center; padding: 0 8px;
}
.center-file-preview-img {
  position: relative; z-index: 1; display: block; width: 100%;
  height: 100%; object-fit: cover;
}
/* Ô nhập dính dưới khung chat (Q-C4): chỉ dính trong mục chat
   (section bọc riêng), không dính cả trang. */
.center-chat-section { position: relative; }
.center-chat-form {
  position: sticky; bottom: 0; background: var(--page);
  padding: 8px 0 12px; z-index: 1;
}
/* (DOT A #10, 21/11 — SUA L1 bo sung) Mobile <=699px: thanh tab
   co dinh chiem 78px day viewport (cao 68px + bottom: 10px) nen
   form sticky bottom: 0 cu bi tab che kin nut "Gửi" (do o 390px:
   nut [306,769,72,52], tab [10,766,370,68], tab z-index 10 >
   form 1). Day form len 90px = 78px + khe 12px. NHUNG sticky
   bottom CHI co tac dung khi cuon trang du de dua day form len
   duong dinh 754 — o vi tri doc tu nhien form van nam o flow
   (day 970 > viewport 844) nen nut van de tab 12px (L1, do that
   10:12 21/11). Vi vay L1 sua goc bang cach rut ngan form + hop
   chat 40vh (xem khoi <=720px ben duoi) de vi tri flow cua nut
   da sach tab; bottom: 90px giu lai lam luoi an toan khi cuon
   xuong day. Chi ap mobile vi desktop tab khong co dinh
   (in-flow) — desktop giu bottom: 0. */
@media (max-width: 699px) {
  .center-chat-form { bottom: 90px; }
}
.center-chat-form textarea { min-height: 48px; }
/* 360px: textarea 1 dòng riêng, hàng nút: Đính kèm trái, Gửi phải. */
.chat-form-row {
  display: flex; gap: 8px; align-items: center; margin-top: 8px;
}
.chat-form-row .file-label {
  flex: 1; min-width: 0; margin: 0; font-weight: 400;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  cursor: pointer; color: var(--accent);
}
.chat-form-row input[type="file"] {
  position: absolute; width: 1px; height: 1px; opacity: 0;
  overflow: hidden;
}
.chat-form-row button { flex: 0 0 auto; }
/* --- Danh sach phong chat trung tam (v0.3.0, T3c) --- */
/* Moi hang phong la nut submit trong form POST (khong phai link),
   nen reset dang nut thanh hang danh sach; phong dang mo nen tim
   nhat; phong luu tru chu mo + nhan "lưu trữ". */
.center-room-list { display: flex; flex-direction: column; gap: 6px;
  margin: 8px 0; }
.center-room-list .room-form { margin: 0; }
.room-row {
  display: flex; align-items: center; gap: 8px; width: 100%;
  text-align: left; padding: 8px 10px; border: 1px solid var(--border);
  border-radius: 12px; background: var(--surface); color: var(--text);
  font: inherit; cursor: pointer;
}
.room-row--active { background: var(--purple-bg); border-color: var(--purple-bg-strong); }
.room-row .meta { color: var(--muted); }
/* Huy hieu so tin chua doc: vien nho, nen do ruou, chu trang. */
.room-badge {
  margin-left: auto; min-width: 20px; height: 20px; padding: 0 6px;
  border-radius: 10px; background: var(--wine); color: #fff;
  font-size: 12px; line-height: 20px; text-align: center;
}
/* Huy hieu "@" (tag chu nhan chua doc, v0.3.0 T3d — P7): nen tim,
   chu trang; nam sat huy hieu so (khong tu day sang phai). */
.room-badge--mention { margin-left: 4px; background: var(--purple); }
/* Ten duoc tag trong bong bóng (v0.3.0, T3d — DESIGN §8.3): dam,
   tim dam, nen tim nhat; overflow-wrap de khong tran bong bóng
   o man 360px. */
.msg-mention {
  font-weight: 500; color: var(--secondary-text);
  background: var(--secondary-bg);
  border-radius: 4px; padding: 0 2px; overflow-wrap: anywhere;
}
/* Bong bóng co tin tag chu nhan (v0.3.0, T3d): vien trai tim 4px. */
.msg--mention-owner { border-left: 4px solid var(--purple); }
/* --- Thao luan tai khoan (v0.6.0, T3l — doc 10 §3.8 §6a/§6b/§4b) --- */
/* Cham mau tai khoan gui trong dau bong bóng (theo chat_color trong
   accounts.json; chu nhan #57534e). Cham typing xanh la cua T3i
   (.room-typing-dot) GIU NGUYEN, khong doi mau. */
.msg-color-dot {
  display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  margin-right: 6px; vertical-align: 1px; flex: 0 0 auto;
}
/* O loc tin theo nguoi gui tren dau khung chat tab Chat: hang rieng,
   xuong dong duoc o man 360px; select khong tran khung. */
.center-sender-filter {
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  margin: 8px 0;
}
.center-sender-filter select { min-width: 0; max-width: 100%; }
/* Dong "vong da dung" tren dau khung chat (khi vong hien tai cua
   phong da cham 5 luot). */
.center-round-stopped {
  background: var(--gray-bg); border: 1px solid var(--border);
  border-radius: 12px; padding: 8px 10px; margin: 8px 0;
  font-size: 15px;
}
/* O "Gửi cho:" chon nhieu (select multiple): hien ~5 hang, vua
   man 360px (khong tran ngang; chieu cao tu theo size=5). */
#center-hoi-to[multiple] { min-height: 96px; }
/* --- Tab Chat rieng, mo hinh A (v0.6.0, T3i — DOI Q-T3-05) --- */
/* Bo cuc 2 cot: trai danh sach phong 300px + phai khung chat,
   khoang cach 2 cot 16px; dien thoai <= 720px xep doc. */
.center-chat-layout {
  display: flex; gap: 16px; align-items: flex-start;
}
.center-chat-rooms { flex: 0 0 300px; min-width: 0; }
.center-chat-main { flex: 1; min-width: 0; }
.center-chat-room-head { margin: 8px 0 4px; font-size: 18px; }
/* Dong "dang tra loi…" duoi ten phong (T3h + T3i tieu chi 24):
   hien khi worker dang tra loi trong phong dang mo; tu bien mat
   sau 90 giay (TTL typing phia hub). */
.center-typing-line { color: var(--gray); font-size: 15px;
  min-height: 24px; margin: 0 0 4px; }
/* Cham tren ten phong trong danh sach phong khi worker dang tra
   loi o phong do. */
.room-typing-dot {
  display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: var(--typing); margin-left: 4px; flex: 0 0 auto;
}
/* Hop chat: quy tac goc 65vh, toi thieu 400px (dien thoai <= 720px:
   40vh/320px — SUA L1 21/11, da duoc chu nhan chot), 1 co, khong
   nut phong to/thu nho. (B2, 21/11) Tren desktop tab Chat hop nay
   bi ghi de bang calc(100vh - 266px) trong .center-chat-section
   (xem ben duoi); 65vh chi con hieu luc cho hop TINH trang chi
   tiet viec (ngoai section). */
.center-chat-box-wrap { position: relative; }
/* (SUA B3/B4 + L2, 21/11 — THAY anchor sticky cu) Vien "N tin
   moi" + nut "Ve cuoi" la con TRUC TIEP cua wrap (position:
   relative, KHONG cuon) nen neo absolute theo wrap: luon nam
   co dinh trong khung hien thi cua hop, khong troi theo noi
   dung khi cuon hop hay cuon trang chinh. Anchor sticky cu
   (con cuoi cua hop cuon, height: 0) DA TROI that — video B3
   vien xuong duoi day hop ~300px, do L2 o 390px vien y=869 +
   nut y=917 nam ngoai viewport 844. Khong con
   .center-chat-anchor; JS chen bong bóng bang appendChild. */
.center-chat-box {
  height: 65vh; min-height: 400px; overflow-y: auto;
  border: 1px solid var(--border); border-radius: 16px;
  background: var(--surface); padding: 12px;
}
.center-chat-box--static { margin: 8px 0; }
/* Phuong an B2 (21/11 — THIET-KE-KHUNG-CHAT-B-21-11.md §3.3): hop
   chat tab Chat cao truc tiep bang calc, form day du giu nguyen
   nam duoi hop, trang cuon de toi form (danh doi da chot §1.4).
   H = chrome tren + dem day trang, tinh lai tu CSS that (UC-B-01):
   topbar 69 + h1 67 + thanh tab 90 + dem day 40 = 266px. Hop cao
   co dinh theo calc(100vh - 266px) nen margin-collapse h1/tab chi
   doi khoang cach tren hop, khong doi chieu cao hop; clientHeight
   thuc ~812px o 1080p (814 - 2px vien tren+duoi, box-sizing:
   border-box), trong dai G-BRW B-02 800-830px. (Ghi chu B2-G01:
   vien .tabs 1px x2 lam tab thuc ~92px, H chat ~268px, lech 2px
   — khong can doi code.) Tien to
   .center-chat-section la BAT BUOC: hop tinh trang chi tiet viec
   (ngoai section) giu 65vh/400px. Khong flex, khong JS, khong doi
   hang JS (D-B-03/D-B-04/D-B-05). */
@media (min-width: 721px) {
  .center-chat-section .center-chat-box {
    height: calc(100vh - 266px); min-height: 400px;
  }
}
/* Dong dan o cuoi tab Trung tam sang tab Chat (T3i). */
.center-chat-lead { font-size: 16px; font-weight: 500; margin: 16px 0; }
/* Vien "N tin moi" (v0.6.0, T3i): nen do ruou, chu trang, bo 16px,
   dem 8px 16px, chu 14px, weight 500; noi tren hop chat.
   (v0.6.0, NS-K21-2 — D17; SUA B3 doi moc 21/11) Cach day hop
   81px = 68px cu (frag-02 §2.4: vien khong cham nut "Ve cuoi")
   + 13px (padding-bottom 12px + border 1px cua hop) vi moc
   gio la wrap (day ngoai cua hop) thay cho anchor (day trong
   cua noi dung hop) — giu dung vi tri hien thi cu khi hop
   cuon xuong day. Khe vien-nut giu 12px: 81 - (25 + 44) = 12. */
.center-new-pill {
  position: absolute; left: 50%; bottom: 81px;
  transform: translateX(-50%); z-index: 2; pointer-events: auto;
  background: var(--wine); color: #fff; border: none; border-radius: 16px;
  padding: 8px 16px; font-size: 14px; font-weight: 500;
  min-height: 36px; min-width: 0;
}
/* Nut "Ve cuoi" 44px x 44px (Q-V060-08): chi cuon, khong danh
   dau da doc. (SUA B3, 21/11) bottom 25px = 12px cu + 13px
   (padding-bottom + border cua hop), nhu vien. */
.center-scroll-bottom {
  position: absolute; right: 12px; bottom: 25px; z-index: 2;
  pointer-events: auto;
  width: 44px; height: 44px; min-width: 44px; min-height: 44px;
  padding: 0; border-radius: 50%; background: var(--surface);
  border: 1px solid var(--border); color: var(--accent); font-size: 20px;
}
/* Dong "Da toi tin cu nhat con luu…" o dau hop khi het tin cu. */
.center-oldest-line {
  text-align: center; color: var(--gray); font-size: 15px;
  padding: 8px 0;
}
/* Huy hieu "@" tren tab Chat (tin tag chu nhan chua doc). */
.tab .badge--mention { background: var(--purple); }
@media (max-width: 720px) {
  .center-chat-layout { flex-direction: column; }
  .center-chat-rooms { flex: none; width: 100%; }
  /* (SUA L1, 21/11) Hop 50vh -> 40vh: day toan bo form len
     ~84px (o 844px) de nut "Gửi" sach thanh tab co dinh o vi
     tri doc tu nhien (do that 390px truoc sua: nut de tab 12px,
     day form tran viewport 126px). Hop van du cho vai bong bóng;
     cuon trong hop khong doi. */
  .center-chat-box { height: 40vh; min-height: 320px; }
  /* (SUA L1, 21/11) Rut ngan form o mobile: o "Gửi cho:" chi
     cao 48px (1 hang, cuon noi — chon nhieu van mo bang bo chon
     goc cua trinh duyet, khong mat chuc nang); an 2 ghi chu phu
     (F18 + doi phong mat file) chi o mobile, van hien day du o
     desktop; giu lai ghi chu cach chon nhieu + canh bao mat khau.
     Cung sticky bottom:90px (<=699px) giu form sach tab khi
     cuon xuong day. */
  #center-hoi-to[multiple] { height: 48px; min-height: 48px; }
  .center-chat-form .hint--mobile-hide { display: none; }
  /* (Xem truoc anh phuong an A, 21/11) Thu nho 120x90 o mobile. */
  .center-file-preview { width: 120px; height: 90px; }
}
/* --- Link chung trong tab Chat (21/11 — D-LC-12/13/14/18/19/20) --- */
/* Khung muc: cung ngon ngu voi danh sach phong (nen var(--surface),
   vien 1px var(--border), bo 16px), cach danh sach phong 16px.
   Chi dung token co san, khong them mau moi (D-LC-13). */
.shared-links {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px; margin-top: 16px; padding: 12px;
}
.shared-links-head {
  display: flex; align-items: center; gap: 8px;
}
.shared-links-head h3 { margin: 0; flex: 1; min-width: 0; }
.shared-links-count { color: var(--gray); font-size: 14px; }
.shared-links-toggle-form { margin: 0; }
.shared-links-toggle {
  min-width: 44px; min-height: 44px; padding: 8px 12px;
}
/* Nhan loai bang CHU (Drive/Docs/Sheets/Slides/Link), khong glyph
   icon: token co san, cap da kiem chung o T3k (toi 10,42:1, sang
   5,09:1 — D-LC-13c). */
.shared-link-kind {
  background: var(--secondary-bg); color: var(--secondary-text);
  border-radius: 8px; padding: 4px 8px; font-size: 13px;
  font-weight: 600; flex: 0 0 auto; align-self: flex-start;
}
.shared-links-list {
  list-style: none; margin: 12px 0 0; padding: 0;
}
.shared-link-row {
  display: flex; align-items: flex-start; gap: 8px;
  min-height: 44px; padding: 6px 0;
  border-top: 1px solid var(--border);
}
.shared-link-main { flex: 1; min-width: 0; }
.shared-link-main a {
  display: block; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; color: var(--accent);
}
.shared-link-domain {
  display: block; font-size: 13px; color: var(--gray);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.shared-link-delete { margin: 0; flex: 0 0 auto; }
.shared-link-delete-btn {
  width: 44px; height: 44px; min-width: 44px; min-height: 44px;
  padding: 0; font-size: 20px; line-height: 1;
}
.shared-links-add { margin-top: 12px; }
.shared-links-add label { display: block; margin: 8px 0 4px; }
.shared-links-add input { width: 100%; }
/* Focus ring cho nut/link moi theo token chung (khong them mau
   moi — D-LC-18/G-11): outline bang --accent. */
.shared-links a:focus-visible,
.shared-links button:focus-visible,
.shared-links input:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px;
}
@media (max-width: 720px) {
  /* Chua ai bam bao gio (unset): mobile mac dinh DONG — an danh
     sach + form (desktop van mo). Khi da bam mo thi danh sach
     cuon noi bo toi da 200px (SUA THEO GIA LAP LC-NS-01, 21/11:
     160px la SUY LUAN D-LC-19, do that UC-L-10 thay hang link cao
     61px nen 160px chi lo ~2,6 hang; 200px lo ~3,3 hang, gan dung
     y do ~3,5 hang); form them nam NGOAI khung cuon. */
  .shared-links[data-state="unset"] .shared-links-list,
  .shared-links[data-state="unset"] .shared-links-add {
    display: none;
  }
  .shared-links-list { max-height: 200px; overflow-y: auto; }
}
/* --- Cảnh báo (V2 §5) --- */
.alert {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 12px; margin: 8px 0;
}
.alert[data-platform="zalo"] { border-left: 8px solid var(--zalo); }
.alert[data-platform="telegram"] { border-left: 8px solid var(--telegram); }
.alert .meta { color: var(--gray); font-size: 15px; }
/* --- Dải sức khỏe + bảng ngày (trang chi tiết) --- */
.health { display: flex; flex-wrap: wrap; gap: 3px; margin: 8px 0; }
.dot {
  display: inline-block; width: 14px; height: 14px; border-radius: 50%;
}
.dot.ok { background: var(--dot-ok); }
.dot.stale { background: var(--dot-stale); }
.dot.dead { background: var(--dot-dead); }
.dot.unknown { background: var(--border); }
table { border-collapse: collapse; width: 100%; }
th, td {
  border-bottom: 1px solid var(--border); padding: 10px 8px;
  text-align: left; vertical-align: top; font-size: 16px;
}
table.daily th { background: var(--surface); }
pre.log {
  background: var(--log-bg); color: var(--log-text); padding: 12px;
  border-radius: 12px;
  font-size: 14px; overflow-x: auto; white-space: pre-wrap;
  word-break: break-word;
}
.pager { display: flex; gap: 8px; align-items: center; margin: 16px 0; }
.secret-box {
  border: 2px solid var(--yellow); background: var(--yellow-bg);
  padding: 12px; border-radius: 12px; margin: 12px 0;
}
.secret-box code {
  display: block; word-break: break-all; background: var(--surface);
  padding: 8px; border: 1px solid var(--border); border-radius: 8px;
  margin: 6px 0; font-size: 15px;
}
dl.config { margin: 8px 0; }
dl.config dt { font-weight: 600; }
dl.config dd { margin: 0 0 8px; }
ul.alerts { list-style: none; padding: 0; margin: 8px 0; }
ul.alerts li { margin: 6px 0; }
"""

APP_JS = """
// Làm tươi fragment: polling, dừng khi tab ẩn, backoff khi mất kết nối,
// bỏ qua lượt làm tươi khi người dùng đang gõ/chọn trong fragment.
(function () {
  "use strict";
  var STALE_AFTER_MS = 60000;

  function hasFocus(el) {
    var active = document.activeElement;
    return active && el.contains(active) &&
      /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName);
  }

  // (P0 CSP — doi SỬA LỖI UI, 21/11/2026) To mau cham tai khoan
  // (.msg-color-dot) tu thuoc tinh data-color qua CSSOM: gan qua
  // el.style.* bang JS KHONG bi CSP chan (chi thuoc tinh style
  // trong HTML moi bi chan). Goi sau moi lan noi dung chua cham
  // mau duoc render/chen vao trang — xem 4 diem goi ben duoi.
  function paintColorDots(root) {
    if (!root || !root.querySelectorAll) { return; }
    root.querySelectorAll(".msg-color-dot[data-color]")
      .forEach(function (dot) {
        dot.style.background = dot.getAttribute("data-color");
      });
  }

  // (Xem truoc anh phuong an A, 21/11) Anh thu nho loi (404/file
  // bi xoa): an ca khung preview, chi con chip file ben duoi.
  // Khong dung onerror inline (CSP cam handler inline) — lang
  // nghe tap trung o document (su kien error khong noi bot nen
  // dung capture). Anh loi truoc khi script chay thi quet 1 lan
  // o DOMContentLoaded (complete + naturalWidth 0).
  function hideBrokenPreview(img) {
    var wrap = img.closest ? img.closest(".center-file-preview") : null;
    if (wrap) { wrap.hidden = true; }
  }
  document.addEventListener("error", function (event) {
    var target = event.target;
    if (target && target.classList &&
        target.classList.contains("center-file-preview-img")) {
      hideBrokenPreview(target);
    }
  }, true);
  document.addEventListener("DOMContentLoaded", function () {
    var imgs = document.querySelectorAll(".center-file-preview-img");
    for (var i = 0; i < imgs.length; i++) {
      if (imgs[i].complete && imgs[i].naturalWidth === 0) {
        hideBrokenPreview(imgs[i]);
      }
    }
  });

  // (Link chung, D-LC-18/G-11 — SUA THEO AUDIT A-NS-01, 21/11)
  // Khi trang thai thu gon la "unset", may chu khong biet co man
  // hinh nguoi xem nen luon render aria-expanded="false" theo
  // mobile-dong; tren desktop (>= 721px, nguoc voi media query
  // max-width: 720px ben CSS) muc dang MO san nen sua thanh
  // "true" cho dung trang thai hien thi that (trinh doc man
  // hinh). Chi sua thuoc tinh aria, khong doi nhan nut hay
  // data-state; mobile giu nguyen "false".
  document.addEventListener("DOMContentLoaded", function () {
    if (!window.matchMedia ||
        !window.matchMedia("(min-width: 721px)").matches) { return; }
    document.querySelectorAll(
      '.shared-links[data-state="unset"] .shared-links-toggle')
      .forEach(function (btn) {
        btn.setAttribute("aria-expanded", "true");
      });
  });

  function refreshOne(el, delayMs, failures) {
    var url = el.getAttribute("data-fragment-url");
    if (!url) { return; }
    if (document.visibilityState === "hidden") {
      schedule(el, delayMs, 0);
      return;
    }
    if (hasFocus(el)) {
      // Đang gõ ô tìm kiếm / chọn bộ lọc: để yên, hẹn lượt sau để
      // nội dung đang nhập không bị fragment mới xóa mất.
      schedule(el, delayMs, failures);
      return;
    }
    // P1 (sua sau audit PC): luu gia tri cac o nhap trong fragment
    // + id phan tu dang focus truoc khi thay innerHTML, de poll
    // khong xoa chu nguoi dung vua go nhung chua gui (vi du go
    // "xin chao" roi blur — truoc day node #q-filter bi thay moi
    // nen gia tri ve rong). Dang go (focus) thi da duoc hasFocus()
    // bo qua o tren, khong toi day.
    var savedValues = {};
    var savedFocusId = null;
    // (NS-01, nhom B 21/11/2026) Luu them vi tri con tro + tick
    // checkbox: code cu chi luu value nen sau poll con tro ve dau/
    // cuoi o, va 2 checkbox khong co id (need_owner, overdue) mat
    // tick khi poll toi. Checkbox khoa theo name (khong them id de
    // khoi lo trung id); con tro chi luu cho o nhap text.
    var savedCarets = {};
    var savedChecked = {};
    var formFields = el.querySelectorAll("input, select, textarea");
    for (var i = 0; i < formFields.length; i++) {
      if (formFields[i].id) {
        savedValues[formFields[i].id] = formFields[i].value;
        // Guard (a): doc selectionStart co the nem InvalidStateError
        // voi input checkbox/hidden/file — boc try/catch, chi luu
        // khi doc duoc so (input text/search/textarea).
        try {
          if (typeof formFields[i].selectionStart === "number") {
            savedCarets[formFields[i].id] = {
              start: formFields[i].selectionStart,
              end: formFields[i].selectionEnd,
            };
          }
        } catch (e) { /* o nhap khong ho tro chon text: bo qua */ }
      }
      if (formFields[i].type === "checkbox" && formFields[i].name) {
        savedChecked[formFields[i].name] = formFields[i].checked;
      }
    }
    var activeEl = document.activeElement;
    if (activeEl && el.contains(activeEl) && activeEl.id) {
      savedFocusId = activeEl.id;
    }
    fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (resp) {
        if (!resp.ok) { throw new Error("HTTP " + resp.status); }
        return resp.text();
      })
      .then(function (html) {
        if (hasFocus(el)) {
          // Nguoi dung bat dau go trong luc fetch dang chay: de yen,
          // khong thay fragment de khoi xoa chu vua go.
          schedule(el, delayMs, failures);
          return;
        }
        el.innerHTML = html;
        paintColorDots(el);
        // Khoi phuc gia tri + focus da luu truoc khi thay fragment.
        for (var savedId in savedValues) {
          var savedNode = el.querySelector("#" + savedId);
          if (savedNode && "value" in savedNode) {
            if (savedNode.tagName === "SELECT") {
              var hasOption = false;
              for (var j = 0; j < savedNode.options.length; j++) {
                if (savedNode.options[j].value === savedValues[savedId]) {
                  hasOption = true;
                  break;
                }
              }
              if (hasOption) { savedNode.value = savedValues[savedId]; }
            } else {
              savedNode.value = savedValues[savedId];
            }
          }
        }
        if (savedFocusId) {
          var focusNode = el.querySelector("#" + savedFocusId);
          if (focusNode && focusNode.focus) { focusNode.focus(); }
        }
        // (NS-01) Khoi phuc tick checkbox theo name (tra cuu trong
        // fragment — el.querySelector — nhu guard (d), khong dung
        // document.getElementById toan cuc).
        var savedBoxes = el.querySelectorAll('input[type="checkbox"]');
        for (var k = 0; k < savedBoxes.length; k++) {
          if (savedBoxes[k].name
              && savedBoxes[k].name in savedChecked) {
            savedBoxes[k].checked = savedChecked[savedBoxes[k].name];
          }
        }
        // (NS-01) Khoi phuc vi tri con tro sau khi da gan lai value
        // (gan value dat lai selection ve cuoi) va focus; kep trong
        // do dai value moi de khong vuot qua cuoi o.
        for (var caretId in savedCarets) {
          var caretNode = el.querySelector("#" + caretId);
          if (caretNode && caretNode.setSelectionRange) {
            try {
              var caretLen = caretNode.value
                ? caretNode.value.length : 0;
              caretNode.setSelectionRange(
                Math.min(savedCarets[caretId].start, caretLen),
                Math.min(savedCarets[caretId].end, caretLen));
            } catch (e) { /* o moi khong ho tro chon text */ }
          }
        }
        el.setAttribute("data-last-ok", String(Date.now()));
        var stamp = document.getElementById("data-freshness");
        if (stamp) {
          stamp.textContent = "Dữ liệu cập nhật lúc " +
            new Date().toLocaleTimeString("vi-VN");
        }
        var stale = document.getElementById("stale-warning");
        if (stale) { stale.style.display = "none"; }
        schedule(el, delayMs, 0);
      })
      .catch(function () {
        var next = Math.min(delayMs * Math.pow(2, failures + 1), 60000);
        var lastOk = parseInt(el.getAttribute("data-last-ok") || "0", 10);
        if (Date.now() - lastOk > STALE_AFTER_MS) {
          var stale = document.getElementById("stale-warning");
          if (stale) { stale.style.display = "block"; }
        }
        schedule(el, next, failures + 1);
      });
  }

  function schedule(el, delayMs, failures) {
    window.setTimeout(function () {
      refreshOne(el, delayMs, failures);
    }, delayMs);
  }

  // Tab Chat (v0.6.0, T3i): hop chat co dinh + polling delta +
  // vien "N tin moi" + nut "Ve cuoi" + tin hieu "dang tra loi".
  // Hop chat KHONG qua polling fragment chung nua (khong co
  // data-fragment-url): JS rieng o day tu di polling delta
  // (after_seq) + tai tin cu (before_seq) + /api/center/unread,
  // cung nhip 5 giay, khong tao nhip rieng.
  var CHAT_NEAR_BOTTOM_PX = 150; // cach day <= 150px la "sat day"
  var CHAT_LOAD_TOP_PX = 24;     // scrollTop <= 24px thi tai tin cu
  var CHAT_MAX_BUBBLES = 300;    // Q-V060-06: gioi han ve ~300 bong bóng
  var CHAT_POLL_MS = 5000;

  function chatCsrfToken() {
    var input = document.querySelector('input[name="csrf_token"]');
    return input ? input.value : "";
  }

  // Tach fragment chat tra ve thanh {meta, bubbles}: the meta an
  // [data-chat-meta] mang data-has-more/data-oldest-seq/
  // data-newest-seq; cac bong bóng la cac the .msg (moi bong bóng
  // co data-seq).
  // (SUA B1 — phong thu them) Bo qua node .notice ("Chưa có tin
  // nhắn nào.") khi tach bong bóng: fragment delta khong bao gio
  // duoc them o trong vao hop chat, ke ca khi may chu cu vo tinh
  // tra o trong.
  function chatParseFragment(html) {
    var tmp = document.createElement("div");
    tmp.innerHTML = html;
    var meta = tmp.querySelector("[data-chat-meta]");
    var bubbles = [];
    Array.prototype.forEach.call(tmp.childNodes, function (node) {
      if (node.nodeType === 1 && !node.hasAttribute("data-chat-meta")
          && !(node.classList
               && node.classList.contains("notice"))) {
        bubbles.push(node);
      }
    });
    return { meta: meta, bubbles: bubbles };
  }

  function initCenterChat() {
    var box = document.getElementById("center-chat-box");
    if (!box || !box.hasAttribute("data-center-chat-box")) { return; }
    var room = box.getAttribute("data-room") || "chung";
    var archived = box.getAttribute("data-archived") === "1";
    var oldestSeq = parseInt(box.getAttribute("data-oldest-seq") || "", 10);
    var newestSeq = parseInt(box.getAttribute("data-newest-seq") || "", 10);
    if (isNaN(oldestSeq)) { oldestSeq = null; }
    if (isNaN(newestSeq)) { newestSeq = null; }
    var hasMore = box.getAttribute("data-has-more") === "true";
    var loadingOlder = false;
    var pill = document.getElementById("center-new-pill");
    var toBottomBtn = document.getElementById("center-scroll-bottom");
    // (SUA B3, 21/11) Vien + nut gio la con cua wrap (ngoai hop
    // cuon) nen khong con wrapper anchor trong hop — bong bóng
    // moi append thang vao hop.
    var typingLine = document.getElementById("center-typing-line");

    // (SUA B1 — phong thu them) Khi co bong bóng that duoc them
    // vao hop thi xoa moi o trong (.notice) dang co trong hop:
    // phong co tin thi khong con o trong nao.
    function removeEmptyNotice() {
      box.querySelectorAll(".notice").forEach(function (el) {
        el.remove();
      });
    }

    function distanceToBottom() {
      return box.scrollHeight - box.scrollTop - box.clientHeight;
    }
    function nearBottom() {
      return distanceToBottom() <= CHAT_NEAR_BOTTOM_PX;
    }

    function updateOldestLine() {
      if (hasMore) { return; }
      if (box.querySelector(".center-oldest-line")) { return; }
      var line = document.createElement("div");
      line.className = "center-oldest-line";
      line.textContent = "Đã tới tin cũ nhất còn lưu…";
      box.insertBefore(line, box.firstChild);
    }

    function trimBubbles() {
      // Gioi han ve ~300 bong bóng (Q-V060-06): vuot thi bo bot o
      // dau xa cho doc — dang o sat day thi bo tin cu nhat (tren
      // cung), dang doc tin cu thi bo tin moi nhat (duoi cung);
      // moc seq cao nhat giu nguyen (khong tai lai tin da thay).
      var bubbles = box.querySelectorAll(".msg");
      while (bubbles.length > CHAT_MAX_BUBBLES) {
        if (nearBottom()) {
          bubbles[0].remove();
          var first = box.querySelector(".msg");
          if (first) {
            var seq = parseInt(first.getAttribute("data-seq") || "", 10);
            if (!isNaN(seq)) { oldestSeq = seq; }
          }
        } else {
          bubbles[bubbles.length - 1].remove();
        }
        bubbles = box.querySelectorAll(".msg");
      }
    }

    function markRead(seq, onOk) {
      // Chi bam vien "N tin moi" (hoac ngoai le sat day Q-V060-07
      // ben duoi) moi goi ham nay — mo khung/polling khong tu danh
      // dau (luat moi v0.6.0). Phong luu tru khong danh dau.
      // (v0.6.0, NS-K21-3 — D17) Chi goi onOk (an vien) khi may chu
      // tra OK (HTTP 200 + {"ok": true}); goi that bai thi vien o
      // lai de bam lai — polling refreshUnread (<=5 giay sau) van
      // hien lai vien neu tin chua doc.
      if (archived || seq === null || seq === undefined) { return; }
      var body = "csrf_token=" + encodeURIComponent(chatCsrfToken()) +
        "&room=" + encodeURIComponent(room) +
        "&seq=" + encodeURIComponent(String(seq));
      fetch("/center/mark-read", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body,
      }).then(function (resp) {
        if (!resp.ok) { throw new Error("HTTP " + resp.status); }
        return resp.json();
      }).then(function (data) {
        if (data && data.ok === true && typeof onOk === "function") {
          onOk();
        } else if (typeof console !== "undefined") {
          // (SUA B4, 21/11) Ghi ly do de lan kiem live sau doc
          // duoc console neu vien van khong an (truoc day chi
          // lang lang giu vien).
          console.warn("[center] mark-read khong OK:", data);
        }
      }).catch(function (err) {
        // Doi chu ky sau — vien o lai; ghi log de chan doan.
        if (typeof console !== "undefined") {
          console.warn("[center] mark-read loi:", err);
        }
      });
    }

    function loadOlder() {
      // Dang tai them thi cuon tiep khong goi them request — toi da
      // 1 request before_seq tai mot thoi diem (NT-2.5).
      if (loadingOlder || !hasMore || oldestSeq === null) { return; }
      loadingOlder = true;
      var url = "/fragments/center-chat?room=" +
        encodeURIComponent(room) + "&before_seq=" + oldestSeq;
      fetch(url, { credentials: "same-origin", cache: "no-store" })
        .then(function (resp) {
          if (!resp.ok) { throw new Error("HTTP " + resp.status); }
          return resp.text();
        })
        .then(function (html) {
          var parsed = chatParseFragment(html);
          if (parsed.meta) {
            var nextOldest = parseInt(
              parsed.meta.getAttribute("data-oldest-seq") || "", 10);
            if (!isNaN(nextOldest)) { oldestSeq = nextOldest; }
            hasMore = parsed.meta.getAttribute("data-has-more") === "true";
            box.setAttribute("data-has-more", hasMore ? "true" : "false");
            box.setAttribute("data-oldest-seq", String(oldestSeq));
          }
          if (parsed.bubbles.length) {
            // Neo vi tri bang hieu scrollHeight: tin dang doc
            // khong nhay khi chen tin cu len dau hop.
            var prevHeight = box.scrollHeight;
            var frag = document.createDocumentFragment();
            parsed.bubbles.forEach(function (node) {
              frag.appendChild(node);
            });
            box.insertBefore(frag, box.firstChild);
            paintColorDots(box);
            box.scrollTop += box.scrollHeight - prevHeight;
            // (SUA B1) Co bong bóng that -> xoa o trong neu con.
            removeEmptyNotice();
            trimBubbles();
            applySenderFilter();
          }
          updateOldestLine();
          loadingOlder = false;
        })
        .catch(function () { loadingOlder = false; });
    }

    function updateTyping(typing, typingNames) {
      var roomsWithTyping = Object.keys(typing || {});
      // Dong "dang tra loi…" duoi ten phong dang mo (ten lay tu
      // endpoint — doc tu hub, khong tu suy ra tu phia Muse).
      if (typingLine) {
        var names = (typingNames && typingNames[room]) || [];
        typingLine.textContent = names.length ?
          names.join(", ") + " đang trả lời…" : "";
      }
      // Cham tren ten phong trong danh sach phong.
      document.querySelectorAll(".room-row").forEach(function (btn) {
        var form = btn.closest("form");
        var input = form ? form.querySelector('input[name="room"]') : null;
        var r = input ? input.value : "";
        if (!r) { return; }
        var dot = btn.querySelector("[data-room-typing]");
        if (roomsWithTyping.indexOf(r) >= 0) {
          if (!dot) {
            dot = document.createElement("span");
            dot.className = "room-typing-dot";
            dot.setAttribute("data-room-typing", r);
            dot.setAttribute("title", "Đang trả lời…");
            var badge = btn.querySelector("[data-room-badge]");
            btn.insertBefore(dot, badge || null);
          }
        } else if (dot) {
          dot.remove();
        }
      });
    }

    function refreshUnread() {
      fetch("/api/center/unread",
        { credentials: "same-origin", cache: "no-store" })
        .then(function (resp) {
          if (!resp.ok) { throw new Error("HTTP " + resp.status); }
          return resp.json();
        })
        .then(function (data) {
          if (!data || data.ok !== true) { return; }
          var rooms = data.rooms || {};
          var count = rooms[room] || 0;
          // Vien "N tin moi": so = so tin chua doc cua phong dang
          // mo (may chu dem theo moc da doc) — hien khi > 0.
          if (pill) {
            if (count > 0) {
              pill.textContent = count + " tin mới";
              pill.hidden = false;
            } else {
              pill.hidden = true;
            }
          }
          // Huy hieu tung phong trong danh sach phong (cap nhat
          // khong can tai lai trang).
          document.querySelectorAll("[data-room-badge]")
            .forEach(function (el) {
              var n = rooms[el.getAttribute("data-room-badge")] || 0;
              if (n > 0) {
                el.textContent = String(n);
                el.hidden = false;
              } else {
                el.textContent = "";
                el.hidden = true;
              }
            });
          var mentions = data.mention_rooms || [];
          document.querySelectorAll("[data-room-mention]")
            .forEach(function (el) {
              el.hidden = mentions.indexOf(
                el.getAttribute("data-room-mention")) < 0;
            });
          updateTyping(data.typing || {}, data.typing_names || {});
        })
        .catch(function () { /* doi chu ky sau */ });
    }

    function pollTick() {
      // Polling delta: chi lay tin moi hon after_seq, toi da 100
      // tin; khong thu hep danh sach ve lai 50 tin.
      if (newestSeq !== null) {
        var url = "/fragments/center-chat?room=" +
          encodeURIComponent(room) + "&after_seq=" + newestSeq;
        fetch(url, { credentials: "same-origin", cache: "no-store" })
          .then(function (resp) {
            if (!resp.ok) { throw new Error("HTTP " + resp.status); }
            return resp.text();
          })
          .then(function (html) {
            var parsed = chatParseFragment(html);
            if (!parsed.bubbles.length) { return; }
            var wasNear = nearBottom();
            var frag = document.createDocumentFragment();
            parsed.bubbles.forEach(function (node) {
              frag.appendChild(node);
            });
            // Tin moi chi xuat hien o cuoi hop: tin dang doc o dau
            // hop khong nhay (NT-2.6).
            // (SUA B3, 21/11) Vien + nut da ra khoi hop (con cua
            // wrap) nen append thang — khong con chen truoc
            // wrapper anchor.
            box.appendChild(frag);
            // (SUA B1) Co bong bóng that -> xoa o trong neu con.
            removeEmptyNotice();
            paintColorDots(box);
            if (parsed.meta) {
              var nextNewest = parseInt(
                parsed.meta.getAttribute("data-newest-seq") || "", 10);
              if (!isNaN(nextNewest)) { newestSeq = nextNewest; }
            }
            trimBubbles();
            applySenderFilter();
            if (wasNear) {
              box.scrollTop = box.scrollHeight;
              // NGOAI LE Q-V060-07 (QUYET-DINH-QUA-DEM-20-11):
              // dang o sat day (<=150px) ma tin moi ve thi TU
              // DANH DAU DA DOC — trai luat Q4 da chot ("chi bam
              // vien moi danh dau"), toi da 1 lan/chu ky polling
              // (moi chu ky chi co 1 lan goi after_seq nen chi co
              // toi da 1 lan danh dau). Chu nhan soat lai sang mai;
              // neu doi y thi xoa khoi nay (chi giu tu cuon, khong
              // tu danh dau).
              markRead(newestSeq);
            }
          })
          .catch(function () { /* doi chu ky sau */ });
      } else {
        // (D5/NS-02, nhom B 21/11/2026) Phong mo ra dang RONG thi
        // newestSeq = null nen nhanh delta o tren khong bao gio
        // chay — tin moi ve khong hien cho toi khi tai lai trang.
        // Fetch fragment THUONG (khong after_seq): tra N tin gan
        // nhat + meta co newestSeq moi. Hop dang rong nen chi chen
        // tin CHUA CO (chong trung lap theo data-seq — phong khi
        // fragment tra lai tin da hien); co bong bóng moi thi xoa
        // o trong (B1) + cap nhat newestSeq/oldestSeq. Cung 1 duong
        // code cho ca phong rieng (dm:) va phong chung (chung):
        // chi khac tham so room.
        var emptyUrl = "/fragments/center-chat?room=" +
          encodeURIComponent(room);
        fetch(emptyUrl, { credentials: "same-origin", cache: "no-store" })
          .then(function (resp) {
            if (!resp.ok) { throw new Error("HTTP " + resp.status); }
            return resp.text();
          })
          .then(function (html) {
            var parsed = chatParseFragment(html);
            if (!parsed.bubbles.length) { return; }
            // Chong trung lap: chi giu bong bóng co seq chua co
            // trong hop (fragment thuong tra lai ca danh sach).
            var seenSeqs = {};
            box.querySelectorAll(".msg[data-seq]").forEach(function (m) {
              seenSeqs[m.getAttribute("data-seq")] = true;
            });
            var fresh = parsed.bubbles.filter(function (node) {
              var s = node.getAttribute
                ? node.getAttribute("data-seq") : null;
              return s === null || !seenSeqs[s];
            });
            if (!fresh.length) { return; }
            var wasNear = nearBottom();
            var frag = document.createDocumentFragment();
            fresh.forEach(function (node) {
              frag.appendChild(node);
            });
            // (SUA B3, 21/11) Append thang nhu nhanh delta
            // (khong con wrapper anchor trong hop).
            box.appendChild(frag);
            // (SUA B1) Co bong bóng that -> xoa o trong neu con.
            removeEmptyNotice();
            paintColorDots(box);
            if (parsed.meta) {
              var nextNewest = parseInt(
                parsed.meta.getAttribute("data-newest-seq") || "", 10);
              if (!isNaN(nextNewest)) { newestSeq = nextNewest; }
              var nextOldest = parseInt(
                parsed.meta.getAttribute("data-oldest-seq") || "", 10);
              if (!isNaN(nextOldest)) { oldestSeq = nextOldest; }
              hasMore = parsed.meta.getAttribute("data-has-more")
                === "true";
            }
            trimBubbles();
            applySenderFilter();
            if (wasNear) {
              box.scrollTop = box.scrollHeight;
              markRead(newestSeq);
            }
          })
          .catch(function () { /* doi chu ky sau */ });
      }
      // Cung nhip 5 giay voi polling fragment, khong tao nhip rieng.
      refreshUnread();
      window.setTimeout(pollTick, CHAT_POLL_MS);
    }

    // (v0.6.0, T3l — doc 10 §3.8 §6b) O loc tin theo nguoi gui:
    // CHI an/hien bong bóng dang hien thi bang JS (theo
    // data-from-account cua moi bong bóng), KHONG tai lai, KHONG
    // them loc theo phong, KHONG goi fetch. Tin moi ve qua polling
    // hoac tai tin cu deu chay lai bo loc de khong lot tin sai
    // nguoi gui vao khung.
    var senderFilter = document.getElementById("center-sender-filter");
    function applySenderFilter() {
      if (!senderFilter) { return; }
      var wanted = senderFilter.value || "";
      box.querySelectorAll(".msg").forEach(function (bubble) {
        var from = bubble.getAttribute("data-from-account") || "";
        bubble.style.display =
          (wanted === "" || from === wanted) ? "" : "none";
      });
    }
    if (senderFilter) {
      senderFilter.addEventListener("change", applySenderFilter);
      applySenderFilter();
    }

    if (pill) {
      pill.addEventListener("click", function () {
        box.scrollTop = box.scrollHeight;
        // (v0.6.0, NS-K21-3 — D17) Vien chi an khi may chu tra OK:
        // goi markRead kem callback an vien; goi that bai thi vien
        // o lai de bam lai. Phong luu tru (Q-V060-12) chi cuon
        // xuong cuoi, KHONG danh dau da doc nen an vien ngay.
        if (archived || newestSeq === null) {
          pill.hidden = true;
          return;
        }
        markRead(newestSeq, function () { pill.hidden = true; });
      });
    }
    if (toBottomBtn) {
      // Nut "Ve cuoi" (Q-V060-08): chi cuon, khong danh dau.
      toBottomBtn.addEventListener("click", function () {
        box.scrollTop = box.scrollHeight;
      });
    }
    box.addEventListener("scroll", function () {
      if (box.scrollTop <= CHAT_LOAD_TOP_PX) { loadOlder(); }
    });
    // Mo tab lan dau: xuong tin moi nhat (khong danh dau da doc —
    // chi bam vien moi danh dau).
    box.scrollTop = box.scrollHeight;
    updateOldestLine();
    window.setTimeout(pollTick, CHAT_POLL_MS);
  }

  document.addEventListener("DOMContentLoaded", function () {
    // To mau cac cham tai khoan da render tinh tu server (khung
    // chat tinh trang chi tiet viec + dong mau chat trang Tai khoan).
    paintColorDots(document);
    // (P0 CSP — doi SỬA LỖI UI, 21/11/2026) Nut "Tai lai" canh chip
    // du lieu cu: bam thi tai lai trang de thu lay du lieu tuoi.
    var staleReload = document.getElementById("stale-reload");
    if (staleReload) {
      staleReload.addEventListener("click", function () {
        window.location.reload();
      });
    }
    var nodes = document.querySelectorAll("[data-fragment-url]");
    nodes.forEach(function (el) {
      var delay = parseInt(el.getAttribute("data-interval") || "5000", 10);
      el.setAttribute("data-last-ok", String(Date.now()));
      schedule(el, delay, 0);
    });
    // Tab Chat (v0.6.0, T3i): khoi dong polling delta + cuon.
    initCenterChat();
    // Form chat trung tâm (T3b): hiện tên file đã chọn lên nhãn
    // "Đính kèm file" (input file ẩn để vừa hàng nút 360px).
    var fileInput = document.getElementById("center-hoi-file");
    if (fileInput) {
      fileInput.addEventListener("change", function () {
        var label = document.querySelector(
          'label[for="center-hoi-file"]');
        if (!label) { return; }
        var name = (fileInput.files && fileInput.files[0])
          ? fileInput.files[0].name : "";
        label.textContent = name || "Đính kèm file";
      });
    }
    // Chuyen phong chat trung tam (v0.3.0, T3c — CHANG-5): truoc khi
    // submit form doi phong thi chep noi dung textarea hien tai vao
    // truong an "draft" cua form do + danh dau "draft_copied" = "1"
    // (server chi luu nhap khi co dau hieu nay — JS tat thi giu
    // nguyen ban nhap da luu, khong xoa). File da chon thi mat khi
    // doi phong (chi giu chu — P9).
    var chatBody = document.getElementById("center-hoi-body");
    if (chatBody) {
      document.querySelectorAll(".room-form").forEach(function (form) {
        form.addEventListener("submit", function () {
          var draft = form.querySelector('input[name="draft"]');
          var copied = form.querySelector('input[name="draft_copied"]');
          if (draft) { draft.value = chatBody.value; }
          if (copied) { copied.value = "1"; }
        });
      });
      // Enter gui tin (v0.6.0, T3i — van de 5): keydown trong
      // DOMContentLoaded, sau doan chep ban nhap (T3c). Enter gui,
      // Shift+Enter xuong dong, bo qua khi isComposing (dang go
      // tieng Viet bang bo go). Enter khi o nhap trong (khong co
      // file dinh kem) -> khong gui gi; chi co file (o nhap trong)
      // -> gui duoc. Khong them nut "Xuong dong" tren dien thoai
      // (Q-V060-13).
      chatBody.addEventListener("keydown", function (event) {
        if (event.isComposing) { return; }
        if (event.key !== "Enter" || event.shiftKey) { return; }
        event.preventDefault();
        var fileInput = document.getElementById("center-hoi-file");
        var hasFile = !!(fileInput && fileInput.files &&
          fileInput.files.length);
        if (!chatBody.value.trim() && !hasFile) { return; }
        var form = chatBody.closest("form");
        if (form) { form.requestSubmit(); }
      });
    }
  });
})();

// Trang hiện mã bí mật (v0.6.0, T3j — ngoại lệ F18(3)): đếm ngược 30
// giây (CENTER_VIEW_AUTO_HIDE_SECONDS) + xóa mã khỏi trang + nút
// "Sao chép" + nút "Đã chép xong, ẩn ngay" + xóa mã khi quay lại từ
// bfcache (pageshow persisted). JS chỉ là tiện ích — rào chắn thật
// là cờ "đã xem" phía máy chủ (DESIGN §3.11 bước 5).
// Hien ma khi cap (21/11): nut "Hien lai ma" + giu gia tri trong closure
(function () {
  "use strict";

  // Giữ giá trị mã trong closure của trang (không ghi vào bộ nhớ
  // trình duyệt, không gọi lại máy chủ). Ẩn chỉ thay text hiển thị; hiện lại khôi phục text
  // từ closure + đếm ngược 30 giây mới. Đóng/tải lại trang thì closure
  // mất; bfcache (pageshow persisted) hủy cả closure.
  var secretValues = null; // {fetch: "...", push: "..."} | null
  var countdownTimer = null; // id của setInterval đang chạy (G-01:
  // showSecrets luu id timer MOI vao cung bien nay; hideSecrets dung
  // dung timer hien tai — khong de 2 timer chay song song lam dem
  // nguoc nhay 2 giay/lan)

  function stopCountdown() {
    if (countdownTimer !== null) {
      window.clearInterval(countdownTimer);
      countdownTimer = null;
    }
  }

  function startCountdown(seconds) {
    var countdown = document.getElementById("view-secret-countdown");
    var remaining = seconds ||
      (countdown ? (parseInt(countdown.textContent, 10) || 30) : 30);
    if (countdown) { countdown.textContent = String(remaining); }
    stopCountdown();
    countdownTimer = window.setInterval(function () {
      remaining -= 1;
      if (remaining <= 0) {
        stopCountdown();
        hideSecrets();
        return;
      }
      if (countdown) { countdown.textContent = String(remaining); }
    }, 1000);
  }

  function showSecrets() {
    if (!secretValues) { return; }
    var fetchEl = document.getElementById("view-secret-fetch");
    var pushEl = document.getElementById("view-secret-push");
    if (fetchEl) { fetchEl.textContent = secretValues.fetch; }
    if (pushEl) { pushEl.textContent = secretValues.push; }
    document.querySelectorAll(".view-secret-copy").forEach(
      function (btn) { btn.disabled = false; });
    var showAgain = document.getElementById("view-secret-show-again");
    if (showAgain) { showAgain.hidden = true; }
    startCountdown(30);
  }

  function hideSecrets() {
    // Cau giu cho theo nut co render hay khong (duong du phong
    // §3.3b: nut vang -> cau cu "xoay ma moi de xem lai").
    var showAgain = document.getElementById("view-secret-show-again");
    var hiddenText = showAgain ?
      "Đã ẩn — bấm “Hiện lại mã” để xem lại." :
      "Đã ẩn — xoay mã mới để xem lại.";
    document.querySelectorAll(".view-secret-value").forEach(
      function (el) { el.textContent = hiddenText; });
    document.querySelectorAll(".view-secret-copy").forEach(
      function (btn) { btn.disabled = true; });
    var countdown = document.getElementById("view-secret-countdown");
    if (countdown) { countdown.textContent = "0"; }
    stopCountdown();
    if (showAgain) { showAgain.hidden = false; }
  }

  function clearViewSecrets() {
    secretValues = null;
    stopCountdown();
    document.querySelectorAll(".view-secret-value").forEach(
      function (el) {
        el.textContent = "Đã ẩn — xoay mã mới để xem lại.";
      });
    document.querySelectorAll(".view-secret-copy").forEach(
      function (btn) { btn.disabled = true; });
    var countdown = document.getElementById("view-secret-countdown");
    if (countdown) { countdown.textContent = "0"; }
    var showAgain = document.getElementById("view-secret-show-again");
    if (showAgain) { showAgain.hidden = true; showAgain.disabled = true; }
  }

  function copySecret(button) {
    var target = document.getElementById(
      button.getAttribute("data-target"));
    if (!target) { return; }
    var text = target.textContent;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text);
    } else {
      var area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      document.body.removeChild(area);
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!document.getElementById("view-secret-values")) { return; }
    var fetchEl = document.getElementById("view-secret-fetch");
    var pushEl = document.getElementById("view-secret-push");
    if (fetchEl && pushEl) {
      secretValues = {
        fetch: fetchEl.textContent,
        push: pushEl.textContent
      };
    }
    startCountdown();
    document.querySelectorAll(".view-secret-copy").forEach(
      function (btn) {
        btn.addEventListener("click", function () { copySecret(btn); });
      });
    var hideNow = document.getElementById("view-secret-hide-now");
    if (hideNow) {
      hideNow.addEventListener("click", function () { hideSecrets(); });
    }
    var showAgain = document.getElementById("view-secret-show-again");
    if (showAgain) {
      showAgain.addEventListener("click", function () { showSecrets(); });
    }
  });

  // bfcache: rời trang hiện mã rồi bấm Quay lại — trình duyệt khôi
  // phục trang từ bộ nhớ đệm (pageshow persisted=true) thì trang
  // phải không còn mã (PLAN T3j tiêu chí 12).
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) { clearViewSecrets(); }
  });
})();
"""
