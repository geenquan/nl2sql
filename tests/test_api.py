from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_endpoint() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_milestones_endpoint() -> None:
    response = client.get("/api/v1/milestones")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 9
    assert data[0]["id"] == "M0"
    assert data[0]["status"] == "done"
    # M1 已通过单元测试与真实库集成验证标记完成；M2 起仍为 todo。
    assert data[1]["id"] == "M1"
    assert data[1]["status"] == "done"
    assert all(m["status"] == "todo" for m in data[2:])


# 数据源接口的行为（创建、连接测试、扫描、画像）由 tests/test_sources_api.py
# 通过依赖覆盖用内存实现验证；这里不再对真实网络发起请求。
