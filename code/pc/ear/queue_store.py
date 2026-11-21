"""Hàng đợi append-only trên PC: 1 store/bot (``01`` §1, D2/D8/D9).

Một dòng = một JSON::

    {"seq": N, "event_id": "...",
     "received_at": "ISO-8601 kèm múi giờ", "record": {...bản ghi v1...}}

- Ghi bền: ghi dòng -> flush -> fsync TRƯỚC khi caller cập nhật seen
  (R4). Thứ tự này do caller (poller) giữ: ``append`` rồi mới ghi seen.
- ``seq`` tăng đơn điệu, không bao giờ dùng lại.
- Đọc chịu được "dòng dở cuối file": bỏ dòng lỗi, ghi log, tiếp tục.
- Cắt tỉa: giữ tối đa 5000 dòng VÀ không quá 30 ngày tuổi (SUY LUẬN —
  chốt ở GĐ2) VÀ chỉ cắt dòng có seq <= watermark "đã phục vụ" của
  gateway; chưa có watermark thì chỉ cắt theo tuổi. Cắt tỉa CÓ THỂ
  làm mất tin chưa fetch: mỗi lần cắt đều ghi log số dòng đã cắt +
  cảnh báo.
- Thread-safe: mọi thao tác dưới 1 lock duy nhất trong tiến trình.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

MAX_EVENTS = 5000
MAX_AGE_DAYS = 30  # SUY LUẬN — chốt ở GĐ2 (01 §1).
READ_LIMIT_MAX = 500


def _fsync_dir(dir_path):
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _default_clock():
    return datetime.now().astimezone()


def _parse_received_at(value):
    """Parse received_at; thiếu múi giờ thì hiểu là giờ địa phương."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def cursor_too_old(since, floor_seq, events):
    """Điều kiện 409 ``cursor_too_old`` — giữ nguyên cả 4 điều kiện (R2).

    Chỉ đúng khi: ``since < floor`` VÀ ``since != 0`` VÀ ``floor > 0``
    VÀ đợt trả về rỗng. Gateway dùng hàm này; fetcher đồng bộ lại với
    ``since = floor_seq - 1`` (D8).
    """
    return since < floor_seq and since != 0 and floor_seq > 0 and not events


def _extract_seq(mapping):
    if not isinstance(mapping, dict):
        return None
    # Ưu tiên served_seq: watermark "đã phục vụ" đơn điệu (max), không
    # bao giờ quay lui. last_fetched_seq chỉ là "lần fetch gần nhất":
    # 2 lần ghi chạy ngược thứ tự (gateway ghi sau khi trả response)
    # có thể làm nó quay lui về seq cũ — không dùng làm watermark.
    for key in ("served_seq", "last_fetched_seq", "seq", "watermark"):
        value = mapping.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def default_watermark_reader(path, bot_id=None):
    """Đọc watermark "đã phục vụ" từ file của gateway.

    File watermark do gateway ghi, định dạng chưa chốt cứng ở v0.2 nên
    chấp nhận vài dạng: ``{"last_fetched_seq": N}``, ``{"seq": N}``,
    hoặc lồng theo bot ``{"bots": {bot_id: {"served_seq": N, ...}}}``
    (dạng gateway thật ghi). Trong một mục, ưu tiên ``served_seq``
    (watermark đơn điệu) trước ``last_fetched_seq``.
    Không có file / không parse được / không có số -> None (chỉ cắt
    theo tuổi). Không bao giờ raise vì file watermark hỏng.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    found = _extract_seq(data)
    if found is not None:
        return found
    if bot_id is not None and isinstance(data, dict):
        found = _extract_seq(data.get(bot_id))
        if found is not None:
            return found
        bots = data.get("bots")
        if isinstance(bots, dict):
            found = _extract_seq(bots.get(bot_id))
            if found is not None:
                return found
    return None


class QueueStore:
    """Store hàng đợi của 1 bot, file ``queue.jsonl`` trong data dir."""

    def __init__(self, data_dir, max_events=MAX_EVENTS,
                 max_age_days=MAX_AGE_DAYS, watermark_path=None,
                 watermark_reader=None, bot_id=None, log=None, clock=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.data_dir / "queue.jsonl"
        self.max_events = max_events
        self.max_age_days = max_age_days
        self.watermark_path = (
            Path(watermark_path) if watermark_path is not None else None
        )
        self._watermark_reader = watermark_reader or default_watermark_reader
        self.bot_id = bot_id
        self._log = log or (lambda message: None)
        self._clock = clock or _default_clock
        self.lock = threading.Lock()
        self.latest_seq, self.floor_seq, self.total = self._scan()

    def _scan(self):
        """Đọc seq đầu/cuối + đếm dòng hợp lệ; bỏ qua dòng lỗi."""
        latest = 0
        floor = 0
        total = 0
        try:
            handle = open(self.log_path, encoding="utf-8")
        except OSError:
            return 0, 0, 0
        with handle:
            for line_no, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    seq = row["seq"]
                except (ValueError, KeyError, TypeError):
                    self._log(
                        "queue: bỏ dòng không parse được ở dòng %d "
                        "(có thể là dòng dở cuối file)" % line_no
                    )
                    continue
                if not isinstance(seq, int) or isinstance(seq, bool):
                    continue
                if total == 0:
                    floor = seq
                latest = max(latest, seq)
                floor = min(floor, seq)
                total += 1
        if total == 0:
            floor = 0
            latest = 0
        return latest, floor, total

    def next_seq(self):
        """Seq sẽ gán cho lần append tiếp theo (chỉ đọc, không ghi).

        Tai Telegram dùng để đặt tên file đính kèm trước khi ghi
        queue (tên file mang seq để prune theo watermark). Tai là
        writer duy nhất của queue nên giá trị peek này khớp với seq
        thật của lần append kế tiếp.
        """
        with self.lock:
            return self.latest_seq + 1

    def read_watermark(self):
        """Watermark "đã phục vụ" hiện tại, hoặc None nếu chưa có.

        Cùng nguồn với :meth:`prune` (file của gateway); tai Telegram
        dùng để prune file đính kèm theo cùng watermark.
        """
        return self._read_watermark()

    def append(self, record, event_id, received_at=None):
        """Ghi thêm 1 bản ghi; trả về seq mới.

        Ghi + flush + fsync trước khi trả về — caller PHẢI cập nhật
        seen SAU khi hàm này trả về (R4). Hàm này không tự chống lặp;
        chống lặp là việc của SeenStore ở caller.
        """
        if received_at is None:
            received_at = self._clock().isoformat()
        with self.lock:
            seq = self.latest_seq + 1
            row = {
                "seq": seq,
                "event_id": event_id,
                "received_at": received_at,
                "record": record,
            }
            line = json.dumps(row, ensure_ascii=False) + "\n"
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            self.latest_seq = seq
            if self.total == 0:
                self.floor_seq = seq
            self.total += 1
            return seq

    def read_since(self, since, limit=100):
        """Trả về (rows, latest_seq, floor_seq); rows có seq > since.

        Chịu dòng dở cuối file: bỏ dòng lỗi, ghi log, tiếp tục đọc.
        """
        limit = max(1, min(int(limit), READ_LIMIT_MAX))
        rows = []
        with self.lock:
            try:
                handle = open(self.log_path, encoding="utf-8")
            except OSError:
                return [], self.latest_seq, self.floor_seq
            with handle:
                for line_no, line in enumerate(handle, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        self._log(
                            "queue: bỏ dòng không parse được ở dòng %d "
                            "khi đọc" % line_no
                        )
                        continue
                    if not isinstance(row, dict):
                        continue
                    seq = row.get("seq", 0)
                    if isinstance(seq, int) and seq > since:
                        rows.append(row)
                        if len(rows) >= limit:
                            break
            return rows, self.latest_seq, self.floor_seq

    def _read_watermark(self):
        if self.watermark_path is None:
            return None
        try:
            return self._watermark_reader(self.watermark_path, self.bot_id)
        except TypeError:
            # Reader tùy biến chỉ nhận 1 tham số đường dẫn.
            try:
                return self._watermark_reader(self.watermark_path)
            except Exception:  # noqa: BLE001 - watermark hỏng thì bỏ qua
                return None
        except Exception:  # noqa: BLE001 - watermark hỏng thì bỏ qua
            return None

    def prune(self, now=None):
        """Cắt tỉa theo số lượng + tuổi + watermark (01 §1, D9).

        Trả về dict thống kê ``{"cut", "kept", "unparseable_kept",
        "watermark"}``. Chỉ cắt dòng có seq <= watermark; chưa có
        watermark thì chỉ cắt theo tuổi. Cắt tỉa có thể làm mất tin
        chưa fetch nên luôn ghi log số dòng đã cắt + cảnh báo.
        """
        if now is None:
            now = self._clock()
        cutoff = now - timedelta(days=self.max_age_days)
        with self.lock:
            try:
                handle = open(self.log_path, encoding="utf-8")
            except OSError:
                return {"cut": 0, "kept": 0, "unparseable_kept": 0,
                        "watermark": None}
            with handle:
                lines = handle.readlines()
            parsed = []
            unparseable = []
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                    seq = row["seq"]
                except (ValueError, KeyError, TypeError):
                    unparseable.append(line)
                    continue
                if not isinstance(seq, int) or isinstance(seq, bool):
                    unparseable.append(line)
                    continue
                parsed.append((line, row))
            if not parsed:
                return {"cut": 0, "kept": 0,
                        "unparseable_kept": len(unparseable),
                        "watermark": None}
            watermark = self._read_watermark()
            parsed.sort(key=lambda item: item[1]["seq"])
            over_cap = set()
            if len(parsed) > self.max_events:
                for line, _row in parsed[: len(parsed) - self.max_events]:
                    over_cap.add(id(line))
            cut_lines = []
            keep_lines = list(unparseable)
            kept_rows = []
            for line, row in parsed:
                seq = row["seq"]
                aged = False
                received = _parse_received_at(row.get("received_at"))
                if received is not None and received < cutoff:
                    aged = True
                if watermark is None:
                    should_cut = aged
                else:
                    should_cut = seq <= watermark and (
                        id(line) in over_cap or aged
                    )
                if should_cut:
                    cut_lines.append((line, row))
                else:
                    keep_lines.append(line)
                    kept_rows.append(row)
            if not cut_lines:
                return {"cut": 0, "kept": len(parsed),
                        "unparseable_kept": len(unparseable),
                        "watermark": watermark}
            tmp_path = self.log_path.with_suffix(".jsonl.tmp")
            with open(tmp_path, "w", encoding="utf-8") as handle:
                handle.writelines(keep_lines)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.log_path)
            _fsync_dir(self.data_dir)
            if kept_rows:
                seqs = [row["seq"] for row in kept_rows]
                self.floor_seq = min(seqs)
                self.latest_seq = max(seqs)
                self.total = len(kept_rows)
            else:
                self.floor_seq = 0
                self.total = 0
            cut_seqs = [row["seq"] for _line, row in cut_lines]
            self._log(
                "queue prune: đã cắt %d dòng (seq %d..%d); CẢNH BÁO: "
                "cắt tỉa có thể làm mất tin chưa fetch nếu fetcher "
                "chậm hơn giới hạn giữ" % (
                    len(cut_lines), min(cut_seqs), max(cut_seqs))
            )
            return {"cut": len(cut_lines), "kept": len(kept_rows),
                    "unparseable_kept": len(unparseable),
                    "watermark": watermark}
