# -*- coding: utf-8 -*-
"""纯 Python 实现的 Snappy raw 格式解压。

LevelDB 的 SSTable data block 默认使用 Snappy 压缩，Python 标准库没有
Snappy 实现，而 Windows 上编译 python-snappy 需要 MSVC 工具链。
这里按 https://github.com/google/snappy/blob/main/format_description.txt
的 raw format 逐指令实现，零依赖。

只实现解压（我们只读不写）。
"""

__all__ = ["uncompress", "SnappyError"]


class SnappyError(ValueError):
    """Snappy 数据格式错误。"""


def _read_varint(data, pos):
    """读取 snappy 风格的无符号 LEB128 varint（最多 5 字节 32 位）。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise SnappyError("varint 越界")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 28:
            raise SnappyError("varint 过长")


def uncompress(data):
    """解压 Snappy raw 格式数据，返回 bytes。

    raw 格式 = [解压后长度 varint] + [一系列 tag/指令]
    """
    if not data:
        raise SnappyError("空输入")

    expected_len, pos = _read_varint(data, 0)
    out = bytearray()

    n = len(data)
    while pos < n:
        tag = data[pos]
        pos += 1
        kind = tag & 0x03

        if kind == 0:
            # ---- Literal：直接拷贝 ----
            length = tag >> 2
            if length < 60:
                length += 1
            else:
                extra = length - 59          # 60..63 -> 1..4 字节长度
                if pos + extra > n:
                    raise SnappyError("literal 长度字段越界")
                length = int.from_bytes(data[pos:pos + extra], "little") + 1
                pos += extra
            if pos + length > n:
                raise SnappyError("literal 数据越界")
            out += data[pos:pos + length]
            pos += length
        else:
            # ---- Copy：从已输出区域回拷（允许重叠，必须逐字节） ----
            if kind == 1:
                length = ((tag >> 2) & 0x07) + 4
                if pos >= n:
                    raise SnappyError("copy1 越界")
                offset = ((tag >> 5) << 8) | data[pos]
                pos += 1
            elif kind == 2:
                length = (tag >> 2) + 1
                if pos + 2 > n:
                    raise SnappyError("copy2 越界")
                offset = int.from_bytes(data[pos:pos + 2], "little")
                pos += 2
            else:
                length = (tag >> 2) + 1
                if pos + 4 > n:
                    raise SnappyError("copy4 越界")
                offset = int.from_bytes(data[pos:pos + 4], "little")
                pos += 4

            if offset == 0 or offset > len(out):
                raise SnappyError(f"非法的 copy offset={offset} (已输出 {len(out)})")
            start = len(out) - offset
            # 重叠拷贝：out[start+i] 可能就是本指令刚写入的字节
            for i in range(length):
                out.append(out[start + i])

    if expected_len != len(out):
        raise SnappyError(f"长度不符：头部声明 {expected_len}，实际解出 {len(out)}")
    return bytes(out)
