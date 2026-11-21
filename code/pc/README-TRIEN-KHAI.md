# Triển khai hệ đa-bot V2 trên PC — Hướng dẫn cho agent trên máy tính + chủ nhân

> **Gửi agent đang đọc file này:** bạn làm việc trên máy tính cá nhân của chủ
> nhân (Windows là hệ điều hành chính, Linux chạy song song — WSL). Nhiệm vụ
> của bạn: dựng **phần nghe tin** của hệ đa-bot lên Linux trên máy này. Phần
> suy nghĩ và trả lời vẫn ở phía Muse AI. Đọc hết file này trước khi làm, làm
> theo đúng thứ tự. Khi gặp chỗ cần quyết định của chủ nhân (token, mật khẩu,
> khởi động lại máy), **dừng lại và hỏi chủ nhân**, đừng tự đoán.

## 1. Bức tranh tổng thể

```text
Tin nhắn Zalo/Telegram ──> [TAI trên PC này] ──hàng đợi──> [Muse AI: bộ não]
                           hỏi liên tục, ghi tin       qua Tailscale, lấy
                           vào hàng đợi trên máy       tin, nghĩ, trả lời
                                                        thẳng lên nền tảng
```

- **PC này chỉ NGHE.** Mỗi bot có 1 tiến trình tai riêng (`tai@<bot_id>`), chỉ
  hỏi nền tảng "có tin mới không", ghi tin vào hàng đợi riêng của bot đó. PC
  không suy nghĩ, không trả lời, không có mã gửi tin.
- **Gateway** (1 tiến trình, cổng 8765) chỉ để phía Muse **đọc** hàng đợi, sức
  khỏe, cấu hình và file qua mạng riêng Tailscale. Gateway không đọc token,
  không có endpoint ghi.
- **Muse AI chỉ NGHĨ và TRẢ LỜI.** Cứ khoảng 10 giây, phía Muse hỏi PC qua
  Tailscale, lấy tin về, suy nghĩ, gửi trả lời thẳng lên nền tảng từ phía Muse.
- **Vì sao tách ra?** Máy ảo của Muse bị thay định kỳ, mỗi lần thay, chương
  trình đang chạy chết theo và tin Zalo gửi đúng lúc đó **mất hẳn** (Zalo không
  gửi bù — đã kiểm chứng thật). Đặt tai lên PC thì tin được giữ an toàn trong
  hàng đợi trên PC, Muse thay máy xong thì lấy tiếp — trả lời có thể chậm,
  nhưng **không mất tin**.

## 2. Điều kiện tiên quyết

| # | Điều kiện | Cách kiểm tra |
|---|---|---|
| 1 | Linux (WSL) chạy được, có Python **3.10+** | `python3 --version` |
| 2 | Tailscale **trong Linux** đã đăng nhập (phương án A — khuyên dùng) | `tailscale status` chạy được, `tailscale ip -4` in ra địa chỉ `100.x.x.x` |
| 3 | systemd trong WSL đã bật (để unit tự khởi động lại khi crash) | `systemctl --user --version` chạy được |
| 4 | Máy được cắm điện, đặt **không ngủ** | Hỏi chủ nhân / kiểm tra cài đặt nguồn |
| 5 | Token bot (do **chủ nhân** cung cấp, lấy từ console của nền tảng) | Hỏi chủ nhân, xem mục 4 |

**Nếu chưa có systemd trong WSL:** thêm vào `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

rồi từ PowerShell chạy `wsl --shutdown` và mở lại WSL.

**Nếu Tailscale chỉ có ở phía Windows:** cài thêm vào Linux theo hướng dẫn
chính thức của Tailscale cho distro đang dùng, cùng một tài khoản với phía
Windows, rồi chạy `sudo tailscale up`.

## 3. Quy tắc an toàn — BẮT BUỘC

1. **Gói này KHÔNG chứa secret.** Token lấy từ console của nền tảng; queue
   secret được sinh lúc cài đặt. Không có secret nào nằm sẵn trong code, tài
   liệu hay file cấu hình mẫu.
2. **Token bot:** chỉ lấy từ **chủ nhân** (chủ nhân tự lấy từ console của nền
   tảng). Ghi vào file `secrets/<bot_id>.token`, quyền `600`. **Không bao giờ**
   in token ra màn hình, log, hay gửi đi đâu. Không dán token vào chat nhóm.
3. **Queue secret:** mật khẩu riêng của từng bot giữa PC và Muse, `install.sh`
   tự tạo ngẫu nhiên, ghi file `600`, và chỉ **in ra terminal 1 lần** lúc cài.
   Chủ nhân copy và gửi **riêng** cho Muse qua kênh K3 (chat riêng với Muse).
   Không gửi secret này đi đâu khác, không đưa vào file nào rời khỏi máy.
   Secret của bot này **không** mở được hàng đợi của bot khác.
4. **Gateway chỉ nghe qua Tailscale/localhost.** Không mở cổng ra internet,
   không đặt port-forward trên router, không tắt kiểm tra Bearer hay allowlist
   IP trong code.
5. **Secret/token phải nằm trên filesystem native của WSL** (ví dụ dưới thư
   mục home của Linux). **Không** đặt dưới `/mnt/c/` — quyền 600/700 không có
   hiệu lực ở đó.
6. **Không chạy 2 tai cùng 1 bot/token cùng lúc.** Mỗi bot chỉ có đúng 1 tai.
   Daemon cũ trên máy ảo Muse hiện đang TẮT — bạn không cần động vào phía Muse,
   chỉ cần biết: **chỉ tai trên PC này được phép nghe.**
7. **Chưa có allowlist thì CẤM bật tai** (quyết D5). Tai chỉ được bật sau khi
   bot đã có danh sách cho phép (xem mục 5).
8. **Không tự ý cho bot hoạt động trở lại.** Gói này chỉ là *chuẩn bị*. Bot chỉ
   chạy lại khi **chủ nhân nói rõ "cho chạy"** với Muse, sau khi bàn giao xong.

## 4. Các bước cài đặt (theo đúng thứ tự)

```bash
# 0. Giải nén gói ra một thư mục cố định và vào thư mục pc/ của gói, ví dụ:
cd ~/multibot-package/pc
#    Gói mang đi phải có đủ 2 thư mục shared/ và pc/ ở gốc gói —
#    install.sh copy toàn bộ cây này vào ~/multibot/code/, unit systemd
#    đặt PYTHONPATH về đó để tai import được pc.* và shared.*.

# 1. Cài đặt cho bot đầu tiên (bot_id: chữ thường, số, gạch ngang, tối đa 32 ký tự)
bash install.sh bot-a

#    install.sh sẽ: kiểm tra Python/Tailscale/systemd, tạo cây thư mục
#    ~/multibot/, tạo file token RỖNG, sinh queue secret (in 1 lần),
#    đăng ký bot vào registry ở trạng thái TẮT, render unit systemd.
#    Script chạy lặp lại được: không ghi đè secret/token đã có.
```

### 4.1. Điền token

Hỏi chủ nhân lấy token từ console của nền tảng (Zalo Bot console, hoặc
BotFather với Telegram), rồi ghi vào file token bằng trình soạn thảo — **không
in token ra màn hình**:

```bash
# Mở file bằng trình soạn thảo, dán token vào (1 dòng duy nhất), lưu lại.
nano ~/multibot/secrets/bot-a.token

# Kiểm tra quyền (phải là 600) và file không rỗng — KHÔNG in nội dung:
ls -l ~/multibot/secrets/
wc -c ~/multibot/secrets/bot-a.token
```

### 4.2. Chuyển queue secret cho Muse qua kênh K3

Queue secret đã in 1 lần ở cuối bước cài đặt. Chủ nhân copy dòng secret đó và
gửi **riêng** cho Muse qua chat riêng (kênh K3), kèm bot_id và địa chỉ
Tailscale của máy này. Nếu đã lỡ đóng terminal, chủ nhân tự đọc lại bằng:

```bash
cat ~/multibot/secrets/bot-a.queue_secret
```

Lệnh trên chỉ chủ nhân chạy, chỉ xem để gửi riêng cho Muse — agent không tự
gửi secret đi đâu.

## 5. Tạo allowlist trước khi bật (bắt buộc)

Mỗi bot phải có danh sách cho phép (chat nào được nghe, ai là chủ nhân, vai trò
ra sao). File nguồn chân lý duy nhất nằm trên PC:

```text
~/multibot/config/<bot_id>.allowlist.json
```

Mẫu tối thiểu (thay các giá trị `...` bằng id thật do chủ nhân cung cấp — chưa
biết id thì hỏi chủ nhân, đừng đoán):

```json
{
  "bot_id": "bot-a",
  "bot_kind": "group",
  "owner_sender_ids": ["ID_NGUOI_GUI_CUA_CHU_NHAN"],
  "allowed_chats": ["ID_CHAT_DUOC_PHEP"],
  "sender_roles": {"ID_NGUOI_GUI_CUA_CHU_NHAN": "owner"},
  "default_sender_policy": "deny",
  "admin_commands_owner_only": true,
  "memory_tier_a_owner_only": true,
  "chat_control_commands_enabled": false,
  "version": 1
}
```

Cấu hình không-secret của bot (tên hiển thị, nền tảng, vai trò) nằm ở
`~/multibot/config/<bot_id>.json`. Khi có giao diện web (GĐ3), chủ nhân sẽ
tạo/sửa các file này trên giao diện; ở GĐ0/GĐ1, agent tạo file theo mẫu trên
sau khi chủ nhân duyệt nội dung.

**Chưa có file allowlist thì cấm bật tai.** Phía Muse cũng kiểm tra lại: không
có allowlist thì không đánh thức worker cho bot đó.

## 6. Bật unit và kiểm tra

```bash
# 1. Khởi động gateway (1 tiến trình cho mọi bot):
systemctl --user start multibot-gateway
systemctl --user status multibot-gateway

# 2. Sau khi đã có allowlist, bật tai của bot (thay bot-a bằng bot_id thật):
systemctl --user enable --now tai@bot-a
systemctl --user status tai@bot-a
```

### 6.1. Kiểm tra sức khỏe qua gateway

Từ chính máy này, dùng queue secret của **đúng bot** (đặt vào biến shell, không
gõ secret trực tiếp vào lịch sử lệnh dài hạn nếu có thể tránh):

```bash
SECRET="$(cat ~/multibot/secrets/bot-a.queue_secret)"

# Sức khỏe của bot:
curl -s -H "Authorization: Bearer $SECRET" \
  http://127.0.0.1:8765/v1/bots/bot-a/health
# Phải thấy JSON có "bot_id": "bot-a", "stale": false khi tai đang nghe tốt.

# Hàng đợi của bot (tối đa 100 tin/lần):
curl -s -H "Authorization: Bearer $SECRET" \
  "http://127.0.0.1:8765/v1/bots/bot-a/queue?since=0&limit=100"

# Cấu hình không-secret của bot:
curl -s -H "Authorization: Bearer $SECRET" \
  http://127.0.0.1:8765/v1/bots/bot-a/config

unset SECRET
```

Ví dụ lệnh với secret **giả** (chỉ để minh họa cú pháp — secret này không có
thật, đừng dùng để kiểm tra thật):

```bash
curl -s -H "Authorization: Bearer SECRET_GIA_LAP_CHI_DE_VI_DU" \
  http://127.0.0.1:8765/v1/bots/bot-a/health
# Kết quả đúng với secret giả: {"error": "unauthorized"} (mã 401)
```

### 6.2. Kiểm tra log

```bash
# Log gateway (chỉ metadata, không có nội dung tin hay secret):
journalctl --user -u multibot-gateway -n 50
journalctl --user -u multibot-gateway -f

# Log tai của 1 bot:
journalctl --user -u tai@bot-a -n 50
```

## 7. Thêm bot thứ hai (GĐ1)

Chạy lại `install.sh` với bot_id mới — script chạy lặp lại được, không động
vào bot đã có:

```bash
bash install.sh bot-b
# ... điền token riêng, tạo allowlist riêng, bàn giao queue secret riêng ...
systemctl --user enable --now tai@bot-b
```

Mỗi bot có token, queue secret, hàng đợi, trạng thái và unit riêng. Gateway
thì chỉ có 1, phục vụ mọi bot, mỗi bot 1 secret riêng.

## 8. Vận hành hằng ngày

| Việc | Lệnh (trong WSL) |
|---|---|
| Xem trạng thái gateway | `systemctl --user status multibot-gateway` |
| Xem trạng thái receiver đồng bộ trả lời | `systemctl --user status multibot-sync-receiver` |
| Xem trạng thái Web UI | `systemctl --user status bot-ui` |
| Xem trạng thái tai 1 bot | `systemctl --user status tai@<bot_id>` |
| Xem log mới nhất | `journalctl --user -u multibot-gateway -n 50` |
| Theo dõi log trực tiếp | `journalctl --user -u multibot-gateway -f` |
| Khởi động lại gateway | `systemctl --user restart multibot-gateway` |
| Khởi động lại receiver | `systemctl --user restart multibot-sync-receiver` |
| Khởi động lại Web UI | `systemctl --user restart bot-ui` |
| Khởi động lại tai 1 bot | `systemctl --user restart tai@<bot_id>` |
| Dừng hẳn 1 bot | `systemctl --user stop tai@<bot_id>` |
| Kiểm tra sức khỏe | `curl` kèm header như mục 6.1 |
| Kiểm tra linger còn bật | `loginctl show-user "$USER" --property=Linger` |

## 9. Xử lý lỗi thường gặp

| Triệu chứng | Nguyên nhân thường gặp | Cách xử lý |
|---|---|---|
| `401 {"error": "unauthorized"}` | Sai/thiếu secret, hoặc dùng secret của bot khác | Kiểm tra đang dùng file secret của **đúng bot**; đọc lại bằng `cat` và thử lại. Secret bot A không mở được bot B — đó là hành vi đúng. |
| `403 {"error": "forbidden"}` | IP nguồn ngoài allowlist | Chỉ gọi từ localhost hoặc qua Tailscale. Kiểm tra Tailscale đã lên (`tailscale status`). Không mở rộng allowlist nếu chưa có lý do rõ ràng và được duyệt. |
| `404 {"error": "not_found"}` | Bot chưa đăng ký, sai bot_id, hoặc sai đường dẫn | Kiểm tra bot_id đúng mẫu và đã chạy `install.sh`; kiểm tra đường dẫn có dạng `/v1/bots/<bot_id>/health`. Endpoint cũ `/health` không còn tồn tại. |
| `409 {"error": "cursor_too_old"}` | Con trỏ của phía Muse cũ hơn phần hàng đợi còn giữ | Phía Muse tự đồng bộ lại. Nếu lặp lại nhiều lần, báo cho Muse kèm mã lỗi, đừng xóa file hàng đợi. |
| Gateway không khởi động / cổng bận | Đã có 1 gateway khác đang chạy, hoặc cổng 8765 bị chiếm | `systemctl --user status multibot-gateway`; kiểm tra tiến trình chiếm cổng: `ss -ltnp \| grep 8765`. Chỉ chạy 1 gateway. |
| `loginctl enable-linger` thất bại | Cần quyền quản trị lần đầu | Chạy tay: `sudo loginctl enable-linger "$USER"`. Nếu không bật linger, unit user có thể dừng khi đăng xuất. |
| Tailscale chưa chạy | Chưa đăng nhập / chưa lên mạng | `sudo tailscale up`, mở link xác thực trên trình duyệt. Gateway vẫn chạy local, nhưng phía Muse chưa nối được. |
| Health báo `"stale": true` | Tai chưa ghi trạng thái mới quá 5 phút | Kiểm tra `systemctl --user status tai@<bot_id>` và log của tai. Nếu tai đang ở trạng thái chờ token (token hỏng), đừng restart lặp — kiểm tra token với chủ nhân. |
| Tai không khởi động, báo thiếu allowlist | Chưa có file allowlist | Tạo file allowlist theo mục 5 rồi khởi động lại. Đây là khóa an toàn, không được bỏ qua. |

**Nguyên tắc khi gặp sự cố:** tai chết thì khởi động lại là nghe tiếp; tin đã
vào `data/<bot_id>/queue.jsonl` thì không mất; **đừng xóa file trong `data/`**
khi chưa hiểu rõ — hỏi Muse trước. Log của gateway/tai không bao giờ chứa nội
dung tin hay secret — nếu thấy secret trong log thì đó là sự cố, báo ngay.

## 10. Dừng / gỡ an toàn

```bash
# Dừng 1 bot (tai vẫn giữ tin đã nghe trong hàng đợi):
systemctl --user stop tai@<bot_id>

# Dừng gateway (tai vẫn ghi đĩa; phía Muse sẽ thử lại sau):
systemctl --user stop multibot-gateway

# Tắt hẳn tự khởi động cùng máy của 1 bot:
systemctl --user disable tai@<bot_id>
```

Khi bot bị lộ secret/token: coi như bị lộ toàn bộ — xoay ngay cả token (lấy
mới từ console) và queue secret (tạo mới + cập nhật 2 đầu PC/Muse), kiểm tra
log gateway xem có truy cập lạ không, và báo chủ nhân.

## 11. Cây thư mục sau khi cài

```text
~/multibot/
  code/                  # TOÀN BỘ cây code (quyết đóng gói vòng 3): shared/ + pc/
                         # — tai import pc.config và shared.*, thiếu 1 trong 2
                         # là không chạy được. Unit systemd đặt
                         # PYTHONPATH=<thư mục cài>/code.
    shared/              # mã dùng chung (bản ghi, định danh, transport)
    pc/                  # config.py, ear/, gateway/, sync_receiver/, ui/,
                         # install.sh, systemd/
  config/<bot_id>.json            # cấu hình không-secret
  config/<bot_id>.allowlist.json  # allowlist: nguồn chân lý duy nhất
  registry.json          # liệt kê bot, enabled, paused, không secret
  secrets/               # 700, NGOÀI thư mục code
    <bot_id>.token  <bot_id>.queue_secret  <bot_id>.sync_secret  # 600, 1 dòng
  data/<bot_id>/         # 700: queue.jsonl, seen/offset, state.json,
                         # replies.jsonl, files/ (chỉ Telegram)
  locks/                 # 700: khóa singleton + file chủ sở hữu của tai
  gateway-fetch-state.json  # gateway tự ghi: lần fetch gần nhất từng bot
  ui-config/             # (GĐ3) hash mật khẩu UI, phiên đăng nhập, audit,
                         # platform_endpoints.json (địa chỉ API cho UI
                         # kiểm tra token — do agent PC điền, không có
                         # sẵn trong gói)
  backups/               # backup hằng ngày, giữ 7 ngày — KHÔNG gồm secrets/
  systemd/               # bản recovery của template unit
  ops/OPS-FILE.md        # file vận hành: mạng WSL, Tailscale, linger
```

## 12. Máy tính phải "thức"

Tai chỉ nghe được khi Windows + WSL đang thức. Nhờ chủ nhân (hoặc làm giúp nếu
được phép):

- Cắm sạc liên tục; đặt Sleep = Never khi cắm điện.
- Đặt "khi gập màn hình" = Do nothing (nếu chủ nhân đồng ý), hoặc dặn chủ nhân
  đừng gập máy khi muốn bot nghe.
- Tắt/mềm lịch Windows Update tự khởi động lại ngoài giờ chủ nhân dùng máy;
  sau mỗi lần khởi động lại Windows, kiểm tra tai + gateway đã chạy lại chưa
  (mục 6).
- Mất điện/mất mạng = tai điếc trong lúc đó; có điện/mạng lại thì tai tự nghe
  tiếp, tin Zalo trong lúc mất vẫn mất (giới hạn của nền tảng). Đây là điểm yếu
  cố hữu của việc đặt tai lên máy cá nhân so với thuê máy chủ — chủ nhân đã
  biết và chấp nhận để thử nghiệm.

## 13. Web UI + đồng bộ trả lời (GĐ3)

Web UI là trang quản lý bot trên chính PC này: thẻ từng bot ở trên
(trạng thái, tin gần nhất, nút Bật/Tắt/Tạm dừng/Khởi động lại),
hoạt động mới ở dưới (cả tin đến lẫn trả lời của bot). UI chỉ chạy
trên loopback (cổng 8787) — mở từ xa qua Tailscale Serve HTTPS,
không mở cổng ra internet.

### 13.1. Đặt mật khẩu UI lần đầu (bắt buộc)

UI không có mật khẩu mặc định và từ chối chạy khi chưa có hash.
Chạy trong WSL (nhập 2 lần, không hiện chữ):

```bash
python3 ~/multibot/code/pc/ui/set_ui_password.py --root ~/multibot
```

Mật khẩu tối thiểu 12 ký tự. Đổi mật khẩu sẽ đăng xuất mọi phiên
đang mở. Phiên đăng nhập hết hạn sau 10 giờ. Xác thực 2 lớp (TOTP)
là việc hẹn bổ sung sau GĐ4 — ngày hẹn và điều kiện kích hoạt ghi
trong `ops/OPS-FILE.md`; giai đoạn đầu chưa có 2 lớp (chủ nhân đã
chấp nhận, quyết định D7).

Sau khi đặt mật khẩu, bật UI:

```bash
systemctl --user enable --now bot-ui
```

### 13.2. Mở UI từ điện thoại (Tailscale Serve)

UI chạy ở `http://127.0.0.1:8787` trong WSL. Để mở từ điện thoại,
dùng Tailscale Serve HTTPS trỏ tới cổng 8787 (lệnh chạy phía
Tailscale trên máy, theo hướng dẫn của Tailscale cho `tailscale
serve`). Kiểm tra trên điện thoại thật: đăng nhập được, bấm nút
điều khiển được (phiên + CSRF phải giữ được qua Serve — nếu bị văng
liên tục thì báo cho Muse, đó là điểm kiểm chứng UC-39).

Không dùng Tailscale Funnel, không port-forward, không bind
`0.0.0.0` cho UI.

### 13.3. Địa chỉ API để UI kiểm tra token

Khi thêm bot mới hoặc thay token đã lưu trên UI, UI kiểm tra token
bằng `getMe` TRƯỚC khi lưu. Địa chỉ API của từng nền tảng KHÔNG có
sẵn trong gói (gói cấm chứa host thật) — agent PC điền vào file
`ui-config/platform_endpoints.json` (install.sh đã tạo mẫu rỗng):

```json
{
  "zalo": "<scheme + host API của Zalo Bot Platform>",
  "telegram": "<scheme + host API của Telegram>"
}
```

Thiếu file này hoặc để trống thì UI báo "chưa cấu hình địa chỉ API"
và không kiểm tra được token — thêm/thay bot bằng cách điền token
trực tiếp vào file trên máy (mục 4.1) vẫn làm được như trước.

### 13.4. Receiver đồng bộ trả lời (cổng 8766)

Phía Muse đẩy trả lời đã gửi về receiver để UI hiện được cả 2 chiều
(hỏi gì, trả lời gì). Receiver chạy riêng với gateway, chỉ nhận
`POST /v1/bots/<bot_id>/replies` kèm sync secret riêng từng bot:

```bash
systemctl --user start multibot-sync-receiver
```

Sync secret được install.sh sinh cùng đợt với queue secret và bàn
giao cùng kênh K3 (mục 4.2). Secret của bot A không ghi được trả
lời của bot B — đó là hành vi đúng.

**Kiểm chứng phê duyệt đường hầm:** trước khi trông chờ đồng bộ
chạy, kiểm tra xem đường hầm Tailscale tới PC có cho qua cổng 8766
không (chưa rõ phê duyệt tính theo host hay theo host:cổng — nếu
chỉ phê duyệt cổng 8765 thì cần phê duyệt thêm 8766). Cách kiểm tra:
phía Muse gửi 1 đợt đồng bộ thử; nếu receiver nhận được (log
receiver có dòng nhận) thì đường hầm đã thông. Nếu không thông thì
UI vẫn hiện được tin đến ở chế độ rút gọn (GĐ3a) — không mất tin,
chỉ chưa thấy trả lời trên UI.

### 13.5. Khi UI không mở được (1 trang xử lý)

1. PC có đang thức không? (mục 12) — PC ngủ thì UI chết theo, đó là
   hành vi đã chấp nhận (D7). Mở máy lại, đợi ~1 phút rồi mở lại UI.
2. Unit UI có chạy không? `systemctl --user status bot-ui`
   - `inactive (dead)` + log báo "chưa có mật khẩu": chạy lại bước
     13.1 rồi `systemctl --user start bot-ui`.
   - `failed`: xem log `journalctl --user -u bot-ui -n 50` (chỉ
     metadata), gửi log cho Muse — đừng xóa file trong `ui-config/`.
3. Mở được trang đăng nhập nhưng đăng nhập hoài không vào?
   - Sai mật khẩu 5 lần trong 15 phút thì bị khóa thử tạm thời — đợi
     15 phút rồi thử lại.
   - Nếu quên mật khẩu: chạy lại `set_ui_password.py` để đặt mật khẩu
     mới (mọi phiên cũ bị thu hồi).
4. Mở từ điện thoại không được nhưng mở trên PC được? Kiểm tra
   Tailscale Serve còn chạy và điện thoại có trong cùng mạng
   Tailscale không (`tailscale status` trên PC phải thấy điện thoại).
5. UI mở được nhưng dữ liệu cũ (có cảnh báo "dữ liệu có thể đã cũ")?
   PC có thể vừa thức lại hoặc mất mạng chốc lát — đợi 1 phút, tải
   lại trang. Nếu vẫn cũ, kiểm tra tai + gateway (mục 6, 9).
6. UI hiện "chỉ tin đến, chưa thấy trả lời"? Đó là chế độ rút gọn
   GĐ3a: đồng bộ trả lời chưa tới (receiver chưa chạy, đường hầm
   chưa thông cổng 8766, hoặc PC vừa thức lại). Tin không mất —
   trả lời vẫn xem được trong chính cuộc trò chuyện. Kiểm tra
   receiver (mục 13.4) rồi báo Muse nếu vẫn không đồng bộ.
