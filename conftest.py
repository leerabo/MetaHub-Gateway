import pytest


@pytest.fixture
def anyio_backend() -> str:
    """测试统一使用 asyncio 后端，避免本地未安装 trio 时出现无关失败。"""

    return "asyncio"
