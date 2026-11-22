"""可信附件索引转为有界传输内容；本地路径绝不进入上传数据。"""
from base64 import b64encode
from hashlib import md5, sha256
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024
FILE_SUFFIXES = {'.txt', '.md', '.csv', '.json', '.pdf', '.docx', '.xls', '.xlsx'}


def _image_suffix(data):
    if data.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    return None


def prepare_attachment(record, *, source_key, chat_id):
    """仅精确匹配的真实文件被读取，未下载和未知加密格式显式上报。"""
    result = {key: record[key] for key in ('attachment_key', 'filename', 'kind')}
    result.update(source_key=source_key, chat_id=chat_id)
    reason = record.get('unavailable_reason')
    if reason and reason != 'not_downloaded':
        return {**result, 'unavailable_reason': reason}
    try:
        root = Path(record['storage_root']).resolve()
        candidates = [record['path']] if record.get('path') else record.get('candidate_paths', [])
        path = None
        for candidate in candidates[:4]:
            checked = Path(candidate).resolve()
            if not checked.is_relative_to(root):
                return {**result, 'unavailable_reason': 'path_rejected'}
            if checked.is_file():
                path = checked
                break
        if path is None:
            return {**result, 'unavailable_reason': 'not_downloaded'}
        with path.open('rb') as handle:
            data = handle.read(MAX_BYTES + 1)
    except (KeyError, OSError, ValueError, TypeError):
        return {**result, 'unavailable_reason': 'not_downloaded'}
    if not data or len(data) > MAX_BYTES:
        return {**result, 'unavailable_reason': 'too_large'}
    suffix = Path(result['filename']).suffix.lower()
    if result['kind'] == 'image':
        suffix = _image_suffix(data)
        if suffix is None:
            # 只兼容可验证的单字节异或图片；未知新式加密不猜密钥或上传乱码。
            for magic in (0xff, 0x89):
                key = data[0] ^ magic
                decoded = bytes(value ^ key for value in data)
                suffix = _image_suffix(decoded)
                if suffix:
                    data = decoded
                    break
        if suffix is None:
            return {**result, 'unavailable_reason': 'unsupported_format'}
        result['filename'] = '图片' + suffix
    elif suffix not in FILE_SUFFIXES:
        return {**result, 'unavailable_reason': 'unsupported_format'}
    expected = record.get('content_md5')
    if not expected or md5(data).hexdigest() != expected:
        return {**result, 'unavailable_reason': 'hash_mismatch'}
    return {**result, 'data_base64': b64encode(data).decode('ascii'), 'content_hash': sha256(data).hexdigest()}
