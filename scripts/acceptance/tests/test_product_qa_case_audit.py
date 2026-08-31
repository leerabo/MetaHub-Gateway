from scripts.quality.product_qa_case_audit import _node_test_output_clean


def test_node_test_output_accepts_growth_above_minimum() -> None:
    output = "\n".join(("tests 17", "pass 17", "fail 0", "skipped 0", "todo 0"))

    assert _node_test_output_clean(output, minimum_tests=16) is True


def test_node_test_output_rejects_incomplete_or_failed_runs() -> None:
    assert (
        _node_test_output_clean(
            "\n".join(("pass 15", "fail 0", "skipped 0", "todo 0")),
            minimum_tests=16,
        )
        is False
    )
    assert (
        _node_test_output_clean(
            "\n".join(("pass 17", "fail 1", "skipped 0", "todo 0")),
            minimum_tests=16,
        )
        is False
    )
