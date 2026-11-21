# Cặp secret trung tâm — hướng dẫn tạo trên PC (chặng T1)

Module trung tâm v0.2.2. Mỗi tài khoản tham gia trung tâm có **1 cặp
secret riêng**: 1 secret đọc (fetch) + 1 secret ghi (push). Cặp này
KHÁC secret bot (queue/sync secret) — **cấm dùng lại secret bot**
(TS1). File này chỉ là hướng dẫn; trong file KHÔNG có secret thật
nào, và cũng không được ghi secret thật vào đây.

## Tạo cặp secret cho 1 tài khoản (làm trên PC, lúc áp gói T1)

Giả sử tài khoản là `muse-chinh` (tài khoản 1). Làm trong thư mục
gốc multibot trên PC (mặc định `~/multibot/`, hệ tập tin WSL —
KHÔNG đặt secret dưới `/mnt/c/` vì quyền 600/700 mất tác dụng ở đó).

```bash
cd ~/multibot
mkdir -p secrets
chmod 700 secrets
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/center-muse-chinh.fetch_secret
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/center-muse-chinh.push_secret
chmod 600 secrets/center-muse-chinh.fetch_secret secrets/center-muse-chinh.push_secret
```

Kiểm tra lại (chỉ kiểm tra quyền + file có nội dung, KHÔNG in
nội dung secret ra màn hình):

```bash
ls -l secrets/center-muse-chinh.*
test -s secrets/center-muse-chinh.fetch_secret && echo fetch-ok
test -s secrets/center-muse-chinh.push_secret && echo push-ok
```

## Bàn giao cho tài khoản (mở rộng K3, theo mẫu N15)

1. Agent PC đọc 2 file secret trên (quyền 600).
2. Giao cặp secret + `account_id` cho ĐÚNG tài khoản qua kênh an
   toàn (không dán chat, không qua UI — UI **cấm** hiện secret).
3. Tài khoản đó ghi `center-config/<account_id>.json` (600) phía
   Muse + kiểm chứng fetch 200 ngay.
4. Xóa mọi bản sao tạm sau khi bàn giao xong.

## Xoay secret (TS4)

Xoay theo kỷ luật 2 đầu, giống xoay queue secret: PC trước, Muse
sau, kiểm chứng fetch 200 ngay. **Cấm** gộp xoay secret với đổi
code/cấu hình trong 1 lần. Nếu còn 401 sau 5 phút thì khôi phục
secret cũ trên PC (giữ secret cũ trong ghi chú tạm CHỈ trong lúc
xoay, xóa ngay sau khi kiểm chứng xong).

Lộ secret thì xoay **credential đã lộ** (lộ center secret thì xoay
cặp center của tài khoản đó; lộ queue/sync secret hay token thì xoay
thứ đó — không mặc định xoay cả cặp center) + báo chủ nhân ngay.
Bản sao đã lưu trong kho append-only/backup/bản local không xóa
được secret đã lộ: ghi chú vào audit + theo dõi log truy cập dùng
secret cũ sau mốc xoay.

## Tài khoản 2 trở đi

Lặp lại đúng các bước trên với `account_id` của tài khoản đó
(ví dụ `center-<account_id>.fetch_secret`). Chỉ tài khoản có
`center_participating=true` trong `ui-config/accounts.json` VÀ đã
có đủ cặp secret mới được giao việc (hub từ chối nếu thiếu 1 trong 2).
