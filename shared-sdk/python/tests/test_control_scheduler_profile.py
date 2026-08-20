from metahub_shared.runtime.config import (
    ConfigValue,
    _control_background_schedulers_enabled,
)


def test_control_background_schedulers_keep_the_product_default_enabled():
    values = {
        "METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED": ConfigValue("true", "default")
    }
    assert _control_background_schedulers_enabled(values, "dev") is True
    assert _control_background_schedulers_enabled(values, "test") is True
    assert _control_background_schedulers_enabled(values, "prod") is True


def test_control_background_schedulers_respect_an_explicit_local_override():
    explicit_values = {
        "METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED": ConfigValue("false", "env")
    }
    assert _control_background_schedulers_enabled(explicit_values, "dev") is False
    assert _control_background_schedulers_enabled(explicit_values, "test") is False
    assert _control_background_schedulers_enabled(explicit_values, "prod") is False
