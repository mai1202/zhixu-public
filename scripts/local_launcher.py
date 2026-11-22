"""Windows 本机图形启动器：配置加密、备份迁移、构建和 API/worker 生命周期。"""

from __future__ import annotations

import argparse
import hashlib
import hmac
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import smtplib
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
# 发布包携带运行时，源码版复用项目虚拟环境；不依赖用户 PATH 中的 Python。
def backend_python(root: Path = ROOT) -> Path:
    """源码版使用虚拟环境对应的基础解释器，避免子进程落回受限的 venv 启动器。"""
    bundled = root / 'runtime/python/python.exe'
    if bundled.is_file():
        return bundled
    venv = root / 'backend/.venv'
    configuration = venv / 'pyvenv.cfg'
    if configuration.is_file():
        for line in configuration.read_text(encoding='utf-8-sig').splitlines():
            key, separator, value = line.partition('=')
            if separator and key.strip().lower() == 'home':
                base = Path(value.strip()) / 'python.exe'
                if base.is_file():
                    return base
    return venv / 'Scripts/python.exe'


def backend_environment(root: Path = ROOT) -> dict[str, str]:
    """基础解释器显式加载项目依赖，所有后端子进程共用相同模块搜索路径。"""
    environment = os.environ.copy()
    environment.pop('PYTHONHOME', None)
    paths = [str(root / 'backend')]
    if not (root / 'runtime/python/python.exe').is_file():
        paths.append(str(root / 'backend/.venv/Lib/site-packages'))
    environment['PYTHONPATH'] = os.pathsep.join(paths)
    environment['PYTHONUTF8'] = '1'
    return environment


sys.path.insert(0, str(ROOT / "desktop-client"))
from notification_client.secure_store import WindowsDpapiProtector


def wait_for_local_port(port: int, process: subprocess.Popen, timeout_seconds: float = 5.0) -> bool:
    """等待启动器创建的本机辅助进程监听端口，避免浏览器先打开得到连接拒绝。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


class LocalService:
    """只管理本启动器创建的进程，不根据端口或不可信 PID 杀死其他服务。"""

    def __init__(self, report=print):
        self.report = report
        self.processes: list[subprocess.Popen] = []
        self.log = None

    def run_step(self, title: str, command: list[str], **options):
        """给失败步骤提供可操作提示，不把命令、凭证或捕获的输出回显到窗口。"""
        try:
            return subprocess.run(command, check=True, **options)
        except subprocess.CalledProcessError as error:
            log_hint = f'\n详细日志：{self.log.name}' if self.log else ''
            raise RuntimeError(f'{title}失败（退出码 {error.returncode}）。{log_hint}') from None

    def stop(self):
        """关闭窗口时停止 API 和 worker；先停止后台任务，再关闭接口。"""
        if getattr(self, 'pi_url', None):
            try:
                # 先让 Pi 中止流与工具并保存轨迹，再回收本启动器的进程。
                request = Request(self.pi_url + '/shutdown', data=b'{}', headers={'Authorization': 'Bearer ' + self.pi_token, 'Content-Type': 'application/json'})
                with urlopen(request, timeout=8):
                    pass
                self.pi_url = None
            except OSError:
                pass
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        self.processes.clear()
        if self.log:
            self.log.close()
            self.log = None

    def start(self, data_dir: Path, settings: dict, port: int):
        """先校验端口并初始化数据；失败会清理已启动的子进程。"""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port + 1))
        data_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = data_dir / "private/runtime-temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        if not backend_python().is_file():
            self.report("正在安装后端依赖……")
            uv = shutil.which("uv")
            if not uv:
                raise RuntimeError("尚未安装 uv。请先按 README 安装源码依赖；完整运行时安装包尚未提供。")
            self.run_step('安装后端依赖', [uv, "sync", "--locked", "--link-mode", "copy", "--project", str(ROOT / "backend")], capture_output=True)
        self.python = backend_python()
        self.environment = backend_environment()
        config_path = data_dir / "local-config.dpapi"
        database = data_dir / "notification_management.sqlite3"
        if config_path.exists():
            config = json.loads(WindowsDpapiProtector().unprotect(config_path.read_bytes()))
        else:
            # 旧库不能配上新密钥，防止原有模型密文被误认为损坏。
            if database.exists():
                raise RuntimeError("此目录已有数据库但没有启动器配置。请使用原启动方式，或选择新的空目录；不要删除原数据库。")
            generated = self.run_step('初始化本机配置', [str(self.python), "-c", "import json,secrets; from cryptography.fernet import Fernet; print(json.dumps({'AUTH_HMAC_KEY':secrets.token_urlsafe(48),'LLM_ENCRYPTION_KEY':Fernet.generate_key().decode()}))"], env=self.environment, capture_output=True, text=True)
            config = json.loads(generated.stdout)
        config.update(settings)
        config.update(APP_ENV="development", DATA_DIR=str(data_dir), DATABASE_URL="sqlite:///" + database.as_posix(),
                      SERVER_LISTEN=f"127.0.0.1:{port}", SESSION_COOKIE_SECURE="false", TRUSTED_ORIGINS=f"http://127.0.0.1:{port}",
                      PI_AGENT_URL=f"http://127.0.0.1:{port + 1}", PI_CALLBACK_URL=f"http://127.0.0.1:{port}", PI_AGENT_PORT=str(port + 1))
        encrypted = WindowsDpapiProtector().protect(json.dumps(config).encode("utf-8"))
        staged = config_path.with_suffix('.part')
        staged.write_bytes(encrypted)
        os.replace(staged, config_path)
        # 环境仅传给当前子进程，不写用户或系统级环境变量。
        environment = self.environment
        for name in ("SMTP_AUTH_SECRET", "GRAPH_REFRESH_TOKEN", "GRAPH_CLIENT_SECRET", "OCR_CHROMIUM_EXECUTABLE"):
            environment.pop(name, None)
        environment.update({name: str(value) for name, value in config.items()})
        environment.update(TEMP=str(temp_dir), TMP=str(temp_dir), PYTHONUTF8="1")
        environment['PI_BRIDGE_TOKEN'] = hmac.new(str(config['AUTH_HMAC_KEY']).encode(), b'notification-pi-bridge-v1', hashlib.sha256).hexdigest()
        self.pi_url, self.pi_token = config['PI_AGENT_URL'], environment['PI_BRIDGE_TOKEN']
        self.log = (data_dir / "local-service.log").open('a', encoding='utf-8')
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            if database.exists():
                self.report("正在备份已有数据……")
                backup_dir = data_dir / "backups"
                backup_dir.mkdir(exist_ok=True)
                backup = backup_dir / f"before-start-{time.time_ns()}.sqlite3"
                with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(backup)) as target:
                    source.backup(target)
                # Pi 原始轨迹和私有附件与数据库一并保留，不把摘要当作唯一备份。
                private_backup = backup_dir / backup.stem
                for folder in ('pi', 'agent', 'ocr'):
                    source = data_dir / 'private' / folder
                    if source.exists():
                        shutil.copytree(source, private_backup / 'private' / folder)
            self.report("正在检查数据库版本……")
            self.run_step('检查数据库版本', [str(self.python), '-m', 'alembic', 'upgrade', 'head'], cwd=ROOT / 'backend', env=environment,
                          stdout=self.log, stderr=self.log, creationflags=flags)
            if config.get('INSTALL_OCR_BROWSER') == 'true':
                # 仅在用户明确勾选时下载可选组件，安装日志留在个人数据目录。
                self.report('正在检查或安装服务端 OCR 浏览器……')
                self.run_step('安装 OCR 浏览器', [str(self.python), '-m', 'playwright', 'install', 'chromium'], env=environment,
                              stdout=self.log, stderr=self.log, creationflags=flags)
            built = ROOT / 'frontend/dist/index.html'
            sources = list((ROOT / 'frontend/src').rglob('*')) + list((ROOT / 'packages/ocr-runtime/src').rglob('*'))
            needs_build = not built.exists() or any(path.is_file() and path.stat().st_mtime > built.stat().st_mtime for path in sources)
            if needs_build:
                self.report("正在构建网页……")
                if not shutil.which('npm'):
                    raise RuntimeError("网页尚未构建，请先安装 Node.js 并运行安装脚本；发行包应包含已构建网页。")
                if not (ROOT / 'node_modules/vite/bin/vite.js').is_file():
                    # 源码首次启动按锁文件安装，不要求用户另开终端执行 npm ci。
                    self.report('正在安装网页依赖……')
                    self.run_step('安装网页依赖', ['cmd.exe', '/d', '/c', 'npm', 'ci', '--ignore-scripts'], cwd=ROOT, env=environment,
                                  stdout=self.log, stderr=self.log, creationflags=flags)
                self.run_step('构建网页', ['cmd.exe', '/d', '/c', 'npm', 'run', 'build'], cwd=ROOT, env=environment,
                              stdout=self.log, stderr=self.log, creationflags=flags)
            node = ROOT / 'runtime/node/node.exe'
            if not node.is_file():
                found_node = shutil.which('node')
                if not found_node:
                    raise RuntimeError('未找到 Node.js，请使用包含运行时的普通用户发布包。')
                node = Path(found_node)
            if not (ROOT / 'node_modules/@earendil-works/pi-coding-agent/dist/index.js').is_file():
                if not shutil.which('npm'):
                    raise RuntimeError('Pi 依赖不完整，请重新解压完整发布包。')
                self.report('正在按锁文件安装 Pi 依赖……')
                self.run_step('安装 Pi 依赖', ['cmd.exe', '/d', '/c', 'npm', 'ci', '--ignore-scripts'], cwd=ROOT, env=environment, stdout=self.log, stderr=self.log, creationflags=flags)
            self.report("正在启动 Pi 智能助手、网页服务和后台任务……")
            self.processes.append(subprocess.Popen([str(node), str(ROOT / 'agent-service/src/server.mjs')], cwd=ROOT, env=environment, stdout=self.log, stderr=self.log, creationflags=flags))
            for module in ('notification_app.server', 'notification_app.jobs.worker'):
                self.processes.append(subprocess.Popen([str(self.python), '-m', module], cwd=ROOT / 'backend', env=environment,
                                                       stdout=self.log, stderr=self.log, creationflags=flags))
            url = f'http://127.0.0.1:{port}'
            api_ready = False
            for _ in range(80):
                if any(process.poll() is not None for process in self.processes):
                    raise RuntimeError("服务提前退出，请查看数据目录中的 local-service.log。")
                try:
                    with urlopen(url + '/api/v1/health', timeout=1) as response:
                        if json.load(response).get('status') == 'ok':
                            api_ready = True
                            break
                except (OSError, ValueError):
                    time.sleep(0.25)
            if not api_ready:
                raise RuntimeError("等待网页服务启动超时，请查看数据目录中的 local-service.log。")
            # Pi 单独监听下一端口；API 先就绪时仍要等待助手健康，避免用户进入后立即遇到连接错误。
            for _ in range(80):
                if any(process.poll() is not None for process in self.processes):
                    raise RuntimeError("服务提前退出，请查看数据目录中的 local-service.log。")
                try:
                    request = Request(
                        self.pi_url + '/health',
                        headers={'Authorization': 'Bearer ' + self.pi_token},
                    )
                    with urlopen(request, timeout=1) as response:
                        if json.load(response).get('status') == 'ok':
                            return url
                except (OSError, ValueError):
                    time.sleep(0.25)
            raise RuntimeError("等待 Pi 助手启动超时，请查看数据目录中的 local-service.log。")
        except Exception:
            self.stop()
            raise


def main():
    """图形设置仅面向本机用户；不开放免登录网络配置接口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--start', action='store_true', help='恢复已有本机配置后自动启动服务')
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    if args.smoke:
        if args.data_dir is None:
            parser.error('烟测必须指定独立空数据目录')
        service = LocalService()
        try:
            url = service.start(args.data_dir.resolve(), {'MAIL_TRANSPORT': 'disabled'}, 18125)
            print('Launcher smoke OK:', url)
        finally:
            service.stop()
        return

    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    window = tk.Tk()
    window.title('通知管理 · 本机启动')
    window.geometry('700x680')
    window.minsize(580, 640)
    frame = ttk.Frame(window, padding=20)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='配置一次，以后点击启动即可使用', font=('', 16)).pack(anchor='w')
    ttk.Label(frame, text='若学校已提供网站，直接在浏览器登录，无需本机部署。\n本机模式需要运营邮箱发验证码；请使用邮箱授权码。').pack(anchor='w', pady=8)
    default_dir = Path(os.environ.get('LOCALAPPDATA', ROOT / 'data')) / 'NotificationManagement'
    data = tk.StringVar(value=str(args.data_dir or default_dir))
    port = tk.StringVar(value='8080')
    demo_mode = tk.BooleanVar(value=False)
    email = tk.StringVar()
    host = tk.StringVar(value='smtp.163.com')
    smtp_port = tk.StringVar(value='465')
    secret = tk.StringVar()
    install_ocr = tk.BooleanVar(value=False)
    status = tk.StringVar(value='尚未启动。配置和密钥加密绑定当前 Windows 用户。')
    service = LocalService(lambda text: window.after(0, status.set, text))
    loading = False

    def field(label, value, password=False):
        """输入框按含义标注，不要求用户填写环境变量。"""
        ttk.Label(frame, text=label).pack(anchor='w', pady=(5, 1))
        entry = ttk.Entry(frame, textvariable=value, show='*' if password else '')
        entry.pack(fill='x')

    def load_saved():
        """重开后只显示非敏感设置，授权码留空表示沿用已加密值。"""
        path = Path(data.get()).expanduser() / 'local-config.dpapi'
        if not path.exists():
            return {}
        config = json.loads(WindowsDpapiProtector().unprotect(path.read_bytes()))
        return config

    field('个人数据目录（升级保留，请不要选择项目原有数据库目录）', data)
    ttk.Button(frame, text='选择目录', command=lambda: data.set(filedialog.askdirectory() or data.get())).pack(anchor='e')
    field('网页端口（默认 8080）', port)
    ttk.Checkbutton(
        frame,
        text='比赛演示模式（无需 SMTP，自动创建合成账户并登录）',
        variable=demo_mode,
    ).pack(anchor='w', pady=6)
    field('运营发件邮箱', email)
    field('SMTP 服务器（163 邮箱为 smtp.163.com）', host)
    field('SSL 端口（通常 465；本机 163 可测试 994）', smtp_port)
    field('邮箱授权码（首次必填；已有配置留空表示沿用）', secret, True)
    ttk.Checkbutton(frame, text='安装服务端 OCR 浏览器（可选，需下载组件）', variable=install_ocr).pack(anchor='w', pady=5)
    try:
        saved = load_saved()
        email.set(saved.get('SMTP_AUTH_USER', ''))
        host.set(saved.get('SMTP_HOST', 'smtp.163.com'))
        smtp_port.set(str(saved.get('SMTP_PORT', 465)))
        port.set(str(saved.get('SERVER_LISTEN', '127.0.0.1:8080')).rsplit(':', 1)[-1])
    except Exception:
        status.set('已有配置无法解密，请切回原 Windows 用户，或选择新的空目录。')

    def start():
        """后台启动避免窗口假死；只记录受控错误类别，不回显凭证。"""
        nonlocal loading
        if loading or service.processes:
            return
        try:
            settings = load_saved()
            if not 1 <= int(port.get()) <= 65534 or not 1 <= int(smtp_port.get()) <= 65535:
                raise ValueError('端口必须在 1 到 65535 之间。')
            if demo_mode.get():
                # 比赛模式只使用固定合成账户，绝不绕过真实学生登录或假装邮件已发送。
                settings.update(MAIL_TRANSPORT='disabled')
            else:
                authorization = secret.get() or settings.get('SMTP_AUTH_SECRET', '')
                if not email.get().strip() or not authorization:
                    raise ValueError('请先填写运营邮箱和授权码；应用使用它发送学生登录验证码。')
                settings.update(MAIL_TRANSPORT='smtp', SMTP_HOST=host.get().strip(), SMTP_PORT=smtp_port.get(),
                                SMTP_AUTH_USER=email.get().strip(), SMTP_FROM=email.get().strip(), SMTP_AUTH_SECRET=authorization,
                                SMTP_USE_SSL='true', SMTP_STARTTLS='false')
            settings['INSTALL_OCR_BROWSER'] = 'true' if install_ocr.get() else 'false'
            selected_dir, selected_port = Path(data.get()).expanduser().resolve(), int(port.get())
            demo_requested = demo_mode.get()
        except Exception as error:
            messagebox.showerror('设置需要检查', str(error) if isinstance(error, ValueError) else '配置无法读取，请检查数据目录和 Windows 用户。')
            return
        loading = True
        start_button.configure(state='disabled')

        def run():
            """子线程完成耗时步骤，所有界面回写交给 Tk 主线程。"""
            nonlocal loading
            try:
                url = service.start(selected_dir, settings, selected_port)
                opened_url = url
                if demo_requested:
                    login_port = selected_port + 2
                    if login_port > 65535:
                        login_port = selected_port - 2
                    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                    login_process = subprocess.Popen(
                        [
                            str(service.python),
                            str(ROOT / 'scripts/demo_login_server.py'),
                            '--reset-demo',
                            '--issue-session',
                            '--data-dir',
                            str(selected_dir),
                            '--port',
                            str(login_port),
                            '--target',
                            f'{url}/today',
                        ],
                        cwd=ROOT,
                        env=service.environment,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=flags,
                    )
                    service.processes.append(login_process)
                    if not wait_for_local_port(login_port, login_process):
                        raise RuntimeError('合成账户登录转发器未启动；请检查端口占用或查看本地日志。')
                    opened_url = f'http://127.0.0.1:{login_port}/demo-qa-login'
                window.after(
                    0,
                    status.set,
                    f'已启动：{url}\n'
                    + (f'比赛演示登录：{opened_url}\n' if demo_requested else '')
                    + '关闭此窗口会停止网页服务和后台任务。',
                )
                window.after(0, secret.set, '')
                webbrowser.open(opened_url)
            except Exception as error:
                service.stop()
                message = str(error) if isinstance(error, RuntimeError) else f'启动失败（{type(error).__name__}）。请检查端口、依赖和数据目录。'
                window.after(0, status.set, message)
                window.after(0, start_button.configure, {'state': 'normal'})
            finally:
                loading = False
        threading.Thread(target=run, daemon=True).start()

    def close():
        """启动期间避免关闭窗口留下半启动进程；运行后统一退出。"""
        if loading:
            status.set('正在完成启动，请稍候再退出。')
            return
        service.stop()
        window.destroy()

    def check_mail():
        """分开发信认证检查与邮件投递；诊断只显示分类，不回显授权码。"""
        nonlocal loading
        if loading:
            return
        try:
            saved = load_saved()
            username, hostname, mail_port = email.get().strip(), host.get().strip(), int(smtp_port.get())
            authorization = secret.get() or saved.get('SMTP_AUTH_SECRET', '')
            if not username or not hostname or not authorization or not 1 <= mail_port <= 65535:
                raise ValueError('请填写完整的邮箱、服务器、SSL 端口和授权码。')
        except Exception:
            status.set('请检查邮箱、SSL 端口和授权码，或确认原配置能够解密。')
            return
        loading = True
        status.set('正在检查加密连接与邮箱认证……')

        def check():
            """网络检查不占用图形主线程，也不发送测试邮件。"""
            nonlocal loading
            client = None
            try:
                client = smtplib.SMTP_SSL(hostname, mail_port, timeout=15, context=ssl.create_default_context())
                client.login(username, authorization)
                message = '加密连接及邮箱认证成功；尚未发送邮件。'
            except smtplib.SMTPAuthenticationError:
                message = '邮箱拒绝认证，请核对授权码并开启 SMTP 服务；不能使用网页登录密码。'
            except Exception:
                message = '无法建立加密连接，请检查服务器、SSL 端口和网络；可核对服务商的其他 SSL 端口。'
            finally:
                if client:
                    client.close()
                loading = False
            window.after(0, status.set, message)
        threading.Thread(target=check, daemon=True).start()

    def monitor():
        """任一受管进程退出时明确提示，并清理另一进程以便安全重启。"""
        if not loading and service.processes and any(process.poll() is not None for process in service.processes):
            service.stop()
            status.set('网页服务或后台任务已退出。请查看数据目录中的日志，检查后可以重新启动。')
            start_button.configure(state='normal')
        window.after(2000, monitor)

    ttk.Button(frame, text='检查发信通道（不发送邮件）', command=check_mail).pack(fill='x', pady=4)
    start_button = ttk.Button(frame, text='启动并打开网页', command=start)
    start_button.pack(fill='x', pady=10)
    ttk.Button(frame, text='停止并退出', command=close).pack(fill='x')
    ttk.Label(frame, textvariable=status, wraplength=610).pack(anchor='w', pady=10)
    window.protocol('WM_DELETE_WINDOW', close)
    window.after(2000, monitor)
    if args.start:
        window.after(100, start)
    window.mainloop()


if __name__ == '__main__':
    main()
