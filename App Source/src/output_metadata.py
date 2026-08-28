from __future__ import annotations

import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8"
SUPPORTED_METADATA_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
PNG_METADATA_CHUNKS = frozenset(
    {
        b"tEXt",  # Comment, IPTC and other uncompressed text.
        b"zTXt",  # Compressed Comment/IPTC text.
        b"iTXt",  # XMP, IPTC and other UTF-8 text.
        b"eXIf",  # EXIF, including its embedded thumbnail.
        b"iCCP",  # Embedded ICC colour profile.
    }
)
COPY_BUFFER_SIZE = 1024 * 1024
MAX_PNG_CHUNK_SIZE = 0x7FFFFFFF
JPEG_STANDALONE_MARKERS = frozenset(
    {0x01, 0xD8, 0xD9, *range(0xD0, 0xD8)}
)
WEBP_METADATA_CHUNKS = frozenset({b"EXIF", b"XMP ", b"ICCP"})
WEBP_METADATA_FLAGS = 0x20 | 0x08 | 0x04


class OutputMetadataError(RuntimeError):
    """Raised when a metadata-cleaned image cannot be published safely."""


@dataclass(frozen=True)
class PngFingerprint:
    width: int
    height: int
    has_alpha: bool


@dataclass(frozen=True)
class MetadataRemovalResult:
    path: Path
    removed_chunks: tuple[str, ...]
    original_bytes: int
    final_bytes: int
    replaced: bool


@dataclass(frozen=True)
class ImageFingerprint:
    image_format: str
    width: int
    height: int
    has_alpha: bool
    frames: int


@dataclass(frozen=True)
class CleanedImageResult:
    source_path: Path
    output_path: Path
    removed_sections: tuple[str, ...]
    original_bytes: int
    final_bytes: int


def cleaned_image_filename(source_path: str | Path) -> str:
    source = Path(source_path)
    if source.suffix.casefold() not in SUPPORTED_METADATA_IMAGE_EXTS:
        raise OutputMetadataError(
            f"Unsupported image type for metadata cleanup: {source.name}"
        )
    return f"{source.stem}.cleaned{source.suffix}"


def strip_png_metadata_atomic(
    image_path: str | Path,
    log: Any | None = None,
) -> MetadataRemovalResult:
    """
    Remove embedded PNG metadata without decoding or recompressing image pixels.

    Only the supplied file is processed. A temporary file in the same directory
    is validated before os.replace() atomically swaps it over the original.
    """

    path = Path(image_path)
    if path.suffix.casefold() != ".png":
        raise OutputMetadataError(f"Metadata cleanup only supports PNG output: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"Output image not found: {path}")

    original_bytes = path.stat().st_size
    fingerprint, removable_chunks = _inspect_png(path)
    if not removable_chunks:
        return MetadataRemovalResult(
            path=path,
            removed_chunks=(),
            original_bytes=original_bytes,
            final_bytes=original_bytes,
            replaced=False,
        )

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.metadata-",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)

    try:
        removed_chunks = _copy_png_without_metadata(path, temporary_path)
        cleaned_fingerprint, remaining_chunks = _inspect_png(temporary_path)
        if remaining_chunks:
            names = ", ".join(sorted(set(remaining_chunks)))
            raise OutputMetadataError(f"Metadata remained after cleanup ({names}): {path}")
        if cleaned_fingerprint != fingerprint:
            raise OutputMetadataError(
                "Cleaned PNG changed its dimensions or transparency state: "
                f"{path}"
            )

        _verify_png_can_open(temporary_path, fingerprint)
        os.replace(temporary_path, path)
    except Exception as exc:
        if isinstance(exc, OutputMetadataError):
            raise
        raise OutputMetadataError(
            f"Could not safely remove metadata from {path}: {exc}"
        ) from exc
    finally:
        temporary_path.unlink(missing_ok=True)

    final_bytes = path.stat().st_size
    result = MetadataRemovalResult(
        path=path,
        removed_chunks=tuple(chunk.decode("ascii") for chunk in removed_chunks),
        original_bytes=original_bytes,
        final_bytes=final_bytes,
        replaced=True,
    )
    if log is not None:
        log.info(
            "Removed PNG metadata (%s), verified %sx%s alpha=%s, and replaced output: %s",
            ", ".join(result.removed_chunks),
            fingerprint.width,
            fingerprint.height,
            fingerprint.has_alpha,
            path,
        )
    return result


def clean_image_metadata_copy(
    source_path: str | Path,
    output_path: str | Path,
    log: Any | None = None,
) -> CleanedImageResult:
    """
    Write a cleaned copy while preserving the original selected image.

    PNG, JPEG and WebP container metadata is removed without re-encoding image
    pixels. The output is validated in a temporary file before it appears at
    the requested destination.
    """

    source = Path(source_path).resolve()
    output = Path(output_path).resolve()
    extension = source.suffix.casefold()
    if extension not in SUPPORTED_METADATA_IMAGE_EXTS:
        raise OutputMetadataError(
            f"Unsupported image type for metadata cleanup: {source.name}"
        )
    if not source.is_file():
        raise FileNotFoundError(f"Image not found: {source}")
    if source == output:
        raise OutputMetadataError("Manual metadata cleanup must preserve the source image.")
    if output.suffix.casefold() != extension:
        raise OutputMetadataError(
            "Cleaned output must keep the source image extension."
        )
    if output.exists():
        raise FileExistsError(f"Cleaned output already exists: {output}")

    expected = _inspect_image_with_pillow(source)
    expected_formats = {
        ".png": "PNG",
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".webp": "WEBP",
    }
    expected_format = expected_formats[extension]
    if expected.image_format != expected_format:
        raise OutputMetadataError(
            f"File extension does not match image format: {source.name}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.metadata-",
        suffix=".tmp",
        dir=output.parent,
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)

    try:
        if extension == ".png":
            removed = tuple(
                chunk.decode("ascii")
                for chunk in _copy_png_without_metadata(source, temporary)
            )
            _, remaining = _inspect_png(temporary)
        elif extension in {".jpg", ".jpeg"}:
            removed = _copy_jpeg_without_metadata(source, temporary)
            remaining = _inspect_jpeg_metadata(temporary)
        else:
            removed = _copy_webp_without_metadata(source, temporary)
            remaining = _inspect_webp_metadata(temporary)

        if remaining:
            names = ", ".join(sorted(set(remaining)))
            raise OutputMetadataError(
                f"Metadata remained after cleanup ({names}): {source.name}"
            )
        _verify_image_can_open(temporary, expected)
        # On Windows os.rename() will not overwrite a destination that appears
        # after the earlier existence check, so a concurrent/manual file is
        # never silently replaced.
        os.rename(temporary, output)
    except Exception as exc:
        if isinstance(exc, (OutputMetadataError, FileNotFoundError, FileExistsError)):
            raise
        raise OutputMetadataError(
            f"Could not safely create cleaned image from {source}: {exc}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)

    result = CleanedImageResult(
        source_path=source,
        output_path=output,
        removed_sections=tuple(removed),
        original_bytes=source.stat().st_size,
        final_bytes=output.stat().st_size,
    )
    if log is not None:
        log.info(
            "Created cleaned metadata copy (%s), verified %sx%s alpha=%s: %s",
            ", ".join(result.removed_sections) or "already clean",
            expected.width,
            expected.height,
            expected.has_alpha,
            output,
        )
    return result


def _inspect_png(path: Path) -> tuple[PngFingerprint, tuple[str, ...]]:
    width: int | None = None
    height: int | None = None
    colour_type: int | None = None
    has_transparency_chunk = False
    removable_chunks: list[str] = []
    saw_iend = False

    with path.open("rb") as source:
        if _read_exact(source, len(PNG_SIGNATURE)) != PNG_SIGNATURE:
            raise OutputMetadataError(f"Not a valid PNG file: {path}")

        while not saw_iend:
            header = _read_exact(source, 8)
            length, chunk_type = _parse_chunk_header(header, path)
            if chunk_type == b"IHDR":
                if width is not None or length != 13:
                    raise OutputMetadataError(f"Invalid PNG IHDR chunk: {path}")
                payload = _read_exact(source, length)
                width, height, _, colour_type, _, _, _ = struct.unpack(
                    ">IIBBBBB", payload
                )
                _read_exact(source, 4)
            else:
                if chunk_type == b"tRNS":
                    has_transparency_chunk = True
                if chunk_type in PNG_METADATA_CHUNKS:
                    removable_chunks.append(chunk_type.decode("ascii"))
                _skip_exact(source, length + 4)
            saw_iend = chunk_type == b"IEND"

    if width is None or height is None or colour_type is None or not saw_iend:
        raise OutputMetadataError(f"PNG is missing required chunks: {path}")

    has_alpha = colour_type in {4, 6} or has_transparency_chunk
    return (
        PngFingerprint(width=width, height=height, has_alpha=has_alpha),
        tuple(removable_chunks),
    )


def _copy_png_without_metadata(source_path: Path, target_path: Path) -> tuple[bytes, ...]:
    removed_chunks: list[bytes] = []
    saw_iend = False

    with source_path.open("rb") as source, target_path.open("wb") as target:
        signature = _read_exact(source, len(PNG_SIGNATURE))
        if signature != PNG_SIGNATURE:
            raise OutputMetadataError(f"Not a valid PNG file: {source_path}")
        target.write(signature)

        while not saw_iend:
            header = _read_exact(source, 8)
            length, chunk_type = _parse_chunk_header(header, source_path)
            if chunk_type in PNG_METADATA_CHUNKS:
                removed_chunks.append(chunk_type)
                _skip_exact(source, length + 4)
            else:
                target.write(header)
                _copy_exact(source, target, length + 4)
            saw_iend = chunk_type == b"IEND"

        target.flush()

    return tuple(removed_chunks)


def _inspect_jpeg_metadata(path: Path) -> tuple[str, ...]:
    return _process_jpeg(path)


def _copy_jpeg_without_metadata(
    source_path: Path,
    target_path: Path,
) -> tuple[str, ...]:
    return _process_jpeg(source_path, target_path)


def _process_jpeg(
    source_path: Path,
    target_path: Path | None = None,
) -> tuple[str, ...]:
    """
    Parse every JPEG marker through EOI and optionally write a cleaned copy.

    Entropy-coded scan data, byte stuffing and restart markers are copied
    exactly. Metadata markers between progressive scans are still removed, and
    bytes after EOI are discarded so hidden trailing payload cannot survive.
    """

    removed: list[str] = []
    source = source_path.open("rb")
    target = target_path.open("wb") if target_path is not None else None
    try:
        signature = _read_exact(source, 2, "JPEG")
        if signature != JPEG_SIGNATURE:
            raise OutputMetadataError(f"Not a valid JPEG file: {source_path}")
        if target is not None:
            target.write(signature)

        while True:
            marker, encoded_marker = _read_jpeg_marker_token(source, source_path)
            if marker == 0xD9:
                if target is not None:
                    target.write(encoded_marker)
                trailing = source.read()
                if trailing.lstrip(b"\x00\t\r\n ").startswith(JPEG_SIGNATURE):
                    raise OutputMetadataError(
                        f"Multi-picture JPEG is not supported: {source_path}"
                    )
                if trailing:
                    removed.append("Trailing data")
                break
            if marker == 0xD8:
                raise OutputMetadataError(f"Unexpected JPEG SOI marker in {source_path}")
            if marker in JPEG_STANDALONE_MARKERS:
                if target is not None:
                    target.write(encoded_marker)
                continue

            length_bytes = _read_exact(source, 2, "JPEG")
            segment_length = struct.unpack(">H", length_bytes)[0]
            if segment_length < 2:
                raise OutputMetadataError(f"Invalid JPEG segment in {source_path}")
            payload = _read_exact(source, segment_length - 2, "JPEG")
            if marker == 0xE2 and payload.startswith(b"MPF\x00"):
                raise OutputMetadataError(
                    f"Multi-picture JPEG (MPO) is not supported: {source_path}"
                )
            label = _jpeg_metadata_label(marker, payload)

            if label is not None:
                removed.append(label)
            elif target is not None:
                target.write(encoded_marker)
                target.write(length_bytes)
                target.write(payload)
            if marker == 0xDA:
                _copy_jpeg_entropy_until_marker(source, target, source_path)

        if target is not None:
            target.flush()
    finally:
        source.close()
        if target is not None:
            target.close()
    return tuple(removed)


def _copy_jpeg_entropy_until_marker(
    source: BinaryIO,
    target: BinaryIO | None,
    path: Path,
) -> None:
    while True:
        block = source.read(COPY_BUFFER_SIZE)
        if not block:
            raise OutputMetadataError(f"JPEG is missing its EOI marker: {path}")
        marker_offset = block.find(b"\xff")
        if marker_offset < 0:
            if target is not None:
                target.write(block)
            continue
        if target is not None and marker_offset:
            target.write(block[:marker_offset])

        source.seek(-(len(block) - marker_offset), os.SEEK_CUR)
        token = bytearray(_read_exact(source, 1, "JPEG"))
        while True:
            marker_byte = _read_exact(source, 1, "JPEG")
            token.extend(marker_byte)
            marker = marker_byte[0]
            if marker == 0xFF:
                continue
            break

        if marker == 0x00 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            if target is not None:
                target.write(token)
            continue
        if marker == 0xDC:
            length_bytes = _read_exact(source, 2, "JPEG")
            segment_length = struct.unpack(">H", length_bytes)[0]
            if segment_length < 2:
                raise OutputMetadataError(f"Invalid JPEG DNL segment in {path}")
            payload = _read_exact(source, segment_length - 2, "JPEG")
            if target is not None:
                target.write(token)
                target.write(length_bytes)
                target.write(payload)
            continue

        source.seek(-len(token), os.SEEK_CUR)
        return


def _read_jpeg_marker_token(source: BinaryIO, path: Path) -> tuple[int, bytes]:
    token = bytearray(_read_exact(source, 1, "JPEG"))
    if token != b"\xff":
        raise OutputMetadataError(f"Invalid JPEG marker in {path}")
    while True:
        marker_byte = _read_exact(source, 1, "JPEG")
        token.extend(marker_byte)
        marker = marker_byte[0]
        if marker == 0xFF:
            continue
        if marker == 0x00:
            raise OutputMetadataError(f"Invalid stuffed JPEG marker in {path}")
        return marker, bytes(token)


def _jpeg_metadata_label(marker: int, payload: bytes) -> str | None:
    if marker == 0xFE:
        return "Comment"
    if marker == 0xE1:
        if payload.startswith(b"Exif\x00\x00"):
            return "EXIF"
        if payload.startswith(
            (
                b"http://ns.adobe.com/xap/1.0/\x00",
                b"http://ns.adobe.com/xmp/extension/\x00",
            )
        ):
            return "XMP"
        return "APP1 metadata"
    if marker == 0xED:
        return "IPTC"
    if marker == 0xE2 and payload.startswith(b"ICC_PROFILE\x00"):
        return "ICC profile"
    return None


def _inspect_webp_metadata(path: Path) -> tuple[str, ...]:
    found: list[str] = []
    with path.open("rb") as source:
        end = _read_webp_header(source, path)
        while source.tell() < end:
            header = _read_exact(source, 8, "WebP")
            chunk_type, length = struct.unpack("<4sI", header)
            if chunk_type in WEBP_METADATA_CHUNKS:
                found.append(chunk_type.decode("ascii").strip())
            _skip_exact(source, length + (length & 1), "WebP")
        if source.tell() != end:
            raise OutputMetadataError(f"Invalid WebP RIFF size: {path}")
    return tuple(found)


def _copy_webp_without_metadata(
    source_path: Path,
    target_path: Path,
) -> tuple[str, ...]:
    removed: list[str] = []
    with source_path.open("rb") as source, target_path.open("w+b") as target:
        end = _read_webp_header(source, source_path)
        target.write(b"RIFF\x00\x00\x00\x00WEBP")

        while source.tell() < end:
            header = _read_exact(source, 8, "WebP")
            chunk_type, length = struct.unpack("<4sI", header)
            padded_length = length + (length & 1)
            if chunk_type in WEBP_METADATA_CHUNKS:
                removed.append(chunk_type.decode("ascii").strip())
                _skip_exact(source, padded_length, "WebP")
                continue

            target.write(header)
            if chunk_type == b"VP8X":
                payload = bytearray(_read_exact(source, length, "WebP"))
                if not payload:
                    raise OutputMetadataError(f"Invalid VP8X chunk: {source_path}")
                payload[0] &= ~WEBP_METADATA_FLAGS
                target.write(payload)
                if length & 1:
                    target.write(_read_exact(source, 1, "WebP"))
            else:
                _copy_exact(source, target, padded_length, "WebP")

        if source.tell() != end:
            raise OutputMetadataError(f"Invalid WebP RIFF size: {source_path}")
        final_size = target.tell()
        if final_size < 12 or final_size - 8 > 0xFFFFFFFF:
            raise OutputMetadataError(f"Invalid cleaned WebP size: {source_path}")
        target.seek(4)
        target.write(struct.pack("<I", final_size - 8))
        target.flush()
    return tuple(removed)


def _read_webp_header(source: BinaryIO, path: Path) -> int:
    header = _read_exact(source, 12, "WebP")
    if header[:4] != b"RIFF" or header[8:] != b"WEBP":
        raise OutputMetadataError(f"Not a valid WebP file: {path}")
    declared_size = struct.unpack("<I", header[4:8])[0]
    end = declared_size + 8
    file_size = os.fstat(source.fileno()).st_size
    if end < 12 or end > file_size:
        raise OutputMetadataError(f"Invalid WebP RIFF size: {path}")
    return end


def _inspect_image_with_pillow(path: Path) -> ImageFingerprint:
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - installer always ships Pillow.
        raise OutputMetadataError("Pillow is required to inspect image output.") from exc

    try:
        with Image.open(path) as image:
            fingerprint = ImageFingerprint(
                image_format=str(image.format or "").upper(),
                width=int(image.width),
                height=int(image.height),
                has_alpha=_pillow_image_has_alpha(image),
                frames=int(getattr(image, "n_frames", 1)),
            )
            _load_all_image_frames(image)
            return fingerprint
    except Exception as exc:
        raise OutputMetadataError(f"Could not open image: {path}: {exc}") from exc


def _verify_image_can_open(path: Path, expected: ImageFingerprint) -> None:
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - installer always ships Pillow.
        raise OutputMetadataError("Pillow is required to verify image output.") from exc

    try:
        with Image.open(path) as image:
            actual = ImageFingerprint(
                image_format=str(image.format or "").upper(),
                width=int(image.width),
                height=int(image.height),
                has_alpha=_pillow_image_has_alpha(image),
                frames=int(getattr(image, "n_frames", 1)),
            )
            if actual != expected:
                raise OutputMetadataError(
                    "Cleaned image changed its format, dimensions, transparency "
                    "or frame count: "
                    f"{path}"
                )
            _load_all_image_frames(image)
        with Image.open(path) as image:
            image.verify()
    except OutputMetadataError:
        raise
    except Exception as exc:
        raise OutputMetadataError(f"Cleaned image failed validation: {path}: {exc}") from exc


def _load_all_image_frames(image: Any) -> None:
    frame_count = int(getattr(image, "n_frames", 1))
    for frame_index in range(frame_count):
        if frame_index:
            image.seek(frame_index)
        image.load()


def _pillow_image_has_alpha(image: Any) -> bool:
    return image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info


def _verify_png_can_open(path: Path, expected: PngFingerprint) -> None:
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - installer always ships Pillow.
        raise OutputMetadataError(
            "Pillow is required to verify cleaned PNG output."
        ) from exc

    try:
        with Image.open(path) as image:
            actual_size = tuple(image.size)
            actual_alpha = _pillow_image_has_alpha(image)
            if actual_size != (expected.width, expected.height):
                raise OutputMetadataError(
                    f"Cleaned PNG size changed from "
                    f"{expected.width}x{expected.height} to "
                    f"{actual_size[0]}x{actual_size[1]}: {path}"
                )
            if actual_alpha != expected.has_alpha:
                raise OutputMetadataError(
                    f"Cleaned PNG transparency state changed: {path}"
                )
            image.verify()
    except OutputMetadataError:
        raise
    except Exception as exc:
        raise OutputMetadataError(
            f"Cleaned PNG failed validation and the original was kept: {path}: {exc}"
        ) from exc


def _parse_chunk_header(header: bytes, path: Path) -> tuple[int, bytes]:
    length, chunk_type = struct.unpack(">I4s", header)
    if length > MAX_PNG_CHUNK_SIZE:
        raise OutputMetadataError(f"PNG chunk is too large in {path}")
    if len(chunk_type) != 4 or not all(
        (65 <= byte <= 90) or (97 <= byte <= 122) for byte in chunk_type
    ):
        raise OutputMetadataError(f"Invalid PNG chunk type in {path}")
    return length, chunk_type


def _read_exact(source: BinaryIO, size: int, context: str = "PNG") -> bytes:
    data = source.read(size)
    if len(data) != size:
        raise OutputMetadataError(f"{context} ended unexpectedly.")
    return data


def _skip_exact(source: BinaryIO, size: int, context: str = "PNG") -> None:
    current = source.tell()
    end = current + size
    file_size = os.fstat(source.fileno()).st_size
    if end > file_size:
        raise OutputMetadataError(f"{context} ended unexpectedly.")
    source.seek(end)


def _copy_exact(
    source: BinaryIO,
    target: BinaryIO,
    size: int,
    context: str = "PNG",
) -> None:
    remaining = size
    while remaining:
        data = source.read(min(remaining, COPY_BUFFER_SIZE))
        if not data:
            raise OutputMetadataError(f"{context} ended unexpectedly.")
        target.write(data)
        remaining -= len(data)
