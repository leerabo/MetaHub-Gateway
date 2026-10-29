"""只持久化认证密文的本地原子 Blob Store。

对象路径完全由服务端 file ID 和固定 blob kind 生成，不接收客户端文件名。staging 与最终
对象位于同一 root，发布使用 ``os.replace``；所有路径和 I/O 异常只映射固定文件错误码。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex
from typing import Final, Literal

from .contracts import FileContractError

StoredBlobKind = Literal["original", "extract"]

# 对象键只允许服务端生成的两类固定路径，防止 path traversal 和任意宿主文件读写。
_OBJECT_KEY = re.compile(
    r"^objects/[0-9a-f]{2}/file_[0-9a-f]{32}/(?:original|extract)\.bin$"
)
MAX_STORED_CIPHERTEXT_BYTES: Final[int] = 80 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class StagedFileBlob:
    """同一 storage root 内待原子发布的密文对象。

    临时宿主路径关闭 repr，禁止进入日志/API；object_key 是服务端固定格式，但仍只供数据库
    内部索引，不应出现在公共响应。
    """

    path: Path = field(repr=False)
    object_key: str = field(repr=False)


class EncryptedLocalBlobStore:
    """实现加密本地对象的 stage、publish、read 和 delete。

    构造参数是部署配置而不是请求输入。root 本身若为符号链接会失败关闭；每次操作还复核
    解析后的目标仍位于 root 内，并拒绝现存符号链接节点。
    """

    def __init__(self, root: object) -> None:
        """创建并固定 storage root，路径值不会写入异常。"""

        if not isinstance(root, (str, Path)) or not str(root):
            # 存储根只能来自明确部署路径，禁止把任意对象或空值解释成当前目录。
            raise FileContractError("file.storage_unavailable")
        candidate = Path(root)
        try:
            if candidate.exists() and candidate.is_symlink():
                # 根目录符号链接会把密文写出部署 owner 边界，必须在创建子目录前拒绝。
                raise FileContractError("file.storage_unavailable")
            candidate.mkdir(parents=True, exist_ok=True)
            self._root = candidate.resolve(strict=True)
            self._staging = self._root / ".staging"
            self._staging.mkdir(parents=True, exist_ok=True)
            if self._staging.is_symlink():
                # staging 也必须是本 root 的真实目录，避免发布源被链接节点替换。
                raise FileContractError("file.storage_unavailable")
        # 路径合同错误保持固定语义，宿主 I/O 错误统一折叠且不回显目录。
        except FileContractError:
            raise
        except OSError:
            # 操作系统异常可能包含宿主路径和账号信息，离开边界前必须清除。
            raise FileContractError("file.storage_unavailable") from None

    def stage(self, ciphertext: object, *, object_key: object) -> StagedFileBlob:
        """把有界认证密文写入同卷 staging 文件并 fsync。

        参数：AES-GCM 密文和服务端对象键。返回：关闭 repr 的 staging handle。明文不得调用
        本接口；上层通过密文格式测试保证传入对象已经认证加密。
        """

        key = _normalize_object_key(object_key)
        if type(ciphertext) is not bytes or not ciphertext:
            # Blob Store 只接受非空认证密文，明文字符串或空占位不得持久化。
            raise FileContractError("file.storage_unavailable")
        if len(ciphertext) > MAX_STORED_CIPHERTEXT_BYTES:
            # 密文也需硬上限，避免加密封装或损坏输入绕过原文件大小门禁。
            raise FileContractError("file.extract_limit_exceeded")
        stage_path = self._staging / f"stage_{token_hex(24)}.bin"
        try:
            with stage_path.open("xb") as handle:
                handle.write(ciphertext)
                handle.flush()
                os.fsync(handle.fileno())
            return StagedFileBlob(path=stage_path, object_key=key)
        # staging 写入或 fsync 失败时先清理不完整密文，再返回不含路径的固定错误。
        except OSError:
            _unlink_quietly(stage_path)
            raise FileContractError("file.storage_unavailable") from None

    def publish(self, staged: object) -> str:
        """把同 root staging 密文原子发布到固定对象键。

        参数：本 store 产生的 staging handle。返回：内部对象键。目标存在或任何路径节点为
        符号链接都失败关闭，禁止覆盖既有文件。
        """

        if not isinstance(staged, StagedFileBlob):
            # 发布句柄必须由当前存储实现产生，浏览器对象或任意 Path 不能越过 staging。
            raise FileContractError("file.integrity_failed")
        source = self._resolve_staged(staged.path)
        target = self._target_path(staged.object_key, create_parent=True)
        try:
            if target.exists() or target.is_symlink():
                # 随机对象键仍禁止覆盖，存在目标说明 owner 冲突或目录被篡改。
                raise FileContractError("file.integrity_failed")
            os.replace(source, target)
            return staged.object_key
        # owner/路径完整性错误保持原码，os.replace 故障单独映射为依赖不可用。
        except FileContractError:
            raise
        except OSError:
            # 不回显源和目标宿主路径，避免普通错误响应泄露部署拓扑。
            raise FileContractError("file.storage_unavailable") from None

    def read(self, object_key: object) -> bytes:
        """读取一个有界密文对象，路径或大小异常固定失败。"""

        target = self._target_path(object_key, create_parent=False)
        try:
            if target.is_symlink() or not target.is_file():
                # 缺失、目录或链接都不具备可信 Blob owner，统一视为完整性失败。
                raise FileContractError("file.integrity_failed")
            if target.stat().st_size > MAX_STORED_CIPHERTEXT_BYTES:
                # 读取前复核密文大小，防止磁盘篡改导致无界内存读取。
                raise FileContractError("file.integrity_failed")
            return target.read_bytes()
        # 已识别对象异常保持完整性语义，宿主 I/O 失败不携带路径上抛。
        except FileContractError:
            raise
        except OSError:
            # 文件系统错误统一折叠，调用方只能依据 request_id 排障。
            raise FileContractError("file.storage_unavailable") from None

    def delete(self, object_key: object) -> None:
        """删除精确对象键，缺失视为幂等成功但符号链接视为完整性故障。"""

        target = self._target_path(object_key, create_parent=False)
        try:
            if target.is_symlink():
                # delete 不能跟随链接，否则可能删除 storage root 外的宿主文件。
                raise FileContractError("file.integrity_failed")
            target.unlink(missing_ok=True)
        # 路径完整性错误不可降级为幂等缺失，避免掩盖对象目录篡改。
        except FileContractError:
            raise
        except OSError:
            # 删除故障只报告存储不可用，禁止暴露对象键对应的宿主路径。
            raise FileContractError("file.storage_unavailable") from None

    def discard(self, staged: object) -> None:
        """尽力删除尚未发布的 staging 密文，不暴露宿主路径。"""

        if not isinstance(staged, StagedFileBlob):
            return
        try:
            path = self._resolve_staged(staged.path)
        # discard 是补偿清理；无效句柄不能覆盖调用方正在处理的原始业务错误。
        except FileContractError:
            return
        _unlink_quietly(path)

    def _resolve_staged(self, value: Path) -> Path:
        """证明 staging 路径是本 store 的直接普通文件子节点。"""

        try:
            resolved = value.resolve(strict=True)
        # 无法解析的 staging 路径不回显宿主细节，统一视为存储暂不可用。
        except OSError:
            raise FileContractError("file.storage_unavailable") from None
        if (
            resolved.parent != self._staging
            or value.is_symlink()
            or not resolved.is_file()
        ):
            # 只接受 staging 直接普通文件，拒绝跨目录、链接或目录节点替换。
            raise FileContractError("file.integrity_failed")
        return resolved

    def _target_path(self, object_key: object, *, create_parent: bool) -> Path:
        """把严格对象键解析到 root 内并复核父目录符号链接。"""

        key = _normalize_object_key(object_key)
        target = self._root.joinpath(*key.split("/"))
        try:
            parent = target.parent
            if create_parent:
                parent.mkdir(parents=True, exist_ok=True)
            current = self._root
            for part in Path(key).parts[:-1]:
                current = current / part
                if current.is_symlink():
                    # 任一父节点成为链接都会绕过 root 边界，必须在访问目标前拒绝。
                    raise FileContractError("file.integrity_failed")
            resolved_parent = parent.resolve(strict=True)
            if self._root != resolved_parent and self._root not in resolved_parent.parents:
                # 规范化父目录必须仍在固定 root 下，路径穿越不能依赖字符串前缀判断。
                raise FileContractError("file.integrity_failed")
            return resolved_parent / target.name
        # 已识别的链接或越界错误保持完整性语义，宿主 I/O 异常另行折叠。
        except FileContractError:
            raise
        except OSError:
            # 文件系统异常可能包含完整部署路径，禁止原样传播到日志或响应。
            raise FileContractError("file.storage_unavailable") from None


def file_object_key(file_id: object, blob_kind: object) -> str:
    """从固定 file ID 和 blob kind 生成不可预测对象键。

    参数：服务端生成的 file ID 与 original/extract。返回：严格内部对象键。客户端文件名
    不参与路径，避免路径穿越和同名覆盖。
    """

    if (
        type(file_id) is not str
        or re.fullmatch(r"file_[0-9a-f]{32}", file_id) is None
        or blob_kind not in {"original", "extract"}
    ):
        # 对象键只从服务端随机 file ID 和固定字段域生成，任何变体均拒绝。
        raise FileContractError("file.integrity_failed")
    key = f"objects/{file_id[5:7]}/{file_id}/{blob_kind}.bin"
    return _normalize_object_key(key)


def _normalize_object_key(value: object) -> str:
    """严格校验服务端对象键，拒绝任何非规范变体。"""

    if type(value) is not str or _OBJECT_KEY.fullmatch(value) is None:
        # 严格整串匹配防止路径穿越、分隔符变体和额外后缀进入宿主路径。
        raise FileContractError("file.integrity_failed")
    return value


def _unlink_quietly(path: Path) -> None:
    """清理 staging 文件且不把清理异常覆盖原始固定错误。"""

    try:
        path.unlink(missing_ok=True)
    # 清理失败由后续存储巡检发现；这里禁止记录含宿主路径的异常。
    except OSError:
        return
