# -*- coding: utf-8 -*-
"""最小可用的 LevelDB 只读解析器。

只实现读取 Chrome Local Storage 所需的最小子集：
  * SSTable（``*.ldb``）—— footer / index block / data block
  * Write-Ahead Log（``*.log``）—— 32KB block 切分的 record + WriteBatch

参考 leveldb 的 table/format.cc、db/log_reader.cc、db/write_batch.cc。

内部键（internal key）= user_key + (sequence << 8 | type) 8 字节小端后缀。
同一 user_key 可能有多条记录，只保留 sequence 最大的一条。
"""

import os
import struct

from . import snappy

__all__ = ["LevelDB", "LevelDBError"]

# SSTable footer 末尾的魔数
_TABLE_MAGIC = 0xDB4775248B80FB57
_BLOCK_TRAILER = 5          # 1 字节压缩标志 + 4 字节 crc32
_LOG_BLOCK_SIZE = 32768
_LOG_HEADER = 7             # crc32(4) + length(2) + type(1)

_KIND_DELETION = 0
_KIND_VALUE = 1


class LevelDBError(Exception):
    """LevelDB 文件格式错误。"""


def _uvarint(buf, pos):
    """读无符号 LEB128 varint（LevelDB 用 64 位）。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise LevelDBError("varint 越界")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise LevelDBError("varint 过长")


def _read_block(container, offset, size):
    """读取并解压一个 block，返回其内容 bytes。

    重要：BlockHandle 里的 size **不包含** trailer。trailer 是紧跟在 block
    数据之后的 5 个字节——1 字节压缩类型 + 4 字节 crc32。leveldb 的
    table::ReadBlock 读的就是 ``handle.size() + kBlockTrailerSize``。
    """
    if offset + size + _BLOCK_TRAILER > len(container):
        raise LevelDBError(f"block 范围非法 offset={offset} size={size}")
    payload = container[offset:offset + size]
    comp_type = container[offset + size]
    if comp_type == 0:
        return payload
    if comp_type == 1:
        return snappy.uncompress(payload)
    raise LevelDBError(f"未知压缩类型 {comp_type}")


def _parse_block_entries(block):
    """解析 data block / index block，返回 [(key, value), ...]。

    block 布局：若干 entry，随后是 restart 数组，最后 4 字节是 restart 个数。
    """
    if len(block) < 4:
        raise LevelDBError("block 过短")
    num_restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
    restart_bytes = 4 * num_restarts
    limit = len(block) - 4 - restart_bytes
    if limit < 0:
        raise LevelDBError("restart 数组越界")

    entries = []
    pos = 0
    prev_key = b""
    while pos < limit:
        shared, pos = _uvarint(block, pos)
        unshared, pos = _uvarint(block, pos)
        vlen, pos = _uvarint(block, pos)
        if pos + unshared + vlen > len(block):
            raise LevelDBError("entry 越界")
        key = prev_key[:shared] + block[pos:pos + unshared]
        pos += unshared
        value = block[pos:pos + vlen]
        pos += vlen
        prev_key = key
        entries.append((key, value))
    return entries


def _read_sstable(path):
    """读取一个 .ldb 文件，产出 (internal_key, value, seq) 三元组。"""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 48:
        return
    footer = data[-48:]
    magic = struct.unpack_from("<Q", footer, 40)[0]
    if magic != _TABLE_MAGIC:
        raise LevelDBError(f"{os.path.basename(path)} 魔数不匹配：{magic:#x}")

    pos = 0
    _meta_off, pos = _uvarint(footer, pos)
    _meta_size, pos = _uvarint(footer, pos)
    idx_off, pos = _uvarint(footer, pos)
    idx_size, pos = _uvarint(footer, pos)

    index_block = _read_block(data, idx_off, idx_size)
    for _sep_key, handle in _parse_block_entries(index_block):
        p = 0
        blk_off, p = _uvarint(handle, p)
        blk_size, p = _uvarint(handle, p)
        data_block = _read_block(data, blk_off, blk_size)
        for ikey, value in _parse_block_entries(data_block):
            if len(ikey) < 8:
                continue
            packed = struct.unpack_from("<Q", ikey, len(ikey) - 8)[0]
            seq = packed >> 8
            kind = packed & 0xFF
            if kind != _KIND_VALUE:
                continue
            yield ikey[:-8], value, seq


def _read_log(path):
    """读取一个 .log (WAL) 文件，产出 WriteBatch 里的记录。

    产出 (user_key, value_or_None, seq)；value 为 None 表示删除。
    """
    with open(path, "rb") as f:
        data = f.read()

    # 第一步：按 block 切分并重组 record
    records = []
    pos = 0
    pending = b""
    while pos < len(data):
        block_end = min(len(data), (pos // _LOG_BLOCK_SIZE + 1) * _LOG_BLOCK_SIZE)
        if pos + _LOG_HEADER > block_end:
            pos = block_end                      # 跳到下一 block
            continue
        length = struct.unpack_from("<H", data, pos + 4)[0]
        rtype = data[pos + 6]
        if length == 0 and rtype == 0:
            pos = block_end                      # zero padding，丢弃
            continue
        if pos + _LOG_HEADER + length > len(data):
            break
        body = data[pos + _LOG_HEADER:pos + _LOG_HEADER + length]
        pos += _LOG_HEADER + length
        if rtype == 1:                           # FULL
            records.append(body)
        elif rtype == 2:                         # FIRST
            pending = body
        elif rtype == 3:                         # MIDDLE
            pending += body
        elif rtype == 4:                         # LAST
            pending += body
            records.append(pending)
            pending = b""
        else:
            raise LevelDBError(f"未知 log record 类型 {rtype}")

    # 第二步：把每条 record 当成 WriteBatch 解析
    for rec in records:
        if len(rec) < 12:
            continue
        seq0 = struct.unpack_from("<Q", rec, 0)[0]
        count = struct.unpack_from("<I", rec, 8)[0]
        p = 12
        for i in range(count):
            if p >= len(rec):
                break
            tag = rec[p]
            p += 1
            klen, p = _uvarint(rec, p)
            key = rec[p:p + klen]
            p += klen
            if tag == _KIND_DELETION:
                yield key, None, seq0 + i
            else:
                vlen, p = _uvarint(rec, p)
                value = rec[p:p + vlen]
                p += vlen
                yield key, value, seq0 + i


class LevelDB:
    """把某个 LevelDB 目录里的所有数据合并成 {user_key: value} 视图。"""

    def __init__(self, directory):
        self.directory = directory
        self.errors = []
        self._map = {}
        self._load()

    def _put(self, key, value, seq):
        """按 sequence 保留最新版本。"""
        old = self._map.get(key)
        if old is not None and old[1] >= seq:
            return
        self._map[key] = (value, seq)

    def _load(self):
        if not os.path.isdir(self.directory):
            raise LevelDBError(f"目录不存在：{self.directory}")
        for fn in sorted(os.listdir(self.directory)):
            path = os.path.join(self.directory, fn)
            if not os.path.isfile(path):
                continue
            try:
                if fn.endswith(".ldb"):
                    for key, value, seq in _read_sstable(path):
                        self._put(key, value, seq)
                elif fn.endswith(".log"):
                    for key, value, seq in _read_log(path):
                        if value is None:
                            self._map.pop(key, None)
                        else:
                            self._put(key, value, seq)
            except (LevelDBError, snappy.SnappyError, OSError, struct.error) as exc:
                # 单个文件失败不阻断整体（Chrome 可能正在写）
                self.errors.append(f"{fn}: {type(exc).__name__}: {exc}")

    # ---- 对外接口 ----

    def keys(self):
        return list(self._map.keys())

    def get(self, key):
        item = self._map.get(key)
        return None if item is None else item[0]

    def prefix_items(self, prefix):
        """返回所有以 prefix 开头的 (key, value)，按 key 排序。"""
        return sorted(
            ((k, v[0]) for k, v in self._map.items() if k.startswith(prefix)),
            key=lambda kv: kv[0],
        )

    def __len__(self):
        return len(self._map)
