"""用 Windows 原生通知区域图标管理本机采集器。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import threading
from typing import Any

from .api_client import ClientApiError
from .client_app import ClientApplication
from .local_queue import QueueFullError
from .wechat_cli import WechatCliError

WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_USER = 0x0400
WM_TRAY_CALLBACK = WM_USER + 20
WM_TRAY_NOTICE = WM_USER + 21
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
MF_STRING = 0x00000000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
ID_STATUS = 1001
ID_SYNC = 1002
ID_PAUSE = 1003
ID_EXIT = 1004


class _Point(ctypes.Structure):
    """Win32 屏幕坐标。"""

    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _Message(ctypes.Structure):
    """包含 Windows 新版 MSG 结构尾字段，避免 GetMessageW 覆写缓冲区。"""

    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", _Point),
        ("lPrivate", wintypes.DWORD),
    ]


class _Guid(ctypes.Structure):
    """通知区域图标的系统标识。"""

    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _NotifyIconData(ctypes.Structure):
    """Shell_NotifyIconW 所需的通知图标数据结构。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _Guid),
        ("hBalloonIcon", wintypes.HICON),
    ]


def run_tray(client: ClientApplication) -> None:
    """显示状态菜单并在后台线程执行同步，避免阻塞 Windows 消息循环。"""
    if os.name != "nt":
        raise RuntimeError("通知区域界面仅支持 Windows。")

    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 显式声明指针宽度相关签名，避免 64 位 Windows 上 HWND/HMENU 被截断。
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = wintypes.LPARAM
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.DestroyMenu.restype = wintypes.BOOL
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.LoadIconW.restype = wintypes.HICON
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(_Point)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
    user32.TrackPopupMenu.restype = wintypes.UINT
    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(_Message), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(_Message)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(_Message)]
    user32.DispatchMessageW.restype = wintypes.LPARAM
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostQuitMessage.restype = None
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.UnregisterClassW.restype = wintypes.BOOL
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NotifyIconData)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    hwnd_box: dict[str, int] = {}
    callback_box: dict[str, Any] = {}

    # 仅将错误类别和队列计数放入系统通知气泡，不显示原文或令牌。
    def show_notice(title: str, body: str) -> None:
        hwnd = hwnd_box.get("value")
        if hwnd:
            callback_box["notice"] = (title[:63], body[:255])
            user32.PostMessageW(hwnd, WM_TRAY_NOTICE, 0, 0)

    def do_sync() -> None:
        try:
            result = client.sync_once()
            if result.get("status") == "paused":
                show_notice("通知管理", "采集已暂停。")
            else:
                count = int(result.get("accepted", 0))
                failures = int(result.get("failed_batches", 0))
                show_notice("通知管理", f"本轮完成：接收 {count} 条，失败批次 {failures}。")
        except (ClientApiError, WechatCliError, QueueFullError, RuntimeError, OSError) as error:
            code = str(getattr(error, "code", "sync_failed"))[:60]
            show_notice("通知管理", f"同步未完成，错误代码：{code}。")

    def status_text() -> str:
        try:
            status = client.status()
            queue = status.get("queue", {})
            queued = int(queue.get("pending_batches", queue.get("queued", 0))) if isinstance(queue, dict) else 0
            return f"采集{'已暂停' if status.get('paused') else '运行中'}；待上传 {queued} 批。"
        except (ClientApiError, QueueFullError, RuntimeError, OSError):
            return "尚未配对或本机配置不可用。"

    def build_menu() -> int:
        menu = user32.CreatePopupMenu()
        if not menu:
            raise OSError(ctypes.get_last_error(), "无法创建通知区域菜单")
        paused = False
        try:
            paused = bool(client.config().get("paused"))
        except (RuntimeError, OSError):
            pass
        labels = (
            (ID_STATUS, "查看状态"),
            (ID_SYNC, "立即同步"),
            (ID_PAUSE, "恢复小时采集" if paused else "暂停小时采集"),
            (ID_EXIT, "退出"),
        )
        for command_id, label in labels:
            if not user32.AppendMenuW(menu, MF_STRING, command_id, label):
                user32.DestroyMenu(menu)
                raise OSError(ctypes.get_last_error(), "无法生成通知区域菜单")
        return int(menu)

    def execute_menu_command(command_id: int) -> None:
        if command_id == ID_STATUS:
            show_notice("通知管理", status_text())
        elif command_id == ID_SYNC:
            threading.Thread(target=do_sync, name="notification-client-sync", daemon=True).start()
        elif command_id == ID_PAUSE:
            try:
                paused = bool(client.config().get("paused"))
                client.pause(not paused)
                show_notice("通知管理", "小时采集已暂停。" if not paused else "小时采集已恢复。")
            except (RuntimeError, OSError):
                show_notice("通知管理", "本机配置不可用；请先在命令行配对设备。")
        elif command_id == ID_EXIT:
            hwnd = hwnd_box.get("value")
            if hwnd:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    def show_menu(hwnd: int) -> None:
        menu = build_menu()
        point = _Point()
        try:
            if not user32.GetCursorPos(ctypes.byref(point)):
                raise OSError(ctypes.get_last_error(), "无法读取指针位置")
            user32.SetForegroundWindow(hwnd)
            command_id = user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                hwnd,
                None,
            )
            if command_id:
                execute_menu_command(int(command_id))
            user32.PostMessageW(hwnd, 0, 0, 0)
        finally:
            user32.DestroyMenu(menu)

    def notify_icon(action: int, *, balloon: tuple[str, str] | None = None) -> bool:
        icon_data = _NotifyIconData()
        icon_data.cbSize = ctypes.sizeof(_NotifyIconData)
        icon_data.hWnd = hwnd_box["value"]
        icon_data.uID = 1
        icon_data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        icon_data.uCallbackMessage = WM_TRAY_CALLBACK
        icon_data.hIcon = user32.LoadIconW(None, ctypes.cast(32512, wintypes.LPCWSTR))
        icon_data.szTip = "通知管理采集器"
        if balloon is not None:
            icon_data.uFlags |= NIF_INFO
            icon_data.szInfoTitle, icon_data.szInfo = balloon
            icon_data.dwInfoFlags = NIIF_INFO
        return bool(shell32.Shell_NotifyIconW(action, ctypes.byref(icon_data)))

    @callback_type
    def window_proc(hwnd, message, w_param, l_param):
        if message == WM_TRAY_CALLBACK:
            if int(l_param) in {WM_RBUTTONUP, WM_CONTEXTMENU}:
                show_menu(hwnd)
            return 0
        if message == WM_TRAY_NOTICE:
            title, body = callback_box.pop("notice", ("通知管理", "操作完成。"))
            if not notify_icon(NIM_MODIFY, balloon=(title, body)):
                show_menu(hwnd)
            return 0
        if message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            notify_icon(NIM_DELETE)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, w_param, l_param)

    class_name = "NotificationManagementTrayWindow"
    instance = kernel32.GetModuleHandleW(None)
    window_class = type(
        "_WindowClass",
        (ctypes.Structure,),
        {
            "_fields_": [
                ("style", wintypes.UINT),
                ("lpfnWndProc", callback_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]
        },
    )
    definition = window_class()
    definition.lpfnWndProc = window_proc
    definition.hInstance = instance
    definition.lpszClassName = class_name
    atom = user32.RegisterClassW(ctypes.byref(definition))
    if not atom:
        raise OSError(ctypes.get_last_error(), "无法注册通知区域窗口")
    hwnd = user32.CreateWindowExW(0, class_name, "通知管理", 0, 0, 0, 0, 0, None, None, instance, None)
    if not hwnd:
        user32.UnregisterClassW(class_name, instance)
        raise OSError(ctypes.get_last_error(), "无法创建通知区域窗口")
    hwnd_box["value"] = int(hwnd)
    callback_box["window_proc"] = window_proc
    if not notify_icon(NIM_ADD):
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(class_name, instance)
        raise OSError(ctypes.get_last_error(), "无法创建通知区域图标")
    show_notice("通知管理", status_text())

    message = _Message()
    try:
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result == 0:
                break
            if result == -1:
                raise OSError(ctypes.get_last_error(), "通知区域消息循环失败")
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    finally:
        if hwnd_box.get("value"):
            notify_icon(NIM_DELETE)
        user32.UnregisterClassW(class_name, instance)
