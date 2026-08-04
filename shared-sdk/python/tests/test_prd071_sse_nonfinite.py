"""PRD-071 SSE 非有限 JSON 数字失败关闭测试。

测试只使用固定虚构正文，验证 Python 特有的宽松 JSON 行为不会突破跨语言协议边界，且异常对象不会
携带 Provider 原始事件或敏感字段。
"""

from __future__ import annotations

import pytest
from metahub_shared.output_safety import BoundedSseEventFramer, OutputSafetyError


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_framer_rejects_non_finite_json_constants_without_echoing_event(
    constant: str,
) -> None:
    """拒绝三类非标准浮点常量，并只暴露固定安全原因码。

    参数：Python ``json`` 默认接受的非有限数字 token。
    返回值：无；断言 framer 在正文投影前抛出 ``OutputSafetyError``。
    关键约束：异常和 repr 不得包含模型正文、非标准 token 或 Provider 原始事件。
    """

    sensitive_text = "prd071-nonfinite-provider-sensitive"
    raw_event = (
        'data: {"choices":[{"index":0,"delta":{"content":"'
        + sensitive_text
        + '"}}],"extension":'
        + constant
        + "}\n\n"
    ).encode("utf-8")
    framer = BoundedSseEventFramer()

    with pytest.raises(OutputSafetyError) as captured:
        framer.feed(raw_event)

    assert captured.value.reason_code == "output_safety.sse_json_invalid"
    assert sensitive_text not in repr(captured.value)
    assert constant not in repr(captured.value)


def test_framer_keeps_standard_finite_json_numbers_compatible() -> None:
    """确认有限整数、负小数和指数数字仍按标准 JSON 正常解析。

    参数：无，使用固定合法 SSE event。
    返回值：无；断言完整 frame 与模型文本投影保持不变。
    关键约束：非有限数字门禁不能误伤 Provider 的合法 usage 或扩展数值字段。
    """

    raw_event = (
        b'data: {"choices":[{"index":0,"delta":{"content":"safe"}}],'
        b'"usage":{"input_tokens":1,"cost":-1.25},"extension":1e3}\n\n'
    )

    frames = BoundedSseEventFramer().feed(raw_event)

    assert len(frames) == 1
    assert frames[0].raw_bytes == raw_event
    assert tuple(fragment.text for fragment in frames[0].fragments) == ("safe",)
