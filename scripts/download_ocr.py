"""按随附哈希清单下载可选 OCR 资源，基础文本演示无需执行。"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def main():
    """只下载清单内文件，验证长度和 SHA-256 后原子保存。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='https://261117.xyz/ocr-runtime/')
    parser.add_argument('--model', choices=('tiny', 'small', 'all'), default='tiny')
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if address.scheme != 'https' or not address.hostname or address.username or address.password or address.query or address.fragment:
        parser.error('资源地址必须是不带凭证、查询参数的 HTTPS 地址')
    root = Path(__file__).resolve().parents[1] / 'packages/ocr-runtime/public/ocr-runtime'
    manifest = json.loads((root / 'resource-manifest.json').read_text(encoding='utf-8'))
    selected = [item for item in manifest['files'] if '/models/' not in item['path']
                or args.model == 'all' or f'_{args.model}_' in item['path']]
    for item in selected:
        relative = PurePosixPath(item['path'])
        # 不允许清单中的路径逃离指定资源目录。
        if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'vendor' or '\\' in item['path']:
            raise ValueError('资源清单包含不安全路径')
        target = root.joinpath(*relative.parts)
        if target.is_file() and target.stat().st_size == item['bytes'] and hashlib.sha256(target.read_bytes()).hexdigest() == item['sha256']:
            print('已校验：', item['path'])
            continue
        for attempt in range(3):
            try:
                request = Request(args.base_url.rstrip('/') + '/' + item['path'], headers={'User-Agent': 'Zhixu-Source-OCR/1'})
                with urlopen(request, timeout=90) as response:
                    # 上界来自本地固定清单，避免错误响应无限占用内存。
                    content = response.read(item['bytes'] + 1)
                if len(content) != item['bytes'] or hashlib.sha256(content).hexdigest() != item['sha256']:
                    raise ValueError('资源长度或 SHA-256 不符：' + item['path'])
                break
            except (URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.part')
        temporary.write_bytes(content)
        temporary.replace(target)
        print('已下载并校验：', item['path'])
    print('OCR 资源准备完成；请重新运行 npm run build。')


if __name__ == '__main__':
    main()
