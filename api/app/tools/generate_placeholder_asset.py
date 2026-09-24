from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct
import wave
import zlib


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def write_png(path: Path, color: tuple[int, int, int, int] = (58, 169, 188, 255)) -> None:
    width = 16
    height = 16
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            shade = 32 if (x + y) % 2 else 0
            row.extend((max(0, color[0] - shade), max(0, color[1] - shade), max(0, color[2] - shade), color[3]))
        rows.append(bytes(row))
    raw = b"".join(rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def write_wav(path: Path) -> None:
    sample_rate = 22050
    duration_s = 0.18
    frequency = 440.0
    frames = int(sample_rate * duration_s)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        data = bytearray()
        for i in range(frames):
            # A tiny square blip keeps the file valid and recognizably non-silent without external deps.
            phase = int((i * frequency / sample_rate) * 2) % 2
            value = 6000 if phase == 0 else -6000
            data.extend(struct.pack("<h", value))
        wav.writeframes(bytes(data))


def infer_kind(path: Path, explicit: str) -> str:
    if explicit:
        return explicit
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "png"
    if suffix == ".wav":
        return "wav"
    raise ValueError("kind is required for unsupported extension; supported placeholder outputs are .png and .wav")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate valid tiny placeholder image/audio assets for Codex Battlefield.")
    parser.add_argument("--output", required=True, help="Output file path, relative or absolute.")
    parser.add_argument("--kind", choices=["png", "wav"], default="", help="Asset kind. Defaults from .png/.wav extension.")
    args = parser.parse_args()
    output = Path(args.output)
    kind = infer_kind(output, args.kind)
    if kind == "png":
        write_png(output)
    elif kind == "wav":
        write_wav(output)
    else:
        raise ValueError(f"unsupported kind: {kind}")
    print(json.dumps({"ok": True, "output": str(output), "kind": kind, "bytes": output.stat().st_size}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
