"""PRD-050 真实运行验收脚本的路径、生命周期、假上游和安全合同轻量门禁。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from metahub_shared.runtime.webhooks import webhook_signature_header

# 被测脚本文件名包含连字符，需要显式加载且不能触发命令行 main 分支。
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "prd050-runtime-acceptance.py"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "prd050_runtime_acceptance",
    SCRIPT_PATH,
)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# 动态模块必须先建立并登记稳定名称，避免被测 dataclass、traceback 或二次导入
# 解析成不同模块实例；加载只执行定义，不触发脚本的 main 命令行入口。
RUNTIME_MODULE = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RUNTIME_MODULE
MODULE_SPEC.loader.exec_module(RUNTIME_MODULE)

# 测试端独立冻结对象集合，避免脚本常量与探针夹具同时漏项后形成自证循环。
EXPECTED_TABLES = {
    "cache_affinities",
    "cache_affinity_scopes",
    "cache_affinity_bindings",
    "cache_affinity_selections",
    "cache_affinity_config_events",
}
EXPECTED_TRIGGERS = {
    "trg_cache_affinities_prd050_owner_insert",
    "trg_cache_affinity_scopes_prd050_pointer_update",
    "trg_cache_affinity_bindings_prd050_graph_insert",
    "trg_cache_affinity_bindings_prd050_transition_update",
    "trg_cache_affinity_selections_prd050_graph_insert",
    "trg_cache_affinity_selections_prd050_update",
    "trg_cache_affinity_config_events_prd050_graph_insert",
    "trg_cache_affinity_config_events_prd050_immutable_update",
}

# Browser 只消费这五个真实 Gateway request ID；测试端独立冻结名称与唯一性，防止
# Runtime/Browser 同时改名后静态测试仍自证通过。
RUNTIME_STATE_REQUEST_ID_FIELDS = {
    "cache_affinity_created",
    "cache_affinity_pending",
    "cache_affinity_warm",
    "cache_affinity_rebound",
    "short_legacy",
}


class FakeReceiverContext(AbstractContextManager[object]):
    """提供不监听端口的三哨兵接收器上下文，用于生命周期单测。"""

    def __init__(self, _long: str, _short: str, _response: str) -> None:
        """接受真实 server_factory 的三个敏感参数，但不保存其值。"""

        self.receiver = object()

    def __enter__(self) -> object:
        """返回无状态接收器占位对象。"""

        return self.receiver

    def __exit__(self, *_exc_info: object) -> None:
        """无端口、线程或文件资源，因此退出无需操作。"""


class FakeAcceptance:
    """模拟成功运行，用调用账本验证清理发生在 passed 状态落盘之前。"""

    instances: list[FakeAcceptance] = []

    def __init__(self, args: argparse.Namespace) -> None:
        """保存路径并提供与真实验收器相同的最小生命周期协议。"""

        self.args = args
        self.long_prompt = "unit-sensitive-long-prompt"
        self.short_prompt = "unit-sensitive-short-prompt"
        self.response_sentinel = "unit-sensitive-response"
        self.cleanup_calls = 0
        self.closed = False
        self.mutation_count = 9
        self.__class__.instances.append(self)

    def run(self, _receiver: object) -> dict[str, Any]:
        """返回不含任何哨兵的最小 PRD-050 成功状态。"""

        return {
            "schema_version": 1,
            "status": "passed",
            "prd_id": "PRD-050",
            "suffix": "testrun01",
            "database_file": self.args.database_path.name,
            "request_ids": {
                "cache_affinity_created": "req_prd050_unit_created",
                "cache_affinity_pending": "req_prd050_unit_pending",
                "cache_affinity_warm": "req_prd050_unit_warm",
                "cache_affinity_rebound": "req_prd050_unit_rebound",
                "short_legacy": "req_prd050_unit_short",
            },
            "checks": {
                "database_pending_selection_count": 0,
                "database_active_selection_count": 0,
            },
        }

    def cleanup_credentials(self) -> dict[str, Any]:
        """登记凭据和入口清理，并返回已验证的安全计数。"""

        self.cleanup_calls += 1
        return {"verified": True, "changed": 6}

    def sensitive_values(self) -> list[str]:
        """提供状态文件必须拒绝的三个内存哨兵。"""

        return [self.long_prompt, self.short_prompt, self.response_sentinel]

    def close(self) -> None:
        """登记连接和敏感引用已经释放。"""

        self.closed = True


class FailingAcceptance(FakeAcceptance):
    """模拟中途失败，证明异常路径仍清理且不写局部状态。"""

    def run(self, _receiver: object) -> dict[str, Any]:
        """抛出不携带 HTTP 正文、路径或凭据的固定异常。"""

        # 固定文本让测试只验证 finally 生命周期；不得把真实响应、Prompt 或凭据拼进异常，
        # 否则失败日志本身会破坏验收脚本承诺的敏感信息仅驻留内存边界。
        raise RuntimeError("fixed PRD-050 acceptance failure")


class LeakyAcceptance(FakeAcceptance):
    """返回带敏感哨兵的 passed 候选，验证首次脱敏失败绝不落盘。"""

    def run(self, receiver: object) -> dict[str, Any]:
        """在正常最小状态中注入已知 Prompt 哨兵，模拟状态组装回归。"""

        state = super().run(receiver)
        state["unsafe_echo"] = self.long_prompt
        return state


class UnverifiedCleanupAcceptance(FakeAcceptance):
    """模拟 cleanup 正常返回但明确报告未验证的失败关闭分支。"""

    def cleanup_credentials(self) -> dict[str, Any]:
        """登记调用并返回 verified=false，不能被写成 passed 生命周期。"""

        self.cleanup_calls += 1
        return {"verified": False, "changed": 0}


class FailingReceiverContext(FakeReceiverContext):
    """模拟 run 已返回 passed 后接收器退出失败的生命周期边界。"""

    def __exit__(self, *_exc_info: object) -> None:
        """固定抛错，证明 Context 退出成功是状态赋值前的硬门禁。"""

        # 这里刻意在 run 已返回 passed 后抛错，用来锁住“退出失败不得写状态”的时间边界，
        # 固定文本不含接收器 URL、Header 或任何敏感哨兵。
        raise RuntimeError("fixed PRD-050 receiver exit failure")


class AliasListControl:
    """返回可控 Alias 列表的最小 Control 客户端，用于租户泄露反例。"""

    def __init__(self, aliases: list[dict[str, Any]]) -> None:
        """保存调用方提供的 DTO 副本，不执行网络请求。"""

        self.aliases = [dict(item) for item in aliases]

    def get(self, _path: str, *, params: dict[str, str], headers: dict[str, str]) -> httpx.Response:
        """返回带统一链路 ID 的 200 列表响应。"""

        assert params
        request_id = headers["X-Request-ID"]
        return httpx.Response(
            200,
            json={
                "model_aliases": self.aliases,
                "request_id": request_id,
                "trace_id": request_id.replace("req_", "trc_", 1),
            },
        )


def _args(tmp_path: Path) -> argparse.Namespace:
    """创建 execute_runtime 生命周期测试所需的最小路径参数。"""

    batch = tmp_path / "artifacts" / "prd050" / "20260723T010203"
    batch.mkdir(parents=True)
    database = batch / "metahub-prd050.db"
    database.touch()
    return SimpleNamespace(
        database_path=database,
        generation=batch.name,
        state_output=batch / "runtime-state.json",
    )


def _create_database_probe(path: Path) -> None:
    """创建只含 0040 启动门禁对象的最小 SQLite 探针库。"""

    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO schema_migrations(version) VALUES(40)")
        for table in sorted(EXPECTED_TABLES):
            connection.execute(f'CREATE TABLE "{table}"(probe_id INTEGER PRIMARY KEY)')
        for trigger in sorted(EXPECTED_TRIGGERS):
            connection.execute(
                f'CREATE TRIGGER "{trigger}" BEFORE INSERT ON cache_affinities BEGIN SELECT 1; END'
            )
        connection.commit()


def _create_cross_tenant_commit_probe(path: Path) -> None:
    """创建可真实提交第二租户夹具并执行精确禁用的最小 SQLite 图。"""

    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE tenants(
              tenant_id TEXT PRIMARY KEY, slug TEXT, name TEXT, service_mode TEXT,
              status TEXT, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE environments(
              environment_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, code TEXT NOT NULL
            );
            CREATE TRIGGER seed_default_environment
            AFTER INSERT ON tenants
            BEGIN
              INSERT INTO environments(environment_id, tenant_id, code)
              VALUES('env_' || NEW.tenant_id, NEW.tenant_id, 'dev');
            END;
            CREATE TABLE providers(
              provider_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, provider_code TEXT,
              name TEXT, base_url TEXT, status TEXT, provider_type TEXT, auth_type TEXT,
              created_at TEXT, updated_at TEXT
            );
            CREATE TABLE models(
              model_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, provider_id TEXT NOT NULL,
              model_alias TEXT, upstream_model_name TEXT, capabilities TEXT, status TEXT,
              created_at TEXT, updated_at TEXT
            );
            CREATE TABLE model_quality_scores(
              model_quality_score_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
              model_id TEXT NOT NULL
            );
            CREATE TRIGGER seed_model_quality_score
            AFTER INSERT ON models
            BEGIN
              INSERT INTO model_quality_scores(
                model_quality_score_id, tenant_id, model_id
              ) VALUES('mqs_' || NEW.model_id, NEW.tenant_id, NEW.model_id);
            END;
            CREATE TABLE model_aliases(
              model_alias_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, alias TEXT,
              environment TEXT, environment_id TEXT, default_model_id TEXT, status TEXT,
              created_at TEXT, updated_at TEXT, disabled_at TEXT
            );
            CREATE TABLE provider_credentials(
              provider_credential_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
              status TEXT
            );
            """
        )
        connection.commit()


def _cross_tenant_acceptance(path: Path) -> Any:
    """构造只运行第二租户提交、恢复和清理逻辑的最小验收对象。"""

    resource_ids = {"main_tenant_id": "tenant_prd050_unit_main"}
    base = SimpleNamespace(
        resource_ids=resource_ids,
        cleanup=lambda *, require_complete: {"verified": not require_complete},
    )
    acceptance = RUNTIME_MODULE.RuntimeAcceptance.__new__(RUNTIME_MODULE.RuntimeAcceptance)
    acceptance.args = SimpleNamespace(database_path=path)
    acceptance.base = base
    acceptance.suffix = "testrun01"
    acceptance.mutation_ledger = []
    return acceptance


def _captured_runtime_webhook_event() -> dict[str, Any]:
    """构造独立于被测 helper 的完整 runtime Webhook 安全信封。"""

    payload = {
        "trigger": "request_selected",
        "request_id": "req_prd050_unit_runtime",
        "cache_affinity_selection_id": "cas_" + "1" * 32,
        "cache_affinity_id": "caf_" + "2" * 32,
        "cache_affinity_version": 1,
        "model_alias_id": "alias_prd050_unit",
        "model_endpoint_id": "model_prd050_unit",
        "selected_provider_id": "provider_prd050_unit",
        "target_kind": "managed",
        "selection_reason": "binding_created",
        "estimated_hit": False,
    }
    return {
        "event_id": payload["cache_affinity_selection_id"],
        "event_type": "cache.affinity_selected",
        "timestamp": "2026-07-21T00:00:00Z",
        "tenant_id": "tenant_prd050_unit",
        "actor_id": None,
        "object_id": payload["cache_affinity_id"],
        "trace_id": "trc_prd050_unit_runtime",
        "payload": payload,
        "payload_keys": sorted(payload),
        "signature_valid": True,
        "privacy_ok": True,
    }


def _database_acceptance(path: Path) -> Any:
    """构造只执行数据库门禁、不初始化 HTTP 客户端的验收对象。"""

    acceptance = RUNTIME_MODULE.RuntimeAcceptance.__new__(RUNTIME_MODULE.RuntimeAcceptance)
    acceptance.args = SimpleNamespace(database_path=path)
    return acceptance


def test_database_and_config_event_exact_membership_contract(
    tmp_path: Path,
) -> None:
    """锁定 0040 结构，并证明同秒随机事件只校验精确成员而不伪造因果顺序。"""

    path = tmp_path / "prd050-probe.db"
    _create_database_probe(path)
    acceptance = _database_acceptance(path)
    assert RUNTIME_MODULE.PRD050_TABLES == frozenset(EXPECTED_TABLES)
    assert RUNTIME_MODULE.PRD050_RUNTIME_TRIGGERS == frozenset(EXPECTED_TRIGGERS)
    assert acceptance._verify_database_ready() == {
        "migration_0040_applied": True,
        "migration_0040_table_count": 5,
        "migration_0040_trigger_count": 8,
    }

    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TRIGGER trg_cache_affinity_selections_prd050_update")
        connection.commit()
    with pytest.raises(AssertionError, match="triggers"):
        acceptance._verify_database_ready()

    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            CREATE TRIGGER trg_cache_affinity_selections_prd050_update
            BEFORE INSERT ON cache_affinities BEGIN SELECT 1; END
            """
        )
        connection.execute("UPDATE schema_migrations SET version=39")
        connection.commit()
    with pytest.raises(AssertionError, match="0040"):
        acceptance._verify_database_ready()

    # 同秒配置事件没有稳定行顺序；倒序必须通过，但精确数量和成员约束不能放宽。
    create_id = "req_prd050_unit_config_create"
    noop_id = "req_prd050_unit_config_noop"
    reversed_events = [
        {"event_type": "noop", "operation_request_id": noop_id},
        {"event_type": "created", "operation_request_id": create_id},
    ]
    RUNTIME_MODULE._assert_exact_config_event_pairs(
        reversed_events,
        create_request_id=create_id,
        noop_request_id=noop_id,
    )

    invalid_event_sets = [
        reversed_events[:1],
        [reversed_events[1], reversed_events[1]],
        [
            *reversed_events,
            {"event_type": "updated", "operation_request_id": "req_prd050_unit_extra"},
        ],
    ]
    for invalid_events in invalid_event_sets:
        with pytest.raises(AssertionError):
            RUNTIME_MODULE._assert_exact_config_event_pairs(
                invalid_events,
                create_request_id=create_id,
                noop_request_id=noop_id,
            )


def test_mutation_is_registered_before_object_id_is_known() -> None:
    """预登记 helper 必须先留下安全恢复线索，成功响应后才能绑定稳定 ID。"""

    acceptance = RUNTIME_MODULE.RuntimeAcceptance.__new__(RUNTIME_MODULE.RuntimeAcceptance)
    acceptance.mutation_ledger = []
    acceptance.base = SimpleNamespace(resource_ids={})
    index = acceptance._pre_register_mutation(
        "provider_create",
        "req_prd050_unit_provider",
        resource_key="provider_a_id",
        resolver="provider_code",
        lookup_value="prd050_unit_local",
    )

    assert index == 0
    assert acceptance.mutation_count == 1
    assert acceptance.mutation_ledger[0]["object_id"] == ""
    assert acceptance.resource_ids == {}

    acceptance._bind_mutation(index, "provider_prd050_unit")
    assert acceptance.mutation_ledger[0]["object_id"] == "provider_prd050_unit"
    assert acceptance.resource_ids["provider_a_id"] == "provider_prd050_unit"


def test_cross_tenant_commit_is_resolved_and_cleaned_after_ledger_handoff_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """第二租户事务提交后即使内存账本交接抛错，finally 仍能定位并禁用入口。"""

    path = tmp_path / "cross-tenant-commit.db"
    _create_cross_tenant_commit_probe(path)
    acceptance = _cross_tenant_acceptance(path)

    def fail_after_commit(_fixture: dict[str, str]) -> None:
        """在事务上下文已经提交后、resource_ids 更新前注入固定异常。"""

        # 异常必须发生在 commit 与内存账本交接之间，才能证明 resolver 读取的是已提交事实；
        # 固定错误不携带 tenant、Alias 或数据库路径，避免测试失败输出扩大业务标识暴露。
        raise RuntimeError("fixed post-commit ledger handoff failure")

    monkeypatch.setattr(
        acceptance,
        "_bind_cross_tenant_fixture_resources",
        fail_after_commit,
    )
    with pytest.raises(RuntimeError, match="post-commit ledger handoff"):
        acceptance._create_cross_tenant_alias()

    with closing(sqlite3.connect(path)) as connection:
        committed = connection.execute(
            "SELECT COUNT(*) FROM tenants WHERE tenant_id=?",
            ("tenant_prd050_cross_testrun01",),
        ).fetchone()[0]
    assert committed == 1
    assert set(acceptance.resource_ids) == {"main_tenant_id"}

    cleanup = acceptance.cleanup_credentials()
    assert cleanup == {"verified": True, "changed": 1}
    assert acceptance.resource_ids == {
        "main_tenant_id": "tenant_prd050_unit_main",
        "cross_tenant_id": "tenant_prd050_cross_testrun01",
        "cross_tenant_environment_id": "env_tenant_prd050_cross_testrun01",
        "cross_tenant_provider_id": "provider_prd050_cross_testrun01",
        "cross_tenant_model_id": "model_prd050_cross_testrun01",
        "cross_tenant_score_id": "mqs_model_prd050_cross_testrun01",
        "cross_tenant_alias_id": "alias_prd050_cross_testrun01",
    }
    with closing(sqlite3.connect(path)) as connection:
        status = connection.execute(
            """
            SELECT status FROM model_aliases
            WHERE tenant_id=? AND model_alias_id=?
            """,
            (
                acceptance.resource_ids["cross_tenant_id"],
                acceptance.resource_ids["cross_tenant_alias_id"],
            ),
        ).fetchone()[0]
    assert status == "disabled"


def test_alias_list_rejects_cross_tenant_rows_for_admin_and_developer() -> None:
    """admin/developer 的公开 Alias 列表只要出现第二租户 ID 就必须失败。"""

    acceptance = RUNTIME_MODULE.RuntimeAcceptance.__new__(RUNTIME_MODULE.RuntimeAcceptance)
    acceptance.suffix = "testrun01"
    acceptance.base = SimpleNamespace(
        resource_ids={
            "main_tenant_id": "tenant_prd050_unit_main",
            "cross_tenant_alias_id": "alias_prd050_unit_cross",
        },
        _headers=lambda token, request_id: {
            "Authorization": token,
            "X-Request-ID": request_id,
        },
    )
    route = {
        "alias_id": "alias_prd050_unit_main",
        "environment_id": "env_prd050_unit_main",
    }
    main_alias = {
        "model_alias_id": route["alias_id"],
        "tenant_id": "tenant_prd050_unit_main",
        "cache_affinity": None,
    }
    acceptance.control = AliasListControl([main_alias])
    assert acceptance._read_alias("admin-token", route, label="admin_clean") == main_alias
    assert acceptance._read_alias("developer-token", route, label="developer_clean") == main_alias

    foreign_alias = {
        "model_alias_id": "alias_prd050_unit_cross",
        "tenant_id": "tenant_prd050_unit_cross",
    }
    acceptance.control = AliasListControl([main_alias, foreign_alias])
    for role in ("admin", "developer"):
        with pytest.raises(AssertionError):
            acceptance._read_alias(f"{role}-token", route, label=f"{role}_leak")


def test_local_provider_proves_sticky_key_and_independent_cache_tokens() -> None:
    """假上游阻塞并发长 Prompt、只接受内存 Key，并保留独立缓存 Token 证据。"""

    long_prompt = "L" * 300
    short_prompt = "unit-sensitive-short-body"
    response_sentinel = "unit-sensitive-response"
    key_a = "sk-prd050-unit-a"
    key_b = "sk-prd050-unit-b"
    with RUNTIME_MODULE.RunningAcceptanceServer(
        long_prompt,
        short_prompt,
        response_sentinel,
    ) as server:
        server.install_provider_keys([key_a, key_b])
        host, port = server.server_address
        url = f"http://{host}:{port}/v1/chat/completions"

        def call(prompt: str, key: str) -> httpx.Response:
            """向真实回环线程发送一次最小 OpenAI-compatible 请求。"""

            return httpx.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": "prd050-cache-model",
                    "messages": [{"role": "system", "content": prompt}],
                },
                timeout=5,
            )

        # 两次长 Prompt 必须并发进入；首请求在第二个到达前不会返回，从而真实模拟
        # Gateway Selection 已提交但 Provider 尚未成功的 pending 窗口。
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(call, long_prompt, key_a)
            second_future = executor.submit(call, long_prompt, key_a)
            first = first_future.result(timeout=10)
            second = second_future.result(timeout=10)
        short = call(short_prompt, key_b)
        unknown = call(short_prompt, "sk-unknown")
        records = server.provider_calls()

    assert [first.status_code, second.status_code, short.status_code, unknown.status_code] == [
        200,
        200,
        200,
        401,
    ]
    assert [
        first.json()["usage"]["prompt_tokens_details"]["cached_tokens"],
        second.json()["usage"]["prompt_tokens_details"]["cached_tokens"],
        short.json()["usage"]["prompt_tokens_details"]["cached_tokens"],
    ] == [17, 17, 5]
    assert [item["authorization_slot"] for item in records] == [
        "slot_a",
        "slot_a",
        "slot_b",
    ]
    encoded = json.dumps(records, sort_keys=True)
    assert key_a not in encoded and key_b not in encoded
    assert long_prompt not in encoded and short_prompt not in encoded


def test_webhook_receiver_verifies_hmac_and_rejects_internal_fields() -> None:
    """接收器只保留精确 runtime 摘要，并拒绝合法签名中的 data_class 扩展。"""

    secret = "whsec_prd050_unit_secret"
    payload = {
        "trigger": "request_selected",
        "request_id": "req_prd050_unit_runtime",
        "cache_affinity_selection_id": "cas_" + "1" * 32,
        "cache_affinity_id": "caf_" + "2" * 32,
        "cache_affinity_version": 1,
        "model_alias_id": "alias_prd050_unit",
        "model_endpoint_id": "model_prd050_unit",
        "selected_provider_id": "provider_prd050_unit",
        "target_kind": "managed",
        "selection_reason": "binding_created",
        "estimated_hit": False,
    }
    envelope = {
        "event_id": payload["cache_affinity_selection_id"],
        "event_type": "cache.affinity_selected",
        "timestamp": "2026-07-21T00:00:00Z",
        "tenant_id": "tenant_prd050_unit",
        "actor_id": None,
        "object_id": payload["cache_affinity_id"],
        "trace_id": "trc_prd050_unit_runtime",
        "payload": payload,
    }
    with RUNTIME_MODULE.RunningAcceptanceServer(
        "L" * 300,
        "short",
        "unit-response",
    ) as server:
        server.signing_secret = secret
        host, port = server.server_address

        def post_event(value: dict[str, Any]) -> httpx.Response:
            """按真实 HMAC wire 投递一个信封，不复用旧正文签名。"""

            raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
            signature = webhook_signature_header(secret, int(time.time()), raw)
            return httpx.post(
                f"http://{host}:{port}/hook",
                headers={
                    "Content-Type": "application/json",
                    "X-MetaHub-Signature": signature,
                },
                content=raw,
                timeout=5,
            )

        accepted = post_event(envelope)
        unsafe_payload = json.loads(json.dumps(envelope))
        unsafe_payload["payload"]["data_class"] = "regulated.secret"
        payload_denied = post_event(unsafe_payload)
        unsafe_top = json.loads(json.dumps(envelope))
        unsafe_top["provider_credential_id"] = "pck_prd050_unit"
        top_denied = post_event(unsafe_top)
        missing_tenant = json.loads(json.dumps(envelope))
        del missing_tenant["tenant_id"]
        missing_denied = post_event(missing_tenant)
        captured = server.webhook_events()

    assert accepted.status_code == 204
    assert [payload_denied.status_code, top_denied.status_code, missing_denied.status_code] == [
        401,
        401,
        401,
    ]
    assert len(captured) == 1
    assert set(captured[0]) == RUNTIME_MODULE.CAPTURED_WEBHOOK_EVENT_FIELDS
    assert set(captured[0]["payload"]) == RUNTIME_MODULE.RUNTIME_EVENT_FIELDS
    assert captured[0]["tenant_id"] == "tenant_prd050_unit"
    assert captured[0]["trace_id"] == "trc_prd050_unit_runtime"
    assert captured[0]["signature_valid"] is True
    assert captured[0]["privacy_ok"] is True
    assert "data_class" not in json.dumps(captured[0], sort_keys=True)
    RUNTIME_MODULE._assert_runtime_webhook_event(
        captured[0],
        tenant_id="tenant_prd050_unit",
        request_id="req_prd050_unit_runtime",
        selection_id="cas_" + "1" * 32,
        cache_affinity_id="caf_" + "2" * 32,
        cache_affinity_version=1,
        model_alias_id="alias_prd050_unit",
        model_endpoint_id="model_prd050_unit",
        selected_provider_id="provider_prd050_unit",
        selection_reason="binding_created",
        estimated_hit=False,
    )


@pytest.mark.parametrize(
    ("container", "field", "unsafe_value"),
    [
        ("event", "tenant_id", "tenant_prd050_foreign"),
        ("event", "trace_id", "trc_prd050_foreign"),
        ("payload", "model_alias_id", "alias_prd050_foreign"),
        ("payload", "model_endpoint_id", "model_prd050_foreign"),
        ("payload", "selected_provider_id", "provider_prd050_foreign"),
        ("payload", "cache_affinity_version", 2),
    ],
)
def test_runtime_webhook_binding_rejects_tenant_and_route_identity_drift(
    container: str,
    field: str,
    unsafe_value: object,
) -> None:
    """tenant/trace/Alias/Endpoint/Provider/version 任一漂移都不能成为运行证据。"""

    event = json.loads(json.dumps(_captured_runtime_webhook_event()))
    target = event if container == "event" else event["payload"]
    target[field] = unsafe_value
    with pytest.raises(AssertionError):
        RUNTIME_MODULE._assert_runtime_webhook_event(
            event,
            tenant_id="tenant_prd050_unit",
            request_id="req_prd050_unit_runtime",
            selection_id="cas_" + "1" * 32,
            cache_affinity_id="caf_" + "2" * 32,
            cache_affinity_version=1,
            model_alias_id="alias_prd050_unit",
            model_endpoint_id="model_prd050_unit",
            selected_provider_id="provider_prd050_unit",
            selection_reason="binding_created",
            estimated_hit=False,
        )


def test_execute_runtime_cleans_before_persisting_passed_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """成功发布必须清理，且入口/发布碰撞和临时文件归属都遵循 write-once。"""

    FakeAcceptance.instances.clear()
    args = _args(tmp_path)
    foreign_temporary = args.state_output.with_name(
        f".{args.state_output.name}.foreign-owned.tmp"
    )
    foreign_temporary.write_bytes(b"foreign-temporary-must-survive")
    # 首次随机名刻意命中外来临时文件，第二次才取得本进程拥有的名字；这锁住了“碰撞
    # 只能重试、不能清理”的边界，同时仍由真实 xb/open、fsync 和 link 完成成功发布。
    temporary_tokens = iter(("foreign-owned", "runtime-owned"))
    with monkeypatch.context() as scoped:
        scoped.setattr(
            RUNTIME_MODULE.secrets,
            "token_hex",
            lambda _size: next(temporary_tokens),
        )
        state = RUNTIME_MODULE.execute_runtime(
            args,
            acceptance_factory=FakeAcceptance,
            server_factory=FakeReceiverContext,
        )

    acceptance = FakeAcceptance.instances[-1]
    assert acceptance.cleanup_calls == 1 and acceptance.closed is True
    assert state["lifecycle"] == {
        "credentials_cleanup_completed": True,
        "credentials_cleanup_verified": True,
        "credentials_changed": 6,
        "mutations_pre_registered": 9,
    }
    assert state["schema_version"] == 1
    assert state["prd_id"] == "PRD-050"
    assert state["generation"] == args.generation
    persisted = args.state_output.read_text(encoding="utf-8")
    assert json.loads(persisted) == state
    assert "unit-sensitive" not in persisted
    expected_keys = set(state["request_ids"])
    assert expected_keys == RUNTIME_STATE_REQUEST_ID_FIELDS
    assert len(set(state["request_ids"].values())) == 5
    assert state["checks"]["database_pending_selection_count"] == 0
    assert state["checks"]["database_active_selection_count"] == 0
    assert foreign_temporary.read_bytes() == b"foreign-temporary-must-survive"
    assert not args.state_output.with_name(
        f".{args.state_output.name}.runtime-owned.tmp"
    ).exists()

    # 同一路径重入必须在 acceptance_factory 前失败，既不删除已发布证据，也不新增任何
    # 业务 mutation；固定码不得携带状态路径或旧 JSON 内容。
    persisted_bytes = args.state_output.read_bytes()
    instance_count = len(FakeAcceptance.instances)
    with pytest.raises(RuntimeError) as entry_collision:
        RUNTIME_MODULE.execute_runtime(
            args,
            acceptance_factory=FakeAcceptance,
            server_factory=FakeReceiverContext,
        )
    assert str(entry_collision.value) == RUNTIME_MODULE.RUNTIME_STATE_OUTPUT_COLLISION_CODE
    assert args.state_output.read_bytes() == persisted_bytes
    assert len(FakeAcceptance.instances) == instance_count

    # 入口检查后仍可能有竞争者率先发布。真实 os.link 必须以 EEXIST 失败，竞争者字节保持
    # 不变，本轮已经清理业务凭据，并且只回收自己通过 xb 创建的临时文件。
    race_args = _args(tmp_path / "publish-race")
    race_bytes = b"concurrent-writer-evidence"
    real_link = os.link

    def publish_competitor_then_link(source: object, destination: object) -> None:
        """在被测 link 的最后竞争窗口创建目标，再调用真实不可覆盖硬链接。"""

        Path(destination).write_bytes(race_bytes)
        real_link(source, destination)

    with monkeypatch.context() as scoped:
        scoped.setattr(RUNTIME_MODULE.os, "link", publish_competitor_then_link)
        with pytest.raises(RuntimeError) as publish_collision:
            RUNTIME_MODULE.execute_runtime(
                race_args,
                acceptance_factory=FakeAcceptance,
                server_factory=FakeReceiverContext,
            )
    assert str(publish_collision.value) == RUNTIME_MODULE.RUNTIME_STATE_OUTPUT_COLLISION_CODE
    assert race_args.state_output.read_bytes() == race_bytes
    race_acceptance = FakeAcceptance.instances[-1]
    assert race_acceptance.cleanup_calls == 1 and race_acceptance.closed is True
    assert list(
        race_args.state_output.parent.glob(f".{race_args.state_output.name}.*.tmp")
    ) == []

    # 发布门禁必须拒绝缺失/错误 schema、bool 冒充整数以及错误 PRD；所有反例仍应执行
    # 凭据清理并关闭验收器，且不能产生任何可能被 Browser 误认的局部状态。
    valid_run = FakeAcceptance.run
    contract_cases = (
        ("missing-schema", {}, {"schema_version"}),
        ("wrong-schema", {"schema_version": 2}, set()),
        ("boolean-schema", {"schema_version": True}, set()),
        ("missing-prd", {}, {"prd_id"}),
        ("wrong-prd", {"prd_id": "PRD-049"}, set()),
    )
    for label, overrides, removed_fields in contract_cases:
        invalid_args = _args(tmp_path / label)

        def invalid_contract_run(
            self: FakeAcceptance,
            receiver: object,
            *,
            _overrides: dict[str, Any] = overrides,
            _removed_fields: set[str] = removed_fields,
        ) -> dict[str, Any]:
            """从合法夹具派生一个固定合同反例，不引入凭据或业务正文。"""

            candidate = valid_run(self, receiver)
            candidate.update(_overrides)
            for field in _removed_fields:
                candidate.pop(field, None)
            return candidate

        with monkeypatch.context() as scoped:
            scoped.setattr(FakeAcceptance, "run", invalid_contract_run)
            with pytest.raises(AssertionError):
                RUNTIME_MODULE.execute_runtime(
                    invalid_args,
                    acceptance_factory=FakeAcceptance,
                    server_factory=FakeReceiverContext,
                )
        invalid_acceptance = FakeAcceptance.instances[-1]
        assert invalid_acceptance.cleanup_calls == 1 and invalid_acceptance.closed is True
        assert invalid_args.state_output.exists() is False


def test_execute_runtime_failure_cleans_without_partial_state(tmp_path: Path) -> None:
    """中途失败仍执行 finally 清理，但不能留下可误认成通过证据的 JSON。"""

    FailingAcceptance.instances.clear()
    args = _args(tmp_path)
    with pytest.raises(RuntimeError, match="fixed PRD-050 acceptance failure"):
        RUNTIME_MODULE.execute_runtime(
            args,
            acceptance_factory=FailingAcceptance,
            server_factory=FakeReceiverContext,
        )

    acceptance = FailingAcceptance.instances[-1]
    assert acceptance.cleanup_calls == 1 and acceptance.closed is True
    assert args.state_output.exists() is False


def test_execute_runtime_redaction_failure_never_writes_passed_state(tmp_path: Path) -> None:
    """run 返回 passed 但候选含敏感值时，清理后也不得以空敏感列表重新放行。"""

    LeakyAcceptance.instances.clear()
    args = _args(tmp_path)
    with pytest.raises(AssertionError, match="sensitive value"):
        RUNTIME_MODULE.execute_runtime(
            args,
            acceptance_factory=LeakyAcceptance,
            server_factory=FakeReceiverContext,
        )

    acceptance = LeakyAcceptance.instances[-1]
    assert acceptance.cleanup_calls == 1 and acceptance.closed is True
    assert args.state_output.exists() is False


def test_execute_runtime_context_exit_failure_never_writes_passed_state(
    tmp_path: Path,
) -> None:
    """run 已返回成功后 Context 退出抛错，仍只能清理并保留原异常。"""

    FakeAcceptance.instances.clear()
    args = _args(tmp_path)
    with pytest.raises(RuntimeError, match="receiver exit failure"):
        RUNTIME_MODULE.execute_runtime(
            args,
            acceptance_factory=FakeAcceptance,
            server_factory=FailingReceiverContext,
        )

    acceptance = FakeAcceptance.instances[-1]
    assert acceptance.cleanup_calls == 1 and acceptance.closed is True
    assert args.state_output.exists() is False


def test_execute_runtime_unverified_cleanup_never_writes_passed_state(
    tmp_path: Path,
) -> None:
    """cleanup 返回 verified=false 必须失败关闭，不能仅把 false 写进 passed 生命周期。"""

    UnverifiedCleanupAcceptance.instances.clear()
    args = _args(tmp_path)
    with pytest.raises(AssertionError, match="cleanup verification"):
        RUNTIME_MODULE.execute_runtime(
            args,
            acceptance_factory=UnverifiedCleanupAcceptance,
            server_factory=FakeReceiverContext,
        )

    acceptance = UnverifiedCleanupAcceptance.instances[-1]
    assert acceptance.cleanup_calls == 1 and acceptance.closed is True
    assert args.state_output.exists() is False


def test_parse_args_requires_isolated_batch_logs_and_explicit_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """数据库、状态、日志必须同批次，且写验收必须有显式确认。"""

    monkeypatch.chdir(tmp_path)
    batch = tmp_path / "artifacts" / "prd050" / "20260723T010203"
    batch.mkdir(parents=True)
    database = batch / "metahub-prd050.db"
    log = batch / "services.log"
    database.touch()
    log.touch()
    password_environment = {
        "METAHUB_PRD050_ADMIN_PASSWORD": "unit-admin-password-from-env",
        "METAHUB_ADMIN_PASSWORD": "unit-admin-password-from-generic-env",
        "METAHUB_PRD050_DEVELOPER_PASSWORD": "unit-developer-password-from-env",
        "METAHUB_PRD050_VIEWER_PASSWORD": "unit-viewer-password-from-env",
    }
    for name, value in password_environment.items():
        monkeypatch.setenv(name, value)
    stdin_admin_password = "unit-admin-password-from-stdin"

    environment_args = RUNTIME_MODULE.parse_args(
        [
            "--database-path",
            str(database),
            "--state-output",
            str(batch / "runtime-state.json"),
            "--generation",
            batch.name,
            "--password-stdin",
            "--log-path",
            str(log),
            "--suffix",
            "passwordenv",
            "--confirm-local-state-mutation",
        ],
        password_stream=io.StringIO(f"{stdin_admin_password}\n"),
    )
    # 只比较域分离摘要，失败输出不会展开三个测试口令的明文。
    parsed_passwords = (
        environment_args.admin_password,
        environment_args.developer_password,
        environment_args.viewer_password,
    )
    parsed_digests = tuple(
        hashlib.sha256(f"prd050-test:{value}".encode()).digest()
        for value in parsed_passwords
    )
    expected_digests = tuple(
        hashlib.sha256(f"prd050-test:{value}".encode()).digest()
        for value in (
            stdin_admin_password,
            password_environment["METAHUB_PRD050_DEVELOPER_PASSWORD"],
            password_environment["METAHUB_PRD050_VIEWER_PASSWORD"],
        )
    )
    assert parsed_digests == expected_digests
    assert all(name not in os.environ for name in password_environment)

    argv = [
        "--database-path",
        str(database),
        "--state-output",
        str(batch / "runtime-state.json"),
        "--generation",
        batch.name,
        "--password-stdin",
        "--log-path",
        str(log),
        "--suffix",
        "testrun01",
    ]
    with pytest.raises(AssertionError, match="confirm-local-state-mutation"):
        RUNTIME_MODULE.parse_args(
            argv,
            password_stream=io.StringIO(f"{stdin_admin_password}\n"),
        )

    args = RUNTIME_MODULE.parse_args(
        [*argv, "--confirm-local-state-mutation"],
        password_stream=io.StringIO(f"{stdin_admin_password}\n"),
    )
    assert args.database_path == database.resolve()
    assert args.log_path == [log.resolve()]
    assert args.control_url.endswith(":18001")
    assert args.gateway_url.endswith(":18000")
    assert args.console_url.endswith(":15173")

    assert args.generation == batch.name

    generation_index = argv.index("--generation")
    missing_generation_argv = list(argv)
    del missing_generation_argv[generation_index : generation_index + 2]
    with pytest.raises(SystemExit):
        RUNTIME_MODULE.parse_args(
            [*missing_generation_argv, "--confirm-local-state-mutation"],
            password_stream=io.StringIO(f"{stdin_admin_password}\n"),
        )

    mismatched_generation_argv = list(argv)
    mismatched_generation_argv[generation_index + 1] = "20260723T010204"
    with pytest.raises(AssertionError):
        RUNTIME_MODULE.parse_args(
            [*mismatched_generation_argv, "--confirm-local-state-mutation"],
            password_stream=io.StringIO(f"{stdin_admin_password}\n"),
        )
    invalid_generation_argv = list(argv)
    invalid_generation_argv[generation_index + 1] = "testrun01"
    with pytest.raises(AssertionError):
        RUNTIME_MODULE.parse_args(
            [*invalid_generation_argv, "--confirm-local-state-mutation"],
            password_stream=io.StringIO(f"{stdin_admin_password}\n"),
        )

    missing_password_stdin_argv = [
        item for item in argv if item != "--password-stdin"
    ]
    with pytest.raises(AssertionError, match="password-stdin"):
        RUNTIME_MODULE.parse_args(
            [*missing_password_stdin_argv, "--confirm-local-state-mutation"],
            password_stream=io.StringIO(f"{stdin_admin_password}\n"),
        )

    with pytest.raises(AssertionError, match="password stdin"):
        RUNTIME_MODULE.parse_args(
            [*argv, "--confirm-local-state-mutation"],
            password_stream=io.StringIO(""),
        )

    with pytest.raises(SystemExit):
        RUNTIME_MODULE.parse_args(
            [
                *argv,
                "--admin-password",
                "unit-forbidden-admin-password",
                "--confirm-local-state-mutation",
            ],
            password_stream=io.StringIO(f"{stdin_admin_password}\n"),
        )
    outside_log = tmp_path / "outside.log"
    outside_log.touch()
    outside_argv = list(argv)
    outside_argv[outside_argv.index(str(log))] = str(outside_log)
    with pytest.raises(AssertionError, match="artifacts/prd050"):
        RUNTIME_MODULE.parse_args(
            [*outside_argv, "--confirm-local-state-mutation"],
            password_stream=io.StringIO(f"{stdin_admin_password}\n"),
        )


def test_source_freezes_public_contract_and_uses_only_local_upstream() -> None:
    """静态门禁锁住验收范围、关键断言与本地上游，防止后续脚本降级成冒烟测试。"""

    source = SCRIPT_PATH.read_text(encoding="utf-8")
    required_fragments = {
        "\"/model-aliases/{route['alias_id']}/cache-affinity\"",
        '"cache_affinity.version_conflict"',
        '"cache_affinity.request_conflict"',
        '"cache.affinity_selected"',
        '"binding_created"',
        '"binding_pending_reused"',
        '"binding_warm_reused"',
        '"binding_rebound"',
        '"cache_affinity_created"',
        '"cache_affinity_pending"',
        '"cache_affinity_warm"',
        '"cache_affinity_rebound"',
        'f"/provider-keys/{provider_key_id}"',
        '"cache_tokens"',
        '"cache_affinity_selections"',
        '"database_active_selection_count"',
        "outcome_status='pending'",
        "ThreadPoolExecutor(max_workers=2",
        '"--confirm-local-state-mutation"',
        'candidate.open("xb")',
        "os.fsync(stream.fileno())",
        '"--generation"',
        '"--password-stdin"',
        "_read_admin_password_from_stdin",
        '"schema_version": 1',
        'isinstance(schema_version, int)',
        'not isinstance(schema_version, bool)',
        'candidate.get("prd_id") == "PRD-050"',
        'candidate["generation"] = args.generation',
        "os.link(temporary, path)",
        "RUNTIME_STATE_OUTPUT_COLLISION_CODE",
        "finally:",
        "self._pre_register_mutation(",
    }
    assert all(fragment in source for fragment in required_fragments)
    assert "_BASE.write_state_atomically(args.state_output, state)" not in source
    assert '"--admin-password"' not in source
    lowered = source.lower()
    assert "openrouter" not in lowered
    assert "siliconflow" not in lowered
    assert "硅基流动" not in source
    assert "http://127.0.0.1" in source
