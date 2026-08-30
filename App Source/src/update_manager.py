"""Background updates from this product's pinned public GitHub release channel.

The channel belongs to the installed app, never to user config.json. Check and
download belong in a background worker. They never use Tk or stop a running job.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import stat
import struct
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib import parse, request
import zipfile

from atomic_json import atomic_write_json

APP_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ID = "png-mockup"
APP_DISPLAY_NAME = "PNG Mockup"
CHANNEL_FILE = "update-channel.json"
MANIFEST_ASSET_NAME = "png-mockup-update.json"
INSTALLER_ASSET_NAME = "PNG-Mockup-Setup.exe"
LOCAL_INSTALLER_NAME = "PNG Mockup Setup.exe"
UPDATE_UNAVAILABLE_MESSAGE = (
    "PNG Mockup chưa có kênh cập nhật trực tuyến riêng. "
    "Để cập nhật, hãy dùng bộ cài PNG Mockup mới. "
    "Cấu hình và ảnh của bạn được giữ lại khi cài vào đúng thư mục app."
)
NETWORK_TIMEOUT = 8
CHUNK_BYTES = 64 * 1024
MAX_INSTALLER_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
_LEGACY_REPOSITORIES = frozenset({"minhtuan5991/autodesignpod"})
_ASSET_HOSTS = frozenset({
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
})
_VERSION_RE = re.compile(r"v?(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
_SHA_RE = re.compile(r"[0-9a-fA-F]{64}")
_LOGGER = logging.getLogger("png_mockup.updates")


class UpdateError(RuntimeError):
    pass


class UpdateCancelled(UpdateError):
    pass


@dataclass(frozen=True)
class UpdateChannel:
    enabled: bool = False
    repository: str = ""
    reason: str = UPDATE_UNAVAILABLE_MESSAGE


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    notes: str
    release_url: str
    download_url: str
    size: int
    sha256: str
    repository: str = ""


def _log(log: Any, message: str) -> None:
    try:
        if callable(log):
            log(message)
        else:
            (log or _LOGGER).info(message)
    except Exception:
        # A UI queue closing must not turn a harmless update into an app error.
        pass


def _cancelled(cancel_event: Any) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise UpdateCancelled("Update cancelled.")


def _version(value: Any) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise UpdateError("Invalid stable release version.")
    return tuple(int(part) for part in match.groups())


def _canonical_version(value: Any) -> str:
    return ".".join(str(part) for part in _version(value))


def _json(data: bytes) -> dict[str, Any]:
    value = json.loads(data.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise UpdateError("Expected a JSON object.")
    return value


def _read_json(path: Path, limit: int = MAX_JSON_BYTES) -> dict[str, Any]:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("JSON file exceeds its safe size.")
    return _json(data)


def read_current_version(app_root: Path = APP_ROOT) -> str:
    """Keep the title API compatible, with a safe fallback for invalid identity."""
    try:
        data = _read_json(Path(app_root) / "version.json")
        if data.get("product_id") != PRODUCT_ID:
            return "0.0.0"
        version = str(data.get("version", "")).strip()
        _version(version)
        return version
    except (OSError, ValueError, TypeError, RecursionError, UpdateError):
        return "0.0.0"


def _installed_version(app_root: Path) -> str:
    data = _read_json(app_root / "version.json")
    if data.get("product_id") != PRODUCT_ID or data.get("name") != APP_DISPLAY_NAME:
        raise UpdateError("The installation has a different product identity.")
    return _canonical_version(data.get("version"))


def normalize_github_repo(value: str) -> str:
    if not isinstance(value, str):
        raise UpdateError("The pinned repository must be owner/repository.")
    value = value.strip()
    if not value:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}", value):
        raise UpdateError("The pinned repository must be owner/repository.")
    if value.split("/")[1] in {".", ".."} or value.casefold() in _LEGACY_REPOSITORIES:
        raise UpdateError("The original application's repository cannot be used.")
    return value


def _channel_data(data: dict[str, Any]) -> UpdateChannel:
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise UpdateError("Unsupported update channel schema.")
    if data.get("product_id") != PRODUCT_ID or type(data.get("enabled")) is not bool:
        raise UpdateError("Invalid update channel identity.")
    repository = normalize_github_repo(data.get("repository"))
    if data["enabled"] and not repository:
        raise UpdateError("The update channel has no pinned repository.")
    return UpdateChannel(data["enabled"], repository, "" if data["enabled"] else UPDATE_UNAVAILABLE_MESSAGE)


def _source_checkout(app_root: Path) -> bool:
    root = app_root.resolve()
    for parent in (root, *root.parents):
        source = parent / "App Source"
        if (parent / "build_setup.ps1").is_file() and source.is_dir():
            if root == parent or root == source or source in root.parents:
                return True
    return False


def read_channel(app_root: Path = APP_ROOT, *, log: Any = None) -> UpdateChannel:
    try:
        if _source_checkout(Path(app_root)):
            return UpdateChannel(reason="Bản mã nguồn phát triển; hãy dùng bộ cài PNG Mockup để cập nhật.")
        return _channel_data(_read_json(Path(app_root) / CHANNEL_FILE, 16 * 1024))
    except FileNotFoundError:
        return UpdateChannel()
    except (OSError, ValueError, TypeError, RecursionError, UpdateError) as exc:
        _log(log, f"Update channel ignored ({type(exc).__name__}).")
        return UpdateChannel(reason="Kênh cập nhật PNG Mockup chưa được cấu hình hợp lệ.")


def auto_check_due(app_root: Path = APP_ROOT, interval_hours: int = 24) -> bool:
    """Check on every startup; an old 24-hour user setting cannot suppress it."""
    del interval_hours
    return read_channel(app_root).enabled


def _https_url(url: str) -> parse.SplitResult:
    if not isinstance(url, str) or any(ord(char) < 32 for char in url) or "\\" in url:
        raise UpdateError("Invalid update URL.")
    try:
        parts = parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UpdateError("Invalid update URL.") from exc
    if (
        parts.scheme != "https" or not parts.hostname or parts.username is not None
        or parts.password is not None or port not in (None, 443) or parts.fragment
    ):
        raise UpdateError("Only approved HTTPS update URLs are accepted.")
    return parts


def _api_validator(repository: str) -> Callable[[str], None]:
    expected = f"/repos/{repository}/releases/latest".casefold()

    def validate(url: str) -> None:
        parts = _https_url(url)
        if parts.hostname != "api.github.com" or parse.unquote(parts.path).casefold() != expected or parts.query:
            raise UpdateError("The release API URL differs from the pinned repository.")
    return validate


def _asset_url(repository: str, tag: str, name: str) -> str:
    return f"https://github.com/{repository}/releases/download/{parse.quote(tag, safe='')}/{parse.quote(name, safe='')}"


def _asset_validator(repository: str, tag: str, name: str, *, redirects: bool) -> Callable[[str], None]:
    expected = f"/{repository}/releases/download/{tag}/{name}".split("/")

    def validate(url: str) -> None:
        parts = _https_url(url)
        if redirects and parts.hostname in _ASSET_HOSTS:
            if not parts.path.startswith("/") or parts.path == "/":
                raise UpdateError("Invalid GitHub release asset location.")
            return
        segments = parse.unquote(parts.path).split("/")
        if (
            parts.hostname != "github.com" or parts.query or len(segments) != len(expected)
            or [part.casefold() for part in segments[:3]] != [part.casefold() for part in expected[:3]]
            or segments[3:] != expected[3:]
        ):
            raise UpdateError("The asset URL differs from the pinned release.")
    return validate


class _PinnedRedirectHandler(request.HTTPRedirectHandler):
    max_redirections = 4
    max_repeats = 2

    def __init__(self, validate: Callable[[str], None], cancel_event: Any = None) -> None:
        self.validate = validate
        self.cancel_event = cancel_event
        super().__init__()

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _cancelled(self.cancel_event)
        self.validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(
    url: str, validate: Callable[[str], None], *, accept: str, cancel_event: Any = None,
) -> Any:
    _cancelled(cancel_event)
    validate(url)
    opener = request.build_opener(_PinnedRedirectHandler(validate, cancel_event))
    response = opener.open(request.Request(url, headers={
        "Accept": accept,
        "Accept-Encoding": "identity",
        "User-Agent": "PNGMockup-Updater",
        "X-GitHub-Api-Version": "2026-03-10",
    }), timeout=NETWORK_TIMEOUT)
    try:
        _cancelled(cancel_event)
        validate(response.geturl())
        if response.getcode() != 200:
            raise UpdateError("Unexpected update server response.")
        return response
    except Exception:
        response.close()
        raise


def _read_chunk(response: Any, count: int) -> bytes:
    # HTTPResponse.read(n) may wait indefinitely for n bytes on a slowly trickling
    # connection. read1 returns after one socket read so cancellation is checked.
    reader = getattr(response, "read1", response.read)
    return reader(count)


def _read_response(response: Any, limit: int, cancel_event: Any = None) -> bytes:
    chunks: list[bytes] = []
    count = 0
    while True:
        _cancelled(cancel_event)
        chunk = _read_chunk(response, min(CHUNK_BYTES, limit + 1 - count))
        if not chunk:
            return b"".join(chunks)
        count += len(chunk)
        if count > limit:
            raise UpdateError("Update metadata exceeds its safe size.")
        chunks.append(chunk)


def _asset(assets: Any, name: str) -> dict[str, Any]:
    if not isinstance(assets, list):
        raise UpdateError("Release assets are missing.")
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1 or matches[0].get("state") != "uploaded":
        raise UpdateError("The required release asset is missing or duplicated.")
    return matches[0]


def _positive_size(value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        raise UpdateError("Invalid update asset size.")
    return value


def _release_from_manifest(
    manifest: dict[str, Any], channel: UpdateChannel, release: dict[str, Any],
) -> ReleaseInfo:
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise UpdateError("Unsupported update manifest schema.")
    if manifest.get("product_id") != PRODUCT_ID:
        raise UpdateError("Update manifest belongs to another product.")
    repository = normalize_github_repo(manifest.get("repository"))
    if repository.casefold() != channel.repository.casefold():
        raise UpdateError("Update manifest changed the pinned repository.")
    version = _canonical_version(manifest.get("version"))
    if manifest.get("version") != version:
        raise UpdateError("Update manifest version is not canonical.")
    tag = release.get("tag_name")
    if manifest.get("tag") != tag or _canonical_version(tag) != version:
        raise UpdateError("Update manifest version/tag differs from the release.")
    installer = manifest.get("installer")
    if not isinstance(installer, dict) or installer.get("name") != INSTALLER_ASSET_NAME:
        raise UpdateError("Update manifest has a different installer name.")
    size = _positive_size(installer.get("size"), MAX_INSTALLER_BYTES)
    sha256 = installer.get("sha256")
    if not isinstance(sha256, str) or not _SHA_RE.fullmatch(sha256):
        raise UpdateError("Update manifest installer hash is invalid.")
    asset = _asset(release.get("assets"), INSTALLER_ASSET_NAME)
    if type(asset.get("size")) is not int or asset["size"] != size:
        raise UpdateError("Installer size differs between release and manifest.")
    _asset_validator(repository, tag, INSTALLER_ASSET_NAME, redirects=False)(asset.get("browser_download_url"))
    digest = asset.get("digest")
    if digest is not None and digest != "sha256:" + sha256.lower():
        raise UpdateError("GitHub asset digest differs from the update manifest.")
    return ReleaseInfo(
        version, tag, str(release.get("body") or "")[:16000],
        f"https://github.com/{repository}/releases/tag/{tag}",
        _asset_url(repository, tag, INSTALLER_ASSET_NAME), size, sha256.lower(), repository,
    )


def check_latest_release(
    app_root: Path = APP_ROOT, *, cancel_event: Any = None, log: Any = None,
) -> ReleaseInfo | None:
    """Return a validated newer release; offline/no-update cases are silent."""
    try:
        root = Path(app_root).resolve()
        channel = read_channel(root, log=log)
        if not channel.enabled:
            return None
        current = _version(_installed_version(root))
        url = f"https://api.github.com/repos/{channel.repository}/releases/latest"
        with _open_url(url, _api_validator(channel.repository), accept="application/vnd.github+json", cancel_event=cancel_event) as response:
            release = _json(_read_response(response, MAX_JSON_BYTES, cancel_event))
        if release.get("draft") is not False or release.get("prerelease") is not False:
            raise UpdateError("Only published stable releases can update this app.")
        tag = release.get("tag_name")
        if _version(tag) <= current:
            return None
        manifest_asset = _asset(release.get("assets"), MANIFEST_ASSET_NAME)
        manifest_size = _positive_size(manifest_asset.get("size"), MAX_JSON_BYTES)
        manifest_url = manifest_asset.get("browser_download_url")
        _asset_validator(channel.repository, tag, MANIFEST_ASSET_NAME, redirects=False)(manifest_url)
        with _open_url(
            manifest_url, _asset_validator(channel.repository, tag, MANIFEST_ASSET_NAME, redirects=True),
            accept="application/octet-stream", cancel_event=cancel_event,
        ) as response:
            raw_manifest = _read_response(response, manifest_size, cancel_event)
        if len(raw_manifest) != manifest_size:
            raise UpdateError("Update manifest size differs from the release asset.")
        result = _release_from_manifest(_json(raw_manifest), channel, release)
        _cancelled(cancel_event)
        return result
    except Exception as exc:
        # Never show an offline/rate-limit/invalid-release dialog on startup.
        # Do not log signed CDN URLs or response bodies.
        _log(log, f"Background update check skipped ({type(exc).__name__}).")
        return None


def _validate_release(release: ReleaseInfo, app_root: Path, channel: UpdateChannel) -> None:
    if not isinstance(release, ReleaseInfo) or not channel.enabled:
        raise UpdateError(UPDATE_UNAVAILABLE_MESSAGE)
    if normalize_github_repo(release.repository).casefold() != channel.repository.casefold():
        raise UpdateError("The cached release differs from the pinned repository.")
    if release.version != _canonical_version(release.version) or _canonical_version(release.tag) != release.version:
        raise UpdateError("The cached release version/tag is invalid.")
    if _version(release.version) <= _version(_installed_version(app_root)):
        raise UpdateError("The update is not newer than this installation.")
    _positive_size(release.size, MAX_INSTALLER_BYTES)
    if not isinstance(release.sha256, str) or not _SHA_RE.fullmatch(release.sha256):
        raise UpdateError("The cached installer hash is invalid.")
    _asset_validator(channel.repository, release.tag, INSTALLER_ASSET_NAME, redirects=False)(release.download_url)
    if release.release_url != f"https://github.com/{release.repository}/releases/tag/{release.tag}":
        raise UpdateError("The cached release page differs from the pinned repository.")


def _owned_path(app_root: Path, path: Path) -> Path:
    root = app_root.resolve()
    cache = root / "logs" / "updates"
    absolute = path.absolute()
    if absolute != cache and cache not in absolute.parents:
        raise UpdateError("The update cache must stay inside this application.")
    if absolute.resolve() != absolute:
        raise UpdateError("Symbolic links/junctions cannot redirect the update cache.")
    return absolute


def _cache_directory(app_root: Path, release: ReleaseInfo) -> Path:
    return _owned_path(app_root, app_root / "logs" / "updates" / f"{release.version}-{release.sha256.lower()[:20]}")


def _hash_stream(stream: Any, length: int, cancel_event: Any = None) -> str:
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        _cancelled(cancel_event)
        chunk = stream.read(min(CHUNK_BYTES, remaining))
        if not chunk:
            raise UpdateError("Installer payload is truncated.")
        digest.update(chunk)
        remaining -= len(chunk)
    _cancelled(cancel_event)
    return digest.hexdigest()


class _PayloadView(io.RawIOBase):
    def __init__(self, source: Any, start: int, length: int) -> None:
        self.source, self.start, self.length, self.position = source, start, length, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = 0) -> int:
        position = offset + (0 if whence == 0 else self.position if whence == 1 else self.length)
        if position < 0 or position > self.length:
            raise UpdateError("ZIP read escaped the installer payload.")
        self.position = position
        return position

    def read(self, size: int = -1) -> bytes:
        count = self.length - self.position if size < 0 else min(size, self.length - self.position)
        self.source.seek(self.start + self.position)
        data = self.source.read(count)
        self.position += len(data)
        return data


def _zip_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise UpdateError("Invalid installer ZIP path.")
    parts = value.replace("\\", "/").split("/")
    for part in parts:
        if (
            not part or part in {".", ".."} or part.endswith((".", " "))
            or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
            or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", part.split(".")[0], re.I)
        ):
            raise UpdateError("Unsafe installer ZIP path.")
    path = "/".join(parts)
    allowed_root = {
        "PNG Mockup.exe", "Uninstall.exe", "Show technical files.exe",
        "config.json", "version.json", "README.md",
        "THIRD_PARTY_NOTICES.md", "setup_manifest.json", CHANNEL_FILE,
    }
    if not (
        len(parts) == 1 and path in allowed_root
        or len(parts) > 1 and parts[0] in {"src", "assets", "tools"}
        or len(parts) > 2 and parts[:2] == ["runtime", "python"]
    ):
        raise UpdateError("Installer ZIP contains user data or unsupported files.")
    return path


def _zip_json(archive: zipfile.ZipFile, info: zipfile.ZipInfo, cancel_event: Any = None) -> dict[str, Any]:
    if info.file_size > 8 * 1024 * 1024:
        raise UpdateError("Installer JSON exceeds its safe size.")
    with archive.open(info) as member:
        data = _read_response(member, info.file_size, cancel_event)
    if len(data) != info.file_size:
        raise UpdateError("Installer JSON length mismatch.")
    return _json(data)


def verify_installer(
    installer: Path, release: ReleaseInfo, *, cancel_event: Any = None,
) -> None:
    """Verify bytes and every declared ZIP member without executing the EXE."""
    with Path(installer).open("rb") as source:
        size = os.fstat(source.fileno()).st_size
        if size != release.size or size < 116 or source.read(2) != b"MZ":
            raise UpdateError("The downloaded file is not the expected installer.")
        source.seek(0)
        if _hash_stream(source, size, cancel_event) != release.sha256.lower():
            raise UpdateError("Installer SHA-256 mismatch.")
        source.seek(size - 48)
        length, payload_hash, marker = struct.unpack("<q32s8s", source.read(48))
        if marker != b"PNGMSP01" or not 0 < length <= size - 116:
            raise UpdateError("PNG Mockup installer footer is invalid.")
        start = size - 48 - length
        source.seek(60)
        pe_offset = struct.unpack("<I", source.read(4))[0]
        if not 64 <= pe_offset <= start - 4:
            raise UpdateError("Installer PE header is invalid.")
        source.seek(pe_offset)
        if source.read(4) != b"PE\0\0":
            raise UpdateError("Installer PE signature is invalid.")
        source.seek(start)
        if _hash_stream(source, length, cancel_event) != payload_hash.hex():
            raise UpdateError("Installer payload SHA-256 mismatch.")
        with zipfile.ZipFile(_PayloadView(source, start, length)) as archive:
            infos = archive.infolist()
            if not 0 < len(infos) <= 20000:
                raise UpdateError("Unexpected installer ZIP entry count.")
            entries: dict[str, zipfile.ZipInfo] = {}
            total = 0
            for info in infos:
                _cancelled(cancel_event)
                key = _zip_path(info.filename).casefold()
                mode = stat.S_IFMT(info.external_attr >> 16)
                if key in entries or info.is_dir() or mode not in (0, stat.S_IFREG) or info.flag_bits & 1:
                    raise UpdateError("Duplicate, encrypted or linked installer ZIP entry.")
                total += info.file_size
                if not 0 <= info.file_size <= MAX_MEMBER_BYTES or total > MAX_EXPANDED_BYTES:
                    raise UpdateError("Installer ZIP exceeds its safe expanded size.")
                entries[key] = info
            manifest_info = entries.get("setup_manifest.json")
            if manifest_info is None:
                raise UpdateError("Installer file manifest is missing.")
            manifest = _zip_json(archive, manifest_info, cancel_event)
            if (
                type(manifest.get("format")) is not int or manifest["format"] != 1
                or manifest.get("product_id") != PRODUCT_ID or manifest.get("version") != release.version
                or not isinstance(manifest.get("files"), list)
            ):
                raise UpdateError("Installer manifest identity/version is invalid.")
            declared: set[str] = set()
            for row in manifest["files"]:
                _cancelled(cancel_event)
                if not isinstance(row, dict):
                    raise UpdateError("Invalid installer file record.")
                key = _zip_path(row.get("path")).casefold()
                info = entries.get(key)
                expected = row.get("sha256")
                if (
                    key in declared or key == "setup_manifest.json" or info is None
                    or type(row.get("length")) is not int or row["length"] != info.file_size
                    or not isinstance(expected, str) or not _SHA_RE.fullmatch(expected)
                ):
                    raise UpdateError("Installer file record differs from its ZIP entry.")
                declared.add(key)
                with archive.open(info) as member:
                    if _hash_stream(member, info.file_size, cancel_event) != expected.lower() or member.read(1):
                        raise UpdateError("Installer file hash/length mismatch.")
            if declared != entries.keys() - {"setup_manifest.json"}:
                raise UpdateError("Installer ZIP contains undeclared files.")
            required = {
                "png mockup.exe", "uninstall.exe", "show technical files.exe",
                "config.json", "version.json", CHANNEL_FILE,
                "src/gui.py", "src/app.py", "src/update_manager.py", "assets/app_icon.ico",
                "assets/uninstall_icon.ico", "assets/technical_files_icon.ico",
                "runtime/python/python.exe", "runtime/python/pythonw.exe",
                "runtime/python/python312.dll", "runtime/python/python312._pth",
                "runtime/python/lib/tkinter/__init__.py",
                "runtime/python/lib/site-packages/pil/__init__.py",
                "runtime/python/tcl/tcl8.6/init.tcl", "runtime/python/tcl/tk8.6/tk.tcl",
                "tools/runtime_check.py", "tools/oxipng/oxipng.exe",
                "runtime/python/license.txt", "tools/oxipng/license", "third_party_notices.md",
            }
            if not required <= declared:
                raise UpdateError("Installer is missing a required PNG Mockup component.")
            identity = _zip_json(archive, entries["version.json"], cancel_event)
            if (
                identity.get("product_id") != PRODUCT_ID or identity.get("name") != APP_DISPLAY_NAME
                or identity.get("version") != release.version
            ):
                raise UpdateError("Installer application identity/version mismatch.")
            channel = _channel_data(_zip_json(archive, entries[CHANNEL_FILE], cancel_event))
            if not channel.enabled or channel.repository.casefold() != release.repository.casefold():
                raise UpdateError("The installer attempts to change the pinned update repository.")
        _cancelled(cancel_event)


def _cache_metadata(release: ReleaseInfo) -> dict[str, Any]:
    return {"schema_version": 1, "product_id": PRODUCT_ID, "release": asdict(release)}


def download_release(
    release: ReleaseInfo, app_root: Path = APP_ROOT, *,
    progress: Callable[[int, int], None] | None = None, cancel_event: Any = None, log: Any = None,
) -> Path | None:
    part: Path | None = None
    try:
        root = Path(app_root).resolve()
        channel = read_channel(root, log=log)
        _validate_release(release, root, channel)
        _cancelled(cancel_event)
        folder = _cache_directory(root, release)
        folder.mkdir(parents=True, exist_ok=True)
        destination = _owned_path(root, folder / LOCAL_INSTALLER_NAME)
        metadata = _owned_path(root, folder / "release.json")
        if destination.is_file():
            try:
                verify_installer(destination, release, cancel_event=cancel_event)
                atomic_write_json(metadata, _cache_metadata(release))
                if progress is not None:
                    progress(release.size, release.size)
                return destination
            except UpdateCancelled:
                raise
            except Exception:
                _log(log, "Cached update did not verify; downloading a fresh copy.")
        descriptor, temporary = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=folder)
        part = Path(temporary)
        with os.fdopen(descriptor, "wb") as output:
            _owned_path(root, part)
            with _open_url(
                release.download_url, _asset_validator(channel.repository, release.tag, INSTALLER_ASSET_NAME, redirects=True),
                accept="application/octet-stream", cancel_event=cancel_event,
            ) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) != release.size:
                    raise UpdateError("Download Content-Length differs from the manifest.")
                received = 0
                while True:
                    _cancelled(cancel_event)
                    chunk = _read_chunk(response, CHUNK_BYTES)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > release.size:
                        raise UpdateError("Download exceeds the declared installer size.")
                    output.write(chunk)
                    if progress is not None:
                        progress(received, release.size)
                if received != release.size:
                    raise UpdateError("Installer download is incomplete.")
                output.flush()
                os.fsync(output.fileno())
        verify_installer(part, release, cancel_event=cancel_event)
        _cancelled(cancel_event)
        _owned_path(root, destination)
        _owned_path(root, metadata)
        os.replace(part, destination)
        part = None
        atomic_write_json(metadata, _cache_metadata(release))
        _log(log, f"PNG Mockup {release.version} update downloaded and verified.")
        return destination
    except Exception as exc:
        _log(log, f"Background update download skipped ({type(exc).__name__}).")
        return None
    finally:
        if part is not None:
            try:
                _owned_path(root, part).unlink(missing_ok=True)
            except (OSError, UpdateError):
                pass


def launch_updater(
    installer: Path, app_root: Path = APP_ROOT, *, wait_pid: int, relaunch: bool = True,
) -> subprocess.Popen[Any]:
    """Launch only a verified private cached setup after the UI's idle approval.

    The setup waits for the GUI PID and the launcher's session lock. This function
    never kills or stops the GUI, any job, Photoshop, or another installation.
    """
    try:
        root = Path(app_root).resolve()
        channel = read_channel(root)
        if type(wait_pid) is not int or not 0 < wait_pid < 2**32 or type(relaunch) is not bool:
            raise UpdateError("A valid GUI process ID is required to install an update.")
        path = _owned_path(root, Path(installer))
        data = _read_json(_owned_path(root, path.parent / "release.json"))
        if (
            type(data.get("schema_version")) is not int or data["schema_version"] != 1
            or data.get("product_id") != PRODUCT_ID or not isinstance(data.get("release"), dict)
        ):
            raise UpdateError("The cached update metadata has a different identity.")
        release = ReleaseInfo(**data["release"])
        _validate_release(release, root, channel)
        if path != _cache_directory(root, release) / LOCAL_INSTALLER_NAME:
            raise UpdateError("Only the verified cached PNG Mockup setup can be launched.")
        report = _owned_path(root, path.parent / "install-result.json")
        if report.is_dir():
            raise UpdateError("The update result path conflicts with a directory.")
        verify_installer(path, release)
        command = [str(path), "/update", str(root), "/wait-pid", str(wait_pid)]
        if relaunch:
            command.append("/relaunch")
        command.extend(["/report", str(report)])
        return subprocess.Popen(
            command, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"Không thể chuẩn bị bộ cài PNG Mockup ({type(exc).__name__}).") from exc
