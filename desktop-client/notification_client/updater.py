"""可信同源发布包检查、校验和旁路安装；更新不接触配对凭证及采集队列。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile

from . import __version__
from .api_client import COLLECTOR_USER_AGENT, validate_server_url

MAX_PACKAGE_BYTES = 300 * 1024 * 1024
MAX_EXPANDED_BYTES = 1200 * 1024 * 1024
REQUIRED_FILES = ('release.json', 'runtime/python/pythonw.exe', 'scripts/collector_launcher.py',
    'scripts/collector_updater.py', 'desktop-client/notification_client/gui.py')


def installation_root(program_root=None):
    """更新版本仍复用最初安装目录的稳定入口，避免自启路径层层嵌套。"""
    program = Path(program_root or Path(__file__).resolve().parents[2]).resolve()
    if program.parent.name == 'releases' and program.parent.parent.name == '.collector-updates':
        return program.parents[2]
    return program


def _manifest(root):
    """只读取非敏感程序版本清单。"""
    try:
        value = json.loads((Path(root) / 'release.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _version(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('更新版本格式无效。')
    return tuple(int(part) for part in value.split('.'))


def _origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


class _SameOriginRedirect(HTTPRedirectHandler):
    """拒绝重定向到其他站点或降级HTTP，不向任意地址下载可执行代码。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(req.full_url) != _origin(newurl):
            raise ValueError('更新下载不能重定向到其他服务。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url):
    """更新请求不携带登录 Cookie、设备令牌或本地聊天数据。"""
    handlers = [_SameOriginRedirect()]
    if urlsplit(url).hostname in {'localhost', '127.0.0.1', '::1'}:
        # 本地部署应直接访问回环，不经系统外部代理转发更新包。
        handlers.append(ProxyHandler({}))
    return build_opener(*handlers).open(Request(url,
        headers={'User-Agent': COLLECTOR_USER_AGENT, 'Accept': 'application/json, application/zip'}), timeout=30)


def check_update(server_url, *, program_root=None):
    """比较语义版本与同版本构建号，拒绝服务器提供的降级版本。"""
    server = validate_server_url(server_url)
    with _open(server + '/api/v1/client/update') as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('更新清单体积异常。')
    info = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(info, dict) or info.get('schema_version') != 1:
        raise ValueError('服务器暂不支持采集器更新，请从网页下载。')
    current = _manifest(program_root or Path(__file__).resolve().parents[2])
    info.update(current_version=current.get('version', __version__), current_built_at_epoch=current.get('built_at_epoch', 0))
    info['can_install'] = all((installation_root(program_root) / name).is_file() for name in REQUIRED_FILES)
    info['update_available'] = False
    if not info.get('available') or not info.get('update_supported'):
        return info
    latest, installed = _version(info.get('version')), _version(info['current_version'])
    built = info.get('built_at_epoch')
    if type(built) is not int or built < 1:
        raise ValueError('更新构建号无效。')
    url = urljoin(server + '/', str(info.get('url') or ''))
    if _origin(url) != _origin(server) or urlsplit(url).username or urlsplit(url).password:
        raise ValueError('更新包必须来自当前服务。')
    if not re.fullmatch(r'[0-9a-f]{64}', str(info.get('sha256', ''))):
        raise ValueError('更新包缺少有效SHA-256摘要。')
    size = info.get('size_bytes')
    if type(size) is not int or not 0 < size <= MAX_PACKAGE_BYTES:
        raise ValueError('更新包大小无效。')
    info['url'] = url
    info['update_available'] = latest > installed or (latest == installed and built > info['current_built_at_epoch'])
    return info


def _safe_entries(archive):
    """Windows大小写、设备名、软链接和路径穿越均在解压前拒绝。"""
    entries, seen, expanded = [], set(), 0
    for item in archive.infolist():
        name = item.filename
        parts = PurePosixPath(name).parts
        if (not parts or name.startswith('/') or '\\' in name or ':' in name
                or any(part in {'.', '..'} or part.rstrip(' .') != part for part in parts)
                or any(re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?', part) for part in parts)
                or (item.external_attr >> 16) & 0o170000 == 0o120000):
            raise ValueError('更新包包含不安全路径。')
        key = '/'.join(parts).casefold()
        if key in seen:
            raise ValueError('更新包包含重复路径。')
        seen.add(key)
        expanded += item.file_size
        entries.append(item)
    if len(entries) > 60000 or expanded > MAX_EXPANDED_BYTES:
        raise ValueError('更新包展开体积异常。')
    return entries, expanded


@contextmanager
def _download_workspace(staging):
    """继承安装目录权限，避免Windows私有临时目录ACL阻止版本目录切换。"""
    parent = staging.resolve()
    workspace = parent / ('download-' + os.urandom(12).hex())
    workspace.mkdir()
    if workspace.resolve().parent != parent:
        raise ValueError('更新临时路径异常。')
    try:
        yield workspace
    finally:
        # 只清理本次随机创建且已核验绝对范围的下载工作区。
        if workspace.resolve().parent == parent:
            shutil.rmtree(workspace)


def stage_update(info, *, install_root=None, progress=None):
    """先完整下载、校验再解压到新版本目录，当前运行程序保持完整。"""
    base = installation_root(install_root)
    if not info.get('update_available') or not info.get('can_install'):
        raise ValueError('当前运行方式不支持自动安装，请从网页下载完整采集器。')
    updates = base / '.collector-updates'
    staging, releases = updates / 'staging', updates / 'releases'
    for directory in (staging, releases):
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.resolve().is_relative_to(base):
            raise ValueError('更新目录必须位于当前安装目录内。')
    if shutil.disk_usage(base).free < info['size_bytes'] + 100 * 1024 * 1024:
        raise ValueError('安装目录磁盘空间不足，请至少保留更新包大小加100MB。')
    # 使用继承权限的工作区，不改动用户数据目录和旧安装。
    with _download_workspace(staging) as workspace:
        if not workspace.is_relative_to(staging.resolve()):
            raise ValueError('更新临时路径异常。')
        package = workspace / 'package.zip'
        digest, received = hashlib.sha256(), 0
        with _open(info['url']) as response, package.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > info['size_bytes'] or received > MAX_PACKAGE_BYTES:
                    raise ValueError('更新包大小与清单不符。')
                digest.update(chunk)
                output.write(chunk)
                if progress:
                    progress(received, info['size_bytes'])
        if received != info['size_bytes'] or digest.hexdigest() != info['sha256']:
            raise ValueError('更新包完整性校验失败，当前版本保持不变。')
        with zipfile.ZipFile(package) as archive:
            entries, expanded = _safe_entries(archive)
            if shutil.disk_usage(base).free < expanded + 50 * 1024 * 1024:
                raise ValueError('磁盘空间不足以展开更新包。')
            metadata = archive.getinfo('release.json')
            if metadata.file_size > 65536:
                raise ValueError('更新包版本清单异常。')
            manifest = json.loads(archive.read(metadata).decode('utf-8'))
            if (manifest.get('kind') != 'collector' or manifest.get('update_protocol') != 1
                    or manifest.get('version') != info['version'] or manifest.get('built_at_epoch') != info['built_at_epoch']):
                raise ValueError('更新包版本与清单不符，请重新检查更新。')
            # Windows可能暂时锁住新解压DLL，直接展开到唯一版本目录，避免整目录rename。
            target = releases / (info['version'] + '-' + str(info['built_at_epoch']) + '-' + os.urandom(6).hex())
            target.mkdir()
            try:
                archive.extractall(target, members=entries)
                if not all((target / name).is_file() for name in REQUIRED_FILES):
                    raise ValueError('更新包缺少启动所需文件。')
                (target / '.package-sha256').write_text(info['sha256'], encoding='utf-8')
            except Exception:
                # 尚未启用的目录仅由本次创建，核验范围后清理；旧版本和数据不受影响。
                if target.resolve().parent == releases.resolve():
                    shutil.rmtree(target, ignore_errors=True)
                raise
        return target


def _write_pointer(base, version_root):
    """只替换小型入口指针，不原地覆盖正在使用的Python DLL。"""
    root = Path(version_root).resolve()
    allowed = (base / '.collector-updates/releases').resolve()
    if root.parent != allowed or not all((root / name).is_file() for name in REQUIRED_FILES):
        raise ValueError('待启用的版本目录无效。')
    temporary = base / 'collector-active.json.tmp'
    temporary.write_text(json.dumps({'path': root.relative_to(base).as_posix()}, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, base / 'collector-active.json')


def launch_helper(staged_root, data_dir, *, install_root=None):
    """辅助进程等待GUI和采集锁释放，当前线程随后安全退出。"""
    base = installation_root(install_root)
    helper = Path(__file__).resolve().parents[2] / 'scripts/collector_updater.py'
    command = [sys.executable, str(helper), '--install-root', str(base),
        '--staged-root', str(staged_root), '--data-dir', str(Path(data_dir).resolve())]
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    return subprocess.Popen(command, creationflags=flags, close_fds=True)


def apply_update(base, staged_root, data_dir, *, start=None, timeout=60):
    """握手确认新GUI能启动；失败恢复旧指针并重新打开旧版，原始数据不变。"""
    from .client_app import SingleInstanceLock
    base, staged, data = Path(base).resolve(), Path(staged_root).resolve(), Path(data_dir).resolve()
    pointer = base / 'collector-active.json'
    previous = pointer.read_bytes() if pointer.is_file() else None
    status_dir = data / 'updates'
    status_dir.mkdir(parents=True, exist_ok=True)
    token = os.urandom(16).hex()
    ready = status_dir / (token + '.ready')
    def default_start(program, args):
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        return subprocess.Popen([str(program / 'runtime/python/pythonw.exe'),
            str(program / 'scripts/collector_launcher.py'), '--data-dir', str(data), *args], creationflags=flags)
    start = start or default_start
    with SingleInstanceLock(data / 'collector-update.lock') as update_lock:
        if not update_lock.acquired:
            raise RuntimeError('已有采集器更新正在进行。')
        until = time.monotonic() + timeout
        while True:
            with SingleInstanceLock(data / 'collector-gui.lock') as gui, SingleInstanceLock(data / 'collector.lock') as collector:
                if gui.acquired and collector.acquired:
                    _write_pointer(base, staged)
                    break
            if time.monotonic() >= until:
                raise RuntimeError('采集器尚未退出，已保留旧版本。请稍后重试更新。')
            time.sleep(0.25)
        process = None
        try:
            process = start(staged, ['--update-token', token])
            until = time.monotonic() + timeout
            while not ready.is_file():
                if process.poll() is not None or time.monotonic() >= until:
                    raise RuntimeError('新版启动未成功，已自动恢复旧版。')
                time.sleep(0.25)
            ready.unlink(missing_ok=True)
            (status_dir / 'last-result.json').write_text(json.dumps({'status': 'updated',
                'version': _manifest(staged).get('version'), 'pid': getattr(process, 'pid', None)}, ensure_ascii=False), encoding='utf-8')
        except Exception:
            # 仅结束本辅助进程启动且未完成握手的新GUI，不结束用户的其他Python进程。
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass  # 无论进程能否及时退出，都先恢复原来的稳定入口。
            if previous is None:
                pointer.unlink(missing_ok=True)
            else:
                temporary = pointer.with_suffix('.rollback')
                temporary.write_bytes(previous)
                os.replace(temporary, pointer)
            (status_dir / 'last-result.json').write_text('{"status":"rolled_back"}', encoding='utf-8')
            start(base, [])
            raise
