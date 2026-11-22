"""Waitress 生产 WSGI 入口；Flask 内置服务器仅用于本机开发。"""

import sys

from . import create_app


def server_options(config: dict) -> dict:
    """使用本地回环默认值，仅在显式指定受信代理时采纳转发协议头。"""
    options = {
        "listen": str(config.get("SERVER_LISTEN", "127.0.0.1:8080")),
        "threads": int(config.get("SERVER_THREADS", 8)),
        "clear_untrusted_proxy_headers": True,
    }
    trusted_proxy = str(config.get("WAITRESS_TRUSTED_PROXY", "")).strip()
    if trusted_proxy:
        # 代理地址必须由部署者显式配置，不能盲信来自公网的 X-Forwarded 头。
        options["trusted_proxy"] = trusted_proxy
        options["trusted_proxy_headers"] = {"x-forwarded-proto"}
        options["trusted_proxy_count"] = 1
    return options


def main() -> int:
    """启动跨平台 Waitress；缺少部署依赖时给出可操作提示。"""
    try:
        from waitress import serve
    except ImportError:
        print("缺少 Waitress，请在 backend 目录运行 uv sync --locked。", file=sys.stderr)
        return 2
    app = create_app()
    options = server_options(app.config)
    serve(app, **options)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
