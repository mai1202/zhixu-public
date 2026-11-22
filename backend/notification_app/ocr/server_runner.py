from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
from pathlib import Path
from threading import RLock, Thread
from typing import Any
from urllib.parse import urlsplit, unquote

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright


class _StaticHandler(SimpleHTTPRequestHandler):
    """只提供固定 OCR bundle 与模型文件的本机静态服务。"""

    def log_message(self, format: str, *args: Any) -> None:
        # 运行日志不包含请求路径中的任何用户数据。
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


class OcrRunnerError(RuntimeError):
    """服务端 OCR 的受控错误，不泄露 Chromium 页面或用户数据。"""

    def __init__(self, code: str, *, retryable: bool = True):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class OcrRunner:
    """单浏览器、单页面固定 runner；运行期间只接收本地已验证图片字节。"""

    def __init__(self, *, timeout_seconds: int, executable_path: str | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.executable_path = executable_path
        self.lock = RLock()
        self.httpd: ThreadingHTTPServer | None = None
        self.http_thread: Thread | None = None
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def _start(self) -> None:
        if self.page is not None:
            return
        # 运行目录只指向项目内固定资源，不从通知或图片内容解析路径。
        project_root = Path(__file__).resolve().parents[3]
        public_root = project_root / "packages" / "ocr-runtime" / "public"
        if not (public_root / "ocr-runtime" / "runner.html").is_file():
            raise OcrRunnerError("ocr_assets_missing", retryable=False)

        # 缺少可执行文件是确定的部署问题，不应让每张图片重复尝试三次。
        if self.executable_path and not Path(self.executable_path).is_file():
            raise OcrRunnerError("ocr_browser_missing", retryable=False)
        self.playwright = sync_playwright().start()
        executable = self.executable_path or self.playwright.chromium.executable_path
        if not Path(executable).is_file():
            raise OcrRunnerError("ocr_browser_missing", retryable=False)

        handler = partial(_StaticHandler, directory=str(public_root))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.http_thread = Thread(target=self.httpd.serve_forever, name="ocr-static-runner", daemon=True)
        self.http_thread.start()

        launch_options: dict[str, Any] = {"headless": True}
        if self.executable_path:
            launch_options["executable_path"] = self.executable_path
        # 保留 Chromium 沙箱默认值；不使用 --no-sandbox 或用户传入的启动参数。
        self.browser = self.playwright.chromium.launch(**launch_options)
        self.context = self.browser.new_context(service_workers="block")
        origin = f"http://127.0.0.1:{self.httpd.server_port}"

        def allow_fixed_assets(route) -> None:
            """runner 只从固定资源目录响应，不允许外网、重定向或用户路径。"""
            request_url = route.request.url
            if request_url.startswith(f"{origin}/ocr-runtime/"):
                relative_path = unquote(urlsplit(request_url).path).removeprefix("/ocr-runtime/")
                resource_path = (public_root / "ocr-runtime" / relative_path).resolve()
                if not resource_path.is_relative_to((public_root / "ocr-runtime").resolve()) or not resource_path.is_file():
                    route.fulfill(status=404, body="not found")
                    return
                content_type = mimetypes.guess_type(resource_path.name)[0] or "application/octet-stream"
                route.fulfill(path=str(resource_path), content_type=content_type)
            else:
                route.abort()

        self.context.route("**/*", allow_fixed_assets)
        self.page = self.context.new_page()
        self.page.set_default_timeout(self.timeout_seconds * 1000)
        self.page.goto(f"{origin}/ocr-runtime/runner.html", wait_until="domcontentloaded")

    def recognize(self, image_bytes: bytes, *, filename: str, media_type: str) -> dict[str, Any]:
        """传入内存图片，不写入静态目录、不接受文件名作为路径。"""
        if filename not in {"upload.png", "upload.jpg"} or media_type not in {"image/png", "image/jpeg"}:
            raise OcrRunnerError("invalid_image_type")
        with self.lock:
            try:
                self._start()
                assert self.page is not None
                self.page.locator("#image").set_input_files(
                    {"name": filename, "mimeType": media_type, "buffer": image_bytes}
                )
                self.page.wait_for_function(
                    "window.__ocrResult !== null || window.__ocrError !== null",
                    timeout=self.timeout_seconds * 1000,
                )
                outcome = self.page.evaluate(
                    "({ result: window.__ocrResult, error: window.__ocrError })"
                )
                if not isinstance(outcome, dict) or not isinstance(outcome.get("result"), dict):
                    raise OcrRunnerError("ocr_runtime_failed")
                result = outcome["result"]
                # 结束任务后删除文件引用和文本框结果，保留静态模型缓存供下一任务复用。
                self.page.evaluate(
                    "() => { document.querySelector('#image').value = ''; window.__ocrResult = null; window.__ocrError = null; }"
                )
                return result
            except OcrRunnerError:
                # 初始化或执行失败后释放半成品资源，下一任务重新初始化。
                self.close()
                raise
            except Exception as error:
                self.close()
                raise OcrRunnerError("ocr_runtime_failed") from error

    def close(self) -> None:
        """进程退出时释放 Chromium、静态服务与任务页面。"""
        with self.lock:
            for item in (self.page, self.context, self.browser):
                try:
                    if item is not None:
                        item.close()
                except Exception:
                    pass
            self.page = None
            self.context = None
            self.browser = None
            if self.playwright is not None:
                self.playwright.stop()
                self.playwright = None
            if self.httpd is not None:
                self.httpd.shutdown()
                self.httpd.server_close()
                self.httpd = None
            self.http_thread = None
