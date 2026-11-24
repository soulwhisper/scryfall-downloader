"""Inspect native Forge image files without decoding or resizing pixels."""

from dataclasses import dataclass
import struct
import zlib


IMAGE_SUFFIXES = (".jpg", ".png", ".webp")  # Forge's lookup order.
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


def _webp_frame_size(file, kind: bytes, length: int) -> tuple[int, int]:
    if kind == b"VP8L":
        header = file.read(min(length, 5))
        if length <= 5 or len(header) != 5 or header[0] != 0x2f:
            raise InvalidImage("Incomplete WebP lossless frame")
        bits = int.from_bytes(header[1:], "little")
        if bits >> 29:
            raise InvalidImage("Unsupported WebP lossless version")
        return (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1

    header = file.read(min(length, 10))
    if length <= 10 or len(header) != 10 or header[3:6] != b"\x9d\x01\x2a":
        raise InvalidImage("Incomplete WebP lossy frame")
    tag = int.from_bytes(header[:3], "little")
    partition = tag >> 5
    if tag & 1 or ((tag >> 1) & 7) > 3 or not tag & 0x10 or not 0 < partition < length - 10:
        raise InvalidImage("Invalid WebP key frame")
    width, height = struct.unpack_from("<HH", header, 6)
    return width & 0x3fff, height & 0x3fff


def _webp_size(file, size: int) -> tuple[int, int]:
    # RFC 9649: validate the still-image container and frame headers, not compressed pixels.
    if size > 0xfffffffe or size % 2 or file.seek(0, 2) != size:
        raise InvalidImage("Invalid WebP file size")
    file.seek(0)
    header = file.read(12)
    if (len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WEBP"
            or int.from_bytes(header[4:8], "little") != size - 8):
        raise InvalidImage("Invalid WebP RIFF header")

    canvas = frame = None
    flags = metadata = 0
    alpha = False
    while file.tell() < size:
        first = file.tell() == 12
        chunk = file.read(8)
        if len(chunk) != 8:
            raise InvalidImage("Incomplete WebP chunk header")
        kind, length = struct.unpack("<4sI", chunk)
        end = file.tell() + length
        if end + (length & 1) > size:
            raise InvalidImage("Incomplete WebP chunk")
        if first and kind not in (b"VP8X", b"VP8 ", b"VP8L"):
            raise InvalidImage("Missing WebP image header")

        if kind == b"VP8X":
            if not first or length != 10:
                raise InvalidImage("Invalid extended WebP header")
            extended = file.read(10)
            if len(extended) != 10:
                raise InvalidImage("Incomplete extended WebP header")
            flags = extended[0]
            if flags & 2:
                raise InvalidImage("Animated WebP is not a still image")
            width = int.from_bytes(extended[4:7], "little") + 1
            height = int.from_bytes(extended[7:], "little") + 1
            if width * height > 0xffffffff:
                raise InvalidImage("Invalid extended WebP canvas")
            canvas = width, height
        elif kind in (b"VP8 ", b"VP8L"):
            if frame is not None or (alpha and kind == b"VP8L"):
                raise InvalidImage("Invalid WebP image chunk sequence")
            frame = _webp_frame_size(file, kind, length)
            if canvas and (frame != canvas or (kind == b"VP8 " and bool(flags & 0x10) != alpha)):
                raise InvalidImage("WebP frame does not match its canvas or alpha flags")
        elif kind == b"ALPH":
            if not canvas or not flags & 0x10 or alpha or frame is not None or length < 2:
                raise InvalidImage("Invalid WebP alpha chunk")
            method = file.read(1)
            if not method:
                raise InvalidImage("Incomplete WebP alpha data")
            compression = method[0] & 3
            if compression > 1 or (compression == 0 and length != 1 + canvas[0] * canvas[1]):
                raise InvalidImage("Invalid WebP alpha data")
            alpha = True
        elif kind == b"ICCP":
            if not canvas or frame is not None or alpha or not length:
                raise InvalidImage("Invalid WebP profile chunk")
            metadata |= 0x20
        elif kind in (b"EXIF", b"XMP "):
            if not canvas:
                raise InvalidImage("Invalid WebP metadata chunk")
            metadata |= 8 if kind == b"EXIF" else 4
        elif kind in (b"ANIM", b"ANMF"):
            raise InvalidImage("Animated WebP is not a still image")

        file.seek(end)
        if length & 1 and file.read(1) != b"\0":
            raise InvalidImage("Invalid WebP chunk padding")
    if frame is None or flags & 0x2c != metadata:
        raise InvalidImage("Missing WebP image data or metadata")
    return frame


def image_size(file, suffix: str, size: int, *, check_png_chunks: bool = True) -> ImageSize:
    if suffix == ".webp":
        width, height = _webp_size(file, size)
    else:
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
