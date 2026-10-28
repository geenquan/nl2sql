import pytest


@pytest.fixture(autouse=True)
def _reset_fake_connector():
    """每个测试前复位 tests.fakes.FakeConnector 的类级故障开关，避免跨测试泄漏。"""
    from tests.fakes import FakeConnector

    FakeConnector.fail_metadata_all = False
    FakeConnector.runtime_metadata_error = False
    FakeConnector.legacy_dropped = False
    FakeConnector.close_error = False
    FakeConnector.broken_fails = True
    yield
