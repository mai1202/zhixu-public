"""IMAP 扩展协商：只声明应用名称和版本，不发送账号、系统路径或秘密。"""

import imaplib


def identify_client(connection: imaplib.IMAP4) -> None:
    """在服务器声明 ID 能力时使用 RFC 2971；163 在打开收件夹前需要此声明。"""
    if "ID" not in connection.capabilities:
        return
    # 使用标准库公开扩展接口；固定参数不含邮箱、授权码或环境变量。
    status, _ = connection.xatom("ID", '("name" "notification-management" "version" "0.1.0")')
    if status != "OK":
        # 不传播可能带账号信息的服务商原始响应。
        raise imaplib.IMAP4.error("imap_client_id_failed")
