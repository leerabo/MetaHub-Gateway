"""PRD-059 后端路由 permission 映射与权限驱动 Console 导航专项测试。"""

from __future__ import annotations

import ast
from pathlib import Path

from metahub_control.console.service import _visible_navigation_items
from metahub_shared.rbac import ROLE_CODES, ROLE_GRANTABLE_PERMISSIONS

# 源码根目录与路由守卫表冻结 PRD-059 直接影响面，测试只做只读 AST 校验。
_CONTROL_ROOT = Path(__file__).resolve().parents[2] / "src" / "metahub_control"
"""控制面源码根目录；静态门禁只读取 PRD-059 直接受影响的路由文件。"""

# 下列函数与精确 permission 守卫构成冻结合同，禁止重新合并成角色级管理员守卫。
_ROUTE_GUARDS = {
    "tenants/routes.py": {
        "current_tenant": "require_tenant_settings_reader",
        "patch_current_tenant": "require_tenant_settings_reader",
    },
    "providers/routes.py": {
        "get_providers": "require_provider_reader",
        "post_provider": "require_provider_writer",
        "patch_provider": "require_provider_writer",
        "post_provider_test": "require_provider_writer",
        "get_provider_keys": "require_provider_reader",
        "post_provider_key": "require_provider_writer",
        "patch_provider_key": "require_provider_writer",
    },
    "models/routes.py": {
        "get_models": "require_model_catalog_reader",
        "post_model": "require_model_catalog_writer",
        "patch_model": "require_model_catalog_writer",
    },
    "model_aliases/routes.py": {
        "get_model_aliases": "require_routing_reader",
        "post_model_alias": "require_routing_writer",
        "patch_model_alias": "require_routing_writer",
        "patch_cache_affinity": "require_routing_writer",
        "patch_fallback_policy": "require_routing_writer",
        "patch_routing_mode": "require_routing_writer",
    },
    "virtual_keys/routes.py": {
        "get_virtual_keys": "require_virtual_key_reader",
        "post_virtual_key": "require_virtual_key_writer",
        "patch_virtual_key": "require_virtual_key_writer",
        "patch_virtual_key_limits": "require_virtual_key_writer",
        "delete_virtual_key": "require_virtual_key_writer",
    },
    "provider_health/routes.py": {
        "get_provider_health": "require_provider_health_reader",
        "post_provider_probe": "require_provider_health_prober",
    },
    "dlp/routes.py": {
        "list_dlp_rules_route": "require_dlp_reader",
        "create_dlp_rule_route": "require_dlp_writer",
        "patch_dlp_rule_route": "require_dlp_writer",
    },
    "finops/routes.py": {
        "get_finops_ledger": "require_finops_reader",
        "post_finops_adjustment": "require_finops_writer",
    },
    "policies/routes.py": {
        "get_policies": "require_policy_reader",
        "post_policy_validation": "require_policy_writer",
        "post_policy_dry_run": "require_policy_simulator",
        "post_policy": "require_policy_writer",
        "patch_policy": "require_policy_writer",
    },
    "policy_snapshots/routes.py": {
        "get_policy_snapshot_current": "require_policy_reader",
        "get_policy_snapshot_history": "require_policy_reader",
        "post_policy_snapshot_publish": "require_policy_publisher",
        "post_policy_snapshot_rollback": "require_policy_publisher",
    },
}
"""函数到精确后端守卫的冻结映射，防止后续重新退化为 admin 角色判断。"""


def _dependency_names(function: ast.AsyncFunctionDef) -> set[str]:
    """提取异步路由参数注解中的 Depends 守卫名，不执行应用或访问数据库。"""

    names: set[str] = set()
    for argument in function.args.args:
        annotation = argument.annotation
        if annotation is None:
            continue
        for node in ast.walk(annotation):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Depends"
                and node.args
                and isinstance(node.args[0], ast.Name)
            ):
                names.add(node.args[0].id)
    return names


def test_directly_affected_routes_use_exact_permission_guards() -> None:
    """静态权限：每个直接受影响路由必须绑定设计指定的读写 permission 守卫。"""

    for relative_path, expected in _ROUTE_GUARDS.items():
        source = (_CONTROL_ROOT / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
        }
        for function_name, guard_name in expected.items():
            assert function_name in functions, f"{relative_path}:{function_name}"
            assert _dependency_names(functions[function_name]) == {guard_name}


def test_six_role_navigation_uses_permissions_not_role_name() -> None:
    """权限：Finance/Security/Developer 的导航严格服从数据库权限集合。"""

    items_by_role = {
        role: {
            item.id: item
            for item in _visible_navigation_items(
                tuple(sorted(ROLE_GRANTABLE_PERMISSIONS[role]))
            )
        }
        for role in ROLE_CODES
    }

    assert "providers" not in items_by_role["finance_admin"]
    assert "usage" in items_by_role["finance_admin"]
    assert "model-prices" in items_by_role["security_admin"]
    assert "providers" not in items_by_role["security_admin"]
    assert "usage" not in items_by_role["developer"]
    assert "finops-ledger" not in items_by_role["developer"]
    assert "role-permissions" in items_by_role["admin"]
    assert all(
        "role-permissions" not in items_by_role[role]
        for role in ROLE_CODES
        if role != "admin"
    )
    assert all(
        item.required_permissions
        for role_items in items_by_role.values()
        for item in role_items.values()
    )


def test_compatibility_roles_are_recomputed_after_permission_revocation() -> None:
    """兼容：required_roles 必须反映租户当前矩阵，不能保留默认角色硬编码。"""

    matrix = {
        role: frozenset(permissions)
        for role, permissions in ROLE_GRANTABLE_PERMISSIONS.items()
    }
    matrix["developer"] = matrix["developer"] - {"webhook.manage"}
    admin_items = _visible_navigation_items(
        tuple(sorted(ROLE_GRANTABLE_PERMISSIONS["admin"])),
        role_permissions_by_role=matrix,
    )
    webhook = next(item for item in admin_items if item.id == "webhooks")

    assert webhook.required_permissions == ["webhook.manage"]
    assert "developer" not in webhook.required_roles
    assert webhook.required_roles == ["admin", "security_admin", "ops_admin"]
