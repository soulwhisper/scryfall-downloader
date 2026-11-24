"""Inspect native Forge image files without decoding or resizing pixels."""

from dataclasses import dataclass
import struct
import zlib


IMAGE_SUFFIXES = (".jpg", ".png")  # Forge probes JPEG before PNG.
QUALITY = {"small": (146, 204), "normal": (488, 680), "large": (672, 936), "png": (744, 1040)}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"
JPEG_SOF = frozenset(0xC0 + n for n in (0, 1, 2, 3, 5, 6, 7, 9, 10, 11, 13, 14, 15))


class InvalidImage(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ImageSize:
    short: int
    long: int
    suffix: str

    def meets(self, quality: str) -> bool:
        short, long = QUALITY[quality]
        return self.short >= short and self.long >= long and (quality != "png" or self.suffix == ".png")

    def improves(self, other: "ImageSize") -> bool:
        if self.short < other.short or self.long < other.long:
            return False
        return ((self.short, self.long) != (other.short, other.long)
                or (self.suffix == ".png" and other.suffix != ".png"))


def _png_size(file, size: int, check_chunks: bool) -> tuple[int, int]:
    if file.read(16) != PNG_SIGNATURE + b"\x00\x00\x00\rIHDR":
        raise InvalidImage("Missing PNG header")
    header = file.read(13)
    if len(header) != 13:
        raise InvalidImage("Incomplete PNG header")
    width, height = struct.unpack_from(">II", header)
    if not check_chunks:
        return width, height

    file.seek(8)
    saw_data = False
    while file.tell() <= size - 12:
        chunk = file.read(8)
        if len(chunk) != 8:
            break
        length, kind = struct.unpack(">I4s", chunk)
        if length > size - file.tell() - 4:
            break
        crc = zlib.crc32(kind)
        remaining = length
        while remaining:
            block = file.read(min(remaining, 128 * 1024))
            if not block:
                raise InvalidImage("Incomplete PNG chunk")
            crc = zlib.crc32(block, crc)
            remaining -= len(block)
        if file.read(4) != struct.pack(">I", crc):
            raise InvalidImage("PNG chunk checksum mismatch")
        saw_data |= kind == b"IDAT" and length > 0
        if kind == b"IEND":
            if saw_data and length == 0 and file.tell() == size:
                return width, height
            break
    raise InvalidImage("Incomplete PNG image")


def _jpeg_size(file, size: int) -> tuple[int, int]:
    if file.read(2) != b"\xff\xd8":
        raise InvalidImage("Missing JPEG header")
    dimensions = None
    while file.tell() <= size - 6:
        if file.read(1) != b"\xff":
            break
        marker = file.read(1)
        while marker == b"\xff":
            marker = file.read(1)
        raw = file.read(2)
        if not marker or len(raw) != 2:
            break
        length = int.from_bytes(raw)
        end = file.tell() + length - 2
        if length < 2 or end > size - 2:
            break
        if marker[0] in JPEG_SOF and length >= 8:
            _, height, width = struct.unpack(">BHH", file.read(5))
            dimensions = (width, height)
        elif marker == b"\xda" and length >= 6 and end < size - 2 and dimensions:
            return dimensions
        file.seek(end)
    raise InvalidImage("Incomplete JPEG header or scan")


def image_size(file, suffix: str, size: int, *, check_png_chunks: bool = True) -> ImageSize:
    end = PNG_END if suffix == ".png" else b"\xff\xd9"
    if size < 32:
        raise InvalidImage("Incomplete image")
    file.seek(size - len(end))
    if file.read(len(end)) != end:
        raise InvalidImage("Missing image end marker")
    file.seek(0)
    if suffix == ".png":
        width, height = _png_size(file, size, check_png_chunks)
    else:
        width, height = _jpeg_size(file, size)
    if not width or not height:
        raise InvalidImage("Invalid image dimensions")
    return ImageSize(min(width, height), max(width, height), suffix)
