from flask import jsonify

from ..security import authenticated
from ..services import client_download_service
from . import api_bp


@api_bp.get("/client/download")
@authenticated()
def client_download_metadata():
    """设备页读取安装包是否可用、体积与摘要；实际文件走公开下载路由。"""
    return jsonify(client_download_service.client_download_info())


@api_bp.get('/client/update')
def client_update_manifest():
    """未配对采集器也可检查公开安装包版本，不暴露设备与账户信息。"""
    response = jsonify(client_download_service.client_update_info())
    response.headers['Cache-Control'] = 'no-store'
    return response
