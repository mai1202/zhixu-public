"""用无网络、只读根文件系统的容器处理用户明确指定的附件。"""
import base64
from io import BytesIO
import json
from pathlib import PurePosixPath
import queue
import shutil
import subprocess
import threading
import time
from uuid import UUID, uuid4
from flask import current_app
from werkzeug.datastructures import FileStorage
from ..errors import ApiProblem
from . import agent_material_service as materials

# 数据通过标准输入传入，容器不挂载宿主路径、数据库、Docker socket 或密钥。
RUNNER = r'''
import base64, contextlib, io, json, os, pathlib, sys, traceback
request=json.load(sys.stdin)
root=pathlib.Path('/tmp/work'); root.mkdir()
os.chdir(root)
for name,data in request['files'].items():
    (root/name).write_bytes(base64.b64decode(data))
class Log(io.TextIOBase):
    def __init__(self): self.parts=[]; self.size=0
    def write(self,text):
        self.size+=len(text)
        if self.size>100000: raise RuntimeError('日志超过上限')
        self.parts.append(text); return len(text)
log=Log(); error=None
try:
    with contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
        exec(compile(request['code'],'<用户授权的文件处理>','exec'),{'__name__':'__main__'})
except BaseException as failure:
    error=type(failure).__name__+': '+str(failure)[:1000]
outputs={}; total=0
for name in request['outputs']:
    path=root/name
    if not path.exists(): continue
    if path.is_symlink() or not path.is_file(): raise RuntimeError('输出必须是普通文件')
    data=path.read_bytes(); total+=len(data)
    if total>1048576: raise RuntimeError('输出文件总量超过1MB')
    outputs[name]=base64.b64encode(data).decode()
print(json.dumps({'stdout':''.join(log.parts),'error':error,'files':outputs},ensure_ascii=False))
'''


def _bounded_run(command, payload, name):
    """同时限制标准输出、错误输出和运行时间，超限后回收容器。"""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    messages = queue.Queue()

    def drain(stream, label):
        total = bytearray()
        try:
            while chunk := stream.read(8192):
                total.extend(chunk)
                if len(total) > 2_000_000:
                    messages.put(("overflow", b"")); return
            messages.put((label, bytes(total)))
        finally:
            stream.close()

    readers = [threading.Thread(target=drain, args=(process.stdout, "out"), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, "err"), daemon=True)]
    for reader in readers: reader.start()
    # 输入不足2MB，异步写入以免容器未启动时阻塞主请求的超时控制。
    def feed():
        try: process.stdin.write(payload); process.stdin.close()
        except (BrokenPipeError, OSError): pass
    writer = threading.Thread(target=feed, daemon=True); writer.start()
    deadline = time.monotonic() + 35
    results = {}
    try:
        while len(results) < 2:
            try: label, data = messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise ApiProblem("sandbox_timeout", "文件处理超过35秒，已停止。", 422) from None
            if label == "overflow": raise ApiProblem("sandbox_output_limit", "文件处理输出超过上限，已停止。", 422)
            results[label] = data
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode:
            raise ApiProblem("sandbox_failed", "隔离容器执行失败，请检查运行环境或调整处理代码。", 422)
        return results["out"]
    finally:
        if process.poll() is None: process.kill()
        process.wait(timeout=5)
        # CLI 被终止不一定代表后台容器退出，按本次随机名称再清理一次。
        try: subprocess.run([command[0], "rm", "-f", name], capture_output=True, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired): pass
        for reader in readers: reader.join(timeout=1)
        writer.join(timeout=1)


def run(user_id, attachment_ids, code, outputs):
    """仅装入本人附件；模型不能传入宿主路径、镜像或容器启动参数。"""
    if not current_app.config.get("FILE_SANDBOX_ENABLED", False):
        raise ApiProblem("sandbox_unavailable", "文件容器尚未启用，请由部署者配置隔离运行环境。", 503)
    executable = shutil.which("docker")
    if not executable: raise ApiProblem("sandbox_unavailable", "未安装容器运行时。", 503)
    if (not isinstance(code, str) or not 1 <= len(code) <= 30000 or not isinstance(attachment_ids, list)
            or len(attachment_ids) > 10 or not isinstance(outputs, list) or not 1 <= len(outputs) <= 10):
        raise ApiProblem("invalid_sandbox_input", "文件处理输入超出限制。", 422)
    if any(not isinstance(name, str) or not name or len(name) > 120 or PurePosixPath(name).name != name
            or "\\" in name or ":" in name or name in {".", ".."} for name in outputs) or len(set(outputs)) != len(outputs):
        raise ApiProblem("invalid_sandbox_output", "输出名称必须是独立文件名。", 422)
    files = {}; total = 0
    for identifier in attachment_ids:
        path, item = materials.material_file(user_id, str(UUID(identifier)))
        data = path.read_bytes(); total += len(data)
        if total > 1_000_000: raise ApiProblem("sandbox_input_limit", "单次处理的附件总量不能超过1MB。", 422)
        filename = f"{item.id}_{item.filename}"
        files[filename] = base64.b64encode(data).decode()
    name = "zhixu-file-" + uuid4().hex
    image = str(current_app.config.get("FILE_SANDBOX_IMAGE", "python:3.13-alpine"))
    command = [executable, "run", "--rm", "--pull=never", "--name", name, "--network=none", "--read-only",
        "--user", "65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=32",
        "--memory=128m", "--memory-swap=128m", "--cpus=0.5", "--ulimit", "fsize=2097152:2097152",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m,mode=1777", "-i", image, "python", "-I", "-c", RUNNER]
    raw = _bounded_run(command, json.dumps({"code": code, "files": files, "outputs": outputs}).encode(), name)
    try:
        result = json.loads(raw)
        if not isinstance(result.get("files"), dict) or not isinstance(result.get("stdout"), str): raise ValueError()
        if set(result["files"]) - set(outputs): raise ValueError()
        decoded = {key: base64.b64decode(value, validate=True) for key, value in result["files"].items()}
        if sum(map(len, decoded.values())) > 1_048_576: raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ApiProblem("invalid_sandbox_result", "容器返回结果无效，未保存文件。", 422) from None
    attachments = []
    for filename, data in decoded.items():
        item = materials.upload_material(user_id, FileStorage(stream=BytesIO(data), filename=filename))
        attachments.append({"id": item["id"], "filename": item["filename"], "download_href": f"/api/v1/agent/attachments/{item['id']}/download"})
    return {"stdout": result["stdout"][:100000], "error": str(result.get("error") or "")[:1000] or None, "attachments": attachments,
            "input_files": list(files)}
