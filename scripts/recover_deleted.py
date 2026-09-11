"""Carve deleted rows out of a sqlite database's freelist pages.

Freelist leaf pages keep their old b-tree header and cell pointer array
(only the first 4 bytes are overwritten with the next-page pointer), so
deleted records — including overflow chains — are usually fully intact.
Decodes records directly and classifies them by YAAH's schema shape:
  - 6 columns ending in two timestamp strings  -> conversations
  - 8 columns with role/conversation_id        -> messages

Writes recovered.db (proper tables) and prints a summary. Read-only on
the source file; never touch the live backend/data/agent.db.

Usage: python scripts/recover_deleted.py <db file> [out db]
"""
import hashlib
import sys
from pathlib import Path

import sqlite3

ROLES = {"user", "assistant", "tool", "system"}


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    for _ in range(8):
        b = buf[pos]
        pos += 1
        result = (result << 7) | (b & 0x7F)
        if not b & 0x80:
            return result, pos
    b = buf[pos]
    pos += 1
    return (result << 8) | b, pos


def serial_size(st: int) -> int:
    if st in (0, 8, 9):
        return 0
    if st == 1:
        return 1
    if st == 2:
        return 2
    if st == 3:
        return 3
    if st in (4, 5):
        return st - 1  # 4 -> 3? no: 4->4? sizes: 1,2,3,4,6,8
    if st == 4:
        return 4
    if st == 5:
        return 6
    if st == 6:
        return 8
    if st == 7:
        return 8
    if st >= 12:
        return (st - 12) // 2 if st % 2 == 0 else (st - 13) // 2
    raise ValueError(st)


def decode_value(st: int, raw: bytes):
    if st == 0:
        return None
    if 1 <= st <= 6:
        n = serial_size(st)
        return int.from_bytes(raw[:n], "big", signed=True)
    if st == 7:
        import struct

        return struct.unpack(">d", raw[:8])[0]
    if st == 8:
        return 0
    if st == 9:
        return 1
    if st >= 13 and st % 2:
        return raw.decode("utf-8", errors="replace")
    return raw.hex()


def parse_record(buf: bytes, pos: int):
    hdr_len, p = read_varint(buf, pos)
    types = []
    while p < pos + hdr_len:
        st, p = read_varint(buf, p)
        types.append(st)
    values = []
    body = pos + hdr_len
    for st in types:
        n = serial_size(st)
        values.append(decode_value(st, buf[body : body + n]))
        body += n
    return values, body


def local_payload(payload: int, usable: int) -> int:
    maxlocal = usable - 35
    if payload <= maxlocal:
        return payload
    minlocal = (usable - 12) * 32 // 255 - 23
    k = minlocal + (payload - minlocal) % (usable - 4)
    return k if k <= maxlocal else minlocal


class Carver:
    def __init__(self, data: bytes):
        self.data = data
        self.page_size = int.from_bytes(data[16:18], "big")
        if self.page_size == 1:
            self.page_size = 65536
        self.usable = self.page_size  # reserved space assumed 0
        self.overflow_seen: set[int] = set()

    def page(self, n: int) -> bytes:
        off = (n - 1) * self.page_size
        return self.data[off : off + self.page_size]

    def freelist_pages(self) -> list[int]:
        pages = []
        trunk = int.from_bytes(self.data[32:36], "big")
        seen = set()
        while trunk and trunk not in seen:
            seen.add(trunk)
            pg = self.page(trunk)
            n = int.from_bytes(pg[4:8], "big")
            for i in range(n):
                leaf = int.from_bytes(pg[8 + 4 * i : 12 + 4 * i], "big")
                pages.append(leaf)
            trunk = int.from_bytes(pg[0:4], "big")
        return pages

    def read_payload(self, page_no: int, pos: int, total: int) -> bytes:
        """Read a cell payload, following its overflow chain if any."""
        pg = self.page(page_no)
        local = local_payload(total, self.usable)
        out = bytearray(pg[pos : pos + local])
        if local < total:
            next_pg = int.from_bytes(pg[pos + local : pos + local + 4], "big")
            got = total - local
            while next_pg and next_pg not in self.overflow_seen:
                self.overflow_seen.add(next_pg)
                opg = self.page(next_pg)
                nxt = int.from_bytes(opg[0:4], "big")
                take = min(self.usable - 4, got)
                out += opg[4 : 4 + take]
                got -= take
                next_pg = nxt
        return bytes(out)

    def cells(self, page_no: int):
        pg = self.page(page_no)
        if pg[0] != 0x0D:
            return
        ncell = int.from_bytes(pg[3:5], "big")
        for i in range(ncell):
            ptr = int.from_bytes(pg[8 + 2 * i : 10 + 2 * i], "big")
            if ptr < 8 or ptr >= self.page_size:
                continue
            try:
                payload_len, p = read_varint(pg, ptr)
                rowid, p = read_varint(pg, p)
                payload = self.read_payload(page_no, p, payload_len)
                values, _ = parse_record(payload, 0)
                yield rowid, values
            except Exception:
                continue
        # Deleted cells chained off the page header as freeblocks: a freeblock
        # is [next:2][size:2] and the old cell content follows intact.
        fb = int.from_bytes(pg[1:3], "big")
        seen = set()
        while fb and fb not in seen and fb + 4 < self.page_size:
            seen.add(fb)
            try:
                payload_len, p = read_varint(pg, fb + 4)
                rowid, p = read_varint(pg, p)
                if 0 < payload_len < 5 * self.page_size:
                    payload = self.read_payload(page_no, p, payload_len)
                    values, _ = parse_record(payload, 0)
                    yield rowid, values
            except Exception:
                pass
            fb = int.from_bytes(pg[fb : fb + 2], "big")


def classify(values: list):
    # Older rows predate system_prompt_override (5 cols); current ones have 6.
    if len(values) in (5, 6) and all(isinstance(v, str) for v in values[-2:]):
        return "conversation"
    if len(values) == 8 and values[2] in ROLES:
        return "message"
    return None


def main() -> None:
    src = Path(sys.argv[1])
    out_path = Path(sys.argv[2] if len(sys.argv) > 2 else src.parent / "recovered.db")
    data = src.read_bytes()
    carver = Carver(data)
    convs: dict[int, tuple] = {}
    msgs: dict[int, tuple] = {}
    junk = 0
    # Freelist pages plus every active table-leaf page (for its freeblocks).
    leaf_pages = set(carver.freelist_pages())
    total_pages = len(data) // carver.page_size
    for n in range(2, total_pages + 1):
        if (n - 1) * carver.page_size < len(data) and carver.page(n)[0:1] == b"\x0d":
            leaf_pages.add(n)
    for page_no in sorted(leaf_pages):
        for rowid, values in carver.cells(page_no):
            kind = classify(values)
            # INTEGER PRIMARY KEY is stored as NULL inside the record; the
            # real id is the cell's rowid.
            if kind == "conversation":
                convs[rowid] = tuple([rowid] + list(values[1:]))
            elif kind == "message":
                msgs[rowid] = tuple([rowid] + list(values[1:]))
            else:
                junk += 1

    # Keep the most complete copy when a rowid appears on several pages.
    if out_path.exists():
        out_path.unlink()
    out = sqlite3.connect(out_path)
    out.execute(
        "CREATE TABLE conversations (id INTEGER PRIMARY KEY, title TEXT,"
        " workspace TEXT, system_prompt_override TEXT, created_at TEXT,"
        " updated_at TEXT)"
    )
    out.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER,"
        " role TEXT, content TEXT, tool_calls TEXT, tool_call_id TEXT,"
        " images TEXT, created_at TEXT)"
    )
    seen_hashes = set()
    n_msg = 0
    for rowid in sorted(msgs):
        row = msgs[rowid]
        h = hashlib.md5(str(row).encode()).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        out.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?)", row)
        n_msg += 1
    n_conv = 0
    for rowid in sorted(convs):
        row = convs[rowid]
        # Pad pre-system_prompt_override (5-col) rows to the current shape.
        row = row[:3] + (None,) * (6 - len(row)) + row[3:] if len(row) == 5 else row
        out.execute("INSERT OR REPLACE INTO conversations VALUES (?,?,?,?,?,?)", row)
        n_conv += 1
    out.commit()
    out.close()
    print(f"freelist pages scanned: {len(carver.freelist_pages())}")
    print(f"recovered conversations: {n_conv} -> {out_path}")
    print(f"recovered messages:      {n_msg}")
    print(f"unclassified records:    {junk}")


if __name__ == "__main__":
    main()
