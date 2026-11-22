"""本地演示会话的一次性登录转发器。

它只绑定回环地址，读取运维命令已生成的合成账户会话，并在一次请求后退出；
不提供账户选择、密码输入或长期监听能力。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import sys


def load_launcher_config(data_dir: Path) -> None:
    """从启动器数据目录读取 DPAPI 配置，避免发布包用户手工复制会话密钥。"""
    resolved = data_dir.expanduser().resolve()
    config_path = resolved / "local-config.dpapi"
    if not config_path.is_file():
        raise RuntimeError(f"未找到启动器配置：{config_path}")
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root / "desktop-client"))
    from notification_client.secure_store import WindowsDpapiProtector

    config = json.loads(WindowsDpapiProtector().unprotect(config_path.read_bytes()))
    database = resolved / "notification_management.sqlite3"
    # 仅在当前一次性进程内注入配置，不写环境变量、日志或其他磁盘文件。
    os.environ.update({str(key): str(value) for key, value in config.items()})
    os.environ["APP_ENV"] = "development"
    os.environ["DATA_DIR"] = str(resolved)
    os.environ["DATABASE_URL"] = "sqlite:///" + database.as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_json", type=Path, nargs="?")
    parser.add_argument(
        "--issue-session",
        action="store_true",
        help="从当前开发数据库为固定合成账户签发一次会话，不读取会话文件。",
    )
    parser.add_argument("--port", type=int, default=5175)
    parser.add_argument("--target", default="http://127.0.0.1:5000/today")
    parser.add_argument("--path", default="/demo-qa-login")
    parser.add_argument(
        "--reset-demo",
        action="store_true",
        help="只重建固定合成账户及演示数据；可与 --issue-session 同时使用。",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Windows 启动器使用的数据目录；用于直接读取 DPAPI 加密的本地配置。",
    )
    arguments = parser.parse_args()
    if arguments.data_dir is not None:
        load_launcher_config(arguments.data_dir)
    application = None
    backend_root = Path(__file__).resolve().parents[1] / "backend"
    sys.path.insert(0, str(backend_root))
    if arguments.reset_demo:
        # 从启动器配置读取同一数据库与密钥，避免发布包用户手工拼环境变量。
        from notification_app import create_app
        from notification_app.ops import DEMO_EMAIL, seed_demo_data

        application = create_app()
        with application.app_context():
            result = seed_demo_data(
                application.extensions["notification_db_engine"],
                Path(str(application.config["OCR_STORAGE_DIR"])),
            )
        print(
            json.dumps(
                {"demo_email": DEMO_EMAIL, **result},
                ensure_ascii=False,
                indent=2,
            )
        )
        if not arguments.issue_session:
            return 0
    if arguments.issue_session:
        # 只在服务端进程内签发并立即使用，不把 Cookie 写入命令行或普通日志。
        from notification_app.ops import issue_demo_session

        if application is None:
            from notification_app import create_app

            application = create_app()
        with application.app_context():
            session = issue_demo_session(
                application.extensions["notification_db_engine"],
                str(application.config.get("AUTH_HMAC_KEY") or ""),
                str(application.config["SESSION_COOKIE_NAME"]),
            )
    elif arguments.session_json is not None:
        session = json.loads(arguments.session_json.read_text(encoding="utf-8-sig"))
    else:
        parser.error("请提供 session_json，或使用 --issue-session。")
    expected_path = arguments.path if arguments.path.startswith("/") else f"/{arguments.path}"

    class LoginHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            # 只有已知的本地路径会发放一次会话，其他请求不会看到任何凭证。
            if self.path != expected_path:
                self.send_error(404)
                return
            self.send_response(303)
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Set-Cookie",
                f"{session['cookie_name']}={session['cookie_value']}; "
                "Path=/; HttpOnly; SameSite=Lax; Max-Age=3600",
            )
            self.send_header("Location", arguments.target)
            self.end_headers()
            self.server.should_stop = True  # type: ignore[attr-defined]

        def log_message(self, _format: str, *_args) -> None:
            # 不记录会话 Cookie、查询参数或目标页面。
            return

    with HTTPServer(("127.0.0.1", arguments.port), LoginHandler) as server:
        server.should_stop = False  # type: ignore[attr-defined]
        while not server.should_stop:  # type: ignore[attr-defined]
            server.handle_request()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
