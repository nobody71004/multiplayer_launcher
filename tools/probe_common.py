"""tools/probe_common.py — shared telemetry helpers for the probe tools.

Currently exposes:

  * ``hexdump(data: bytes) -> str`` — canonical ``Offset | Hex | ASCII``
    formatter, 16 bytes per row, with literal pipe column separators
    so it can be piped straight into documentation schemas.

  * ``parse_hex_bytes(s: str) -> bytes`` — argparse type that parses
    ``"00"``, ``"0x00"``, ``"\\\\x00"`` styles plus arbitrary whitespace,
    underscore, and comma separators.

Designed to be importable without any third-party deps. Both
``probe_greeter.py`` (client-side TCP/UDP probe) and ``greet_server.py``
(asyncio TCP server) share this formatter so the wire bytes they emit
are byte-for-byte aligned when both ends of a captured trace are
pasted into a documentation schema side-by-side.

Import pattern::

    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from probe_common import hexdump, parse_hex_bytes
"""
from __future__ import annotations

import argparse


# Hexdump layout constants. Width is 16 because that's what ``hexdump -C``
# and standard hex viewers use — keeping the row width canonical means a
# reader can paste either tool's output next to ``hexdump -C`` lines
# without re-aligning columns by hand.
HEXDUMP_WIDTH = 16
HEXDUMP_HEX_PAD = HEXDUMP_WIDTH * 3  # 16*2 hex + 15 spaces + 1 mid-double = 48


def hexdump(data: bytes) -> str:
    """Canonical ``Offset | Hex | ASCII`` dump, 16 bytes per row.

    Each row is::

        OFFSET8 | HEX8_BY_SPACE HEX8_BY_SPACE | ASCII

    where the middle ``HEX8`` group has a double-space internal
    separator and the row is right-padded so the ASCII gutter aligns
    vertically across rows of differing lengths. Non-printable bytes
    render as ``.`` in the ASCII column.

    Empty input renders as ``"(empty: 0 bytes)"`` so callers don't
    have to special-case the no-bytes branch.
    """
    if not data:
        return "(empty: 0 bytes)"
    lines: list[str] = []
    for offset in range(0, len(data), HEXDUMP_WIDTH):
        chunk = data[offset : offset + HEXDUMP_WIDTH]
        left_n = min(8, len(chunk))
        left = " ".join(f"{b:02x}" for b in chunk[:left_n])
        if len(chunk) > 8:
            right = " ".join(f"{b:02x}" for b in chunk[8:])
            hex_field = f"{left}  {right}"
        else:
            hex_field = left
        hex_field = hex_field.ljust(HEXDUMP_HEX_PAD)
        ascii_field = "".join(
            chr(b) if 0x20 <= b < 0x7F else "." for b in chunk
        )
        lines.append(f"{offset:08x} | {hex_field} | {ascii_field}")
    return "\n".join(lines)


def parse_hex_bytes(s: str) -> bytes:
    """Argparse type: parse a hex string into bytes.

    Accepts ``"00"``, ``"0x00"``, ``"\\\\x00"`` styles plus arbitrary
    whitespace, underscore, and comma separators.
    """
    cleaned = (
        s.replace("0x", "")
        .replace("\\x", "")
        .replace(" ", "")
        .replace("_", "")
        .replace(",", "")
    )
    if not cleaned:
        raise argparse.ArgumentTypeError("hex string must contain at least one byte")
    if len(cleaned) % 2 != 0:
        raise argparse.ArgumentTypeError(
            f"hex bytes must have an even nibble count; got {len(cleaned)} in {s!r}"
        )
    try:
        return bytes.fromhex(cleaned)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid hex string {s!r}: {e}") from e
