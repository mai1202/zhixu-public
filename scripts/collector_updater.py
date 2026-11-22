"""独立更新辅助进程：等待旧GUI退出、切换版本并完成启动握手。"""
import argparse
import ctypes
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop-client'))
from notification_client.updater import apply_update


def main():
    """错误只展示更新结果，不输出凭证或本地消息。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--install-root', required=True)
    parser.add_argument('--staged-root', required=True)
    parser.add_argument('--data-dir', required=True)
    args = parser.parse_args()
    try:
        apply_update(args.install_root, args.staged_root, args.data_dir)
    except Exception as error:
        if os.name == 'nt':
            ctypes.windll.user32.MessageBoxW(None, '更新未完成：' + str(error) + '\n可重新打开原来的采集器入口。', '知序采集器更新', 0x10)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
