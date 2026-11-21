#!/usr/bin/env python3
"""Parse ``multipart/form-data`` bang thu vien chuan (khong ``cgi``).

Dung cho 2 duong tai file len trung tam (v0.2.5, Q-C7):

- UI: form ``POST /center/messages`` (``pc/ui/ui_app.py``).
- Receiver: ``POST /v1/center/<account_id>/files``
  (``pc/sync_receiver/receiver_main.py``).

Chi parse, khong ghi file, khong gioi han kich thuoc — gioi han kich
thuoc (50 MB/file + mep multipart) do lop goi ap truoc khi parse
(UI: ``MAX_BODY_BYTES``; receiver: ``MAX_FILE_BODY_BYTES``).

Tra ve ``(fields, files)``: ``fields`` la dict key -> [gia tri]
(nhu ``parse_qs``); ``files`` la list dict ``{name, filename,
content_type, data}``. Body sai dinh dang / thieu boundary ->
``ValueError`` (lop goi tu choi 400, khong ghi gi).

Ghi chu: ten file chua dau ``"`` se bi cat o dau ``"`` dau tien
(regex don gian); ten nhu vay hiem va van qua kiem tra R6/kich
thuoc o lop store sau do — khong che ten am tham.
"""

import re

_DISPOSITION_RE = re.compile(
    r'form-data;\s*name="([^"]*)"(?:;\s*filename="([^"]*)")?')
_BOUNDARY_RE = re.compile(r"boundary=([^;]+)")


def _boundary_from(content_type):
    """Lay boundary tu Content-Type; thieu/rong -> None."""
    match = _BOUNDARY_RE.search(content_type or "")
    if not match:
        return None
    boundary = match.group(1).strip().strip('"')
    if not boundary:
        return None
    return boundary.encode("utf-8", "surrogateescape")


def parse_multipart(body, content_type):
    """Parse body multipart/form-data -> (fields, files).

    ``body`` la bytes. ``content_type`` la gia tri header
    Content-Type (ke ca tham so boundary).
    """
    boundary = _boundary_from(content_type)
    if boundary is None:
        raise ValueError("missing_boundary")
    if not isinstance(body, (bytes, bytearray)):
        raise ValueError("bad_body")
    delimiter = b"--" + boundary
    parts = bytes(body).split(delimiter)
    fields = {}
    files = []
    # parts[0] la preamble, parts[-1] la epilogue ("--...").
    for chunk in parts[1:-1]:
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        elif chunk.endswith(b"\n"):
            chunk = chunk[:-1]
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            head, sep, data = chunk.partition(b"\n\n")
            if not sep:
                raise ValueError("bad_part")
        headers = {}
        for line in head.split(b"\r\n"):
            if b":" not in line:
                continue
            key, value = line.split(b":", 1)
            headers[key.strip().lower().decode("ascii", "replace")] = \
                value.strip().decode("utf-8", "replace")
        match = _DISPOSITION_RE.search(
            headers.get("content-disposition", ""))
        if not match:
            raise ValueError("bad_part")
        name = match.group(1)
        filename = match.group(2)
        if filename is None:
            value = data.decode("utf-8", errors="replace")
            fields.setdefault(name, []).append(value)
        else:
            files.append({
                "name": name,
                "filename": filename,
                "content_type": headers.get(
                    "content-type", "application/octet-stream"),
                "data": data,
            })
    return fields, files
