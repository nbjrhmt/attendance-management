"""应用入口与基础接口测试。

覆盖：

- ``GET /health`` 健康检查接口的响应格式
- ``GET /`` 演示前端页面（HTML）
- 未知路径下的统一响应格式（HTTP 404 + 统一响应体）

运行方式（项目根目录）::

    pytest -v
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

# 确保项目根目录在 sys.path 中，便于 pytest 直接从任意目录运行
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import app  # noqa: E402  需在调整 sys.path 之后导入

client = TestClient(app)


def test_health_check_returns_unified_success_response() -> None:
    """健康检查接口应返回统一格式且 data.status 为 ok。"""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "code": 0,
        "message": "success",
        "data": {"status": "ok"},
    }


def test_root_returns_demo_page() -> None:
    """根路径应返回演示前端页面（HTML）。"""
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "乡村基层活动智能签到管理平台" in response.text


def test_static_assets_served() -> None:
    """演示前端静态资源应可通过 /static 访问。"""
    for asset in ("/static/styles.css", "/static/app.js"):
        response = client.get(asset)
        assert response.status_code == 200
        assert len(response.content) > 100


def test_unknown_path_keeps_unified_response_format() -> None:
    """未知路径应保留 HTTP 404 状态码，且响应体为统一格式（提示已本地化）。"""
    response = client.get("/not-exist-path")
    body = response.json()

    assert response.status_code == 404
    assert body["code"] == 404
    assert body["message"] == "请求的接口或资源不存在"
    assert body["data"] == {}
