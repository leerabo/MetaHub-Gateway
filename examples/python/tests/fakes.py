"""为示例项目测试提供不依赖真实 ``openai`` 安装的最小假对象。"""

from __future__ import annotations

from typing import Any


class FakeAPIError(Exception):
    """模拟官方 ``APIError`` 基类并允许异常文本故意携带敏感内容。"""


class FakeAPIStatusError(FakeAPIError):
    """模拟带状态码、响应体和服务端请求标识的官方状态异常。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        request_id: str | None,
        body: Any,
    ) -> None:
        """保存测试所需公开属性，验证实现只提取白名单字段。

        ``message`` 与 ``body`` 可故意放入密钥和请求正文，测试应证明这些内容不会
        被示例错误格式化器写入 stderr。
        """

        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.body = body


class FakeAPITimeoutError(FakeAPIError):
    """模拟官方 ``APITimeoutError``，用于验证可重试且无 request_id 的分支。"""


class FakeAPIConnectionError(FakeAPIError):
    """模拟官方 ``APIConnectionError``，用于验证连接失败的安全输出。"""


class FakeCompletions:
    """记录 Chat Completions 参数，并返回响应或抛出预设异常。"""

    def __init__(self, *, response: Any = None, error: BaseException | None = None) -> None:
        """配置调用结果并初始化请求参数记录列表。

        测试通过 ``calls`` 核对 model、messages 和 ``stream=False`` 是否准确传给
        官方公开接口形状。
        """

        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        """记录一次公开 ``create`` 调用并按预设返回或抛错。

        抛错前不会改写异常，便于安全测试把敏感字符串放入异常后检查 CLI 是否
        错误地打印原始文本。
        """

        self.calls.append(kwargs)
        # 预设异常用于验证生产错误路径是否隐藏密钥、Prompt 和原始响应。
        if self.error is not None:
            raise self.error
        return self.response


class FakeEmbeddings:
    """记录 Embeddings 参数，并返回响应或抛出预设异常。"""

    def __init__(self, *, response: Any = None, error: BaseException | None = None) -> None:
        """配置向量调用结果并初始化参数记录列表。

        参数记录用于证明输入文本和模型别名通过官方 ``embeddings.create`` 公开
        接口传递，而不是走示例自建 HTTP 协议。
        """

        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        """记录一次向量请求并按测试预设返回或抛出异常。

        该方法只模拟官方公开调用形状，不包含网络、序列化或重试的自实现逻辑。
        """

        self.calls.append(kwargs)
        # 预设异常用于覆盖向量请求的安全失败输出，不能在假对象内改写异常。
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    """提供与示例访问路径一致的 ``chat`` 和 ``embeddings`` 资源。"""

    def __init__(self, completions: FakeCompletions, embeddings: FakeEmbeddings) -> None:
        """组合两个资源，并创建 ``client.chat.completions`` 兼容对象。

        这里使用动态对象仅模拟官方属性层级，业务断言仍落在两个有明确行为的假
        资源上。
        """

        self.chat = type("FakeChatResource", (), {"completions": completions})()
        self.embeddings = embeddings


class FakeOpenAIModule:
    """模拟官方顶层模块的公开客户端构造器与异常类型。"""

    APIError = FakeAPIError
    APIStatusError = FakeAPIStatusError
    APITimeoutError = FakeAPITimeoutError
    APIConnectionError = FakeAPIConnectionError

    def __init__(
        self,
        client: FakeClient | None,
        *,
        initialization_error: BaseException | None = None,
    ) -> None:
        """保存待返回客户端、可选初始化异常和构造参数记录。

        ``initialization_error`` 用于确认构造失败时不会泄露异常中的代理地址或
        Virtual Key。
        """

        self.client = client
        self.initialization_error = initialization_error
        self.constructor_calls: list[dict[str, Any]] = []

    def OpenAI(self, **kwargs: Any) -> FakeClient:
        """模拟官方 ``openai.OpenAI`` 构造器并记录公开参数。

        若配置了初始化异常则原样抛出；否则返回预设客户端。测试会确认生产代码
        把 Virtual Key 传入 ``api_key``，但后续输出不会包含该值。
        """

        self.constructor_calls.append(kwargs)
        # 初始化异常故意携带敏感文本，用于证明 CLI 不会打印原始异常。
        if self.initialization_error is not None:
            raise self.initialization_error
        # 未配置客户端属于测试装配错误，应立即失败而不是返回无效对象。
        if self.client is None:
            raise RuntimeError("fake client is not configured")
        return self.client
