"""发现并描述 Windows 采集客户端安装包，供设备页展示下载入口。"""

from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from flask import current_app

_HASH_CACHE: dict[str, tuple[int, int, str]] = {}


def _download_dir() -> Path:
    return Path(str(current_app.config["CLIENT_DOWNLOAD_DIR"])).expanduser()


def client_download_path() -> Path | None:
    """按显式文件名、稳定文件名、最新时间戳包依次查找。"""
    directory = _download_dir()
    explicit = str(current_app.config.get("CLIENT_DOWNLOAD_FILENAME") or "").strip()
    if explicit:
        candidate = directory / explicit
        return candidate if candidate.is_file() else None
    stable = directory / "notification-collector-windows.zip"
    if stable.is_file():
        return stable
    candidates = sorted(
        (path for path in directory.glob("notification-collector-*.zip") if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _file_sha256(path: Path) -> str:
    stat = path.stat()
    cache_key = str(path.resolve())
    cached = _HASH_CACHE.get(cache_key)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    _HASH_CACHE[cache_key] = (stat.st_mtime_ns, stat.st_size, value)
    return value


def client_download_info() -> dict[str, object]:
    """返回页面所需元数据；不暴露服务器绝对路径或任何凭证。"""
    external_url = str(current_app.config.get("CLIENT_DOWNLOAD_URL") or "").strip()
    if external_url:
        parsed = urlsplit(external_url)
        relative_path = external_url.startswith("/") and not external_url.startswith("//")
        if not relative_path and (parsed.scheme not in {"http", "https"} or not parsed.netloc):
            current_app.logger.warning("CLIENT_DOWNLOAD_URL 不是有效的 HTTP(S) 地址，已忽略")
            external_url = ""
    if external_url:
        return {
            "available": True,
            "url": external_url,
            "filename": external_url.rstrip("/").rsplit("/", 1)[-1] or None,
            "size_bytes": None,
            "sha256": None,
            "updated_at_epoch": None,
            "external": True,
        }
    path = client_download_path()
    if path is None:
        return {
            "available": False,
            "url": None,
            "filename": None,
            "size_bytes": None,
            "sha256": None,
            "updated_at_epoch": None,
            "external": False,
        }
    stat = path.stat()
    return {
        "available": True,
        "url": "/downloads/client",
        "filename": path.name,
        "size_bytes": stat.st_size,
        "sha256": _file_sha256(path),
        "updated_at_epoch": int(stat.st_mtime),
        "external": False,
    }

_RELEASE_CACHE = {}


def client_update_info():
    """从实际发布 ZIP 读取版本，避免版本清单与正在下载的包不一致。"""
    import json
    import re
    import zipfile
    info = client_download_info()
    result = {**info, 'schema_version': 1, 'update_supported': False, 'version': None,
        'built_at_epoch': None, 'notes': []}
    path = client_download_path()
    if not info['available'] or info['external'] or path is None:
        return result
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    manifest = _RELEASE_CACHE.get(key)
    if manifest is None:
        try:
            with zipfile.ZipFile(path) as archive:
                entry = archive.getinfo('release.json')
                if entry.file_size > 65536:
                    return result
                manifest = json.loads(archive.read(entry).decode('utf-8'))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, UnicodeError):
            return result
        # 只缓存当前包，替换发布包后不沿用旧版本信息。
        _RELEASE_CACHE.clear()
        _RELEASE_CACHE[key] = manifest
    if not isinstance(manifest, dict):
        return result
    version, built = manifest.get('version'), manifest.get('built_at_epoch')
    if (manifest.get('kind') != 'collector' or manifest.get('update_protocol') != 1
            or not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version)
            or type(built) is not int or built < 1):
        return result
    result.update(update_supported=True, version=version, built_at_epoch=built,
        notes=[str(note)[:500] for note in manifest.get('notes', [])[:10]] if isinstance(manifest.get('notes'), list) else [])
    return result
