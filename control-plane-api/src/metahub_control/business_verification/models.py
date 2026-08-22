"""定义企业认证、审核、证据授权和开票资料的严格脱敏 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VerificationModel(BaseModel):
    """统一拒绝未知字段，避免敏感内部列被宽松投影到 HTTP。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceSubmission(VerificationModel):
    """接收上传服务签发的证据描述；对象引用只在本次提交调用栈内短暂存在。"""

    evidence_type: Literal[
        "business_license", "registration_certificate", "authorization_letter"
    ]
    object_reference: str = Field(min_length=12, max_length=256, pattern=r"^obj_[A-Za-z0-9._:-]+$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: Literal["application/pdf", "image/png", "image/jpeg"]
    size_bytes: int = Field(ge=1, le=10_485_760)
    kms_key_version: int = Field(ge=1, le=1_000_000)
    malware_status: Literal["passed", "blocked", "pending", "failed"]
    dlp_status: Literal["passed", "blocked", "pending", "failed"]
    scan_attestation: str = Field(pattern=r"^[0-9a-f]{64}$")


class BusinessVerificationSubmitRequest(VerificationModel):
    """冻结法定主体与证据 manifest 的显式 Workspace 提交意图。"""

    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    entity_type: Literal["company", "nonprofit", "government"]
    legal_name: str = Field(min_length=2, max_length=240)
    registration_number: str = Field(min_length=3, max_length=128)
    evidence: list[EvidenceSubmission] = Field(min_length=1, max_length=5)
    reason_code: Literal["initial_submission", "resubmission"]
    confirmed: Literal[True]

    @model_validator(mode="after")
    def unique_evidence(self) -> BusinessVerificationSubmitRequest:
        """一个 intent 内不允许重复对象引用或证据类型，避免 manifest 双重解释。"""

        references = [item.object_reference for item in self.evidence]
        types = [item.evidence_type for item in self.evidence]
        # 同一 manifest 的类型或对象重复会造成审核语义歧义，必须在入库前失败关闭。
        if len(references) != len(set(references)) or len(types) != len(set(types)):
            raise ValueError("duplicate evidence")
        return self


class BillingProfileUpdateRequest(VerificationModel):
    """开票资料写 DTO 不接受 workspace/tenant/verification 状态等服务端 owner 字段。"""

    invoice_title: str = Field(min_length=2, max_length=240)
    tax_number: str = Field(min_length=3, max_length=128)
    address: str | None = Field(default=None, min_length=2, max_length=500)
    phone: str | None = Field(default=None, min_length=3, max_length=80)
    bank_name: str | None = Field(default=None, min_length=2, max_length=240)
    bank_account: str | None = Field(default=None, min_length=3, max_length=128)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def bank_fields_are_atomic(self) -> BillingProfileUpdateRequest:
        """银行名称与账号必须同时提供或同时清除，禁止产生半条银行资料。"""

        # 银行字段必须成对更新，避免掩码投影与密文事实出现不一致状态。
        if (self.bank_name is None) != (self.bank_account is None):
            raise ValueError("bank fields must be paired")
        return self


class VerificationReviewRequest(VerificationModel):
    """平台 reviewer 只能追加固定决定与原因，不接收客户资料或自由备注。"""

    decision: Literal["approve", "reject"]
    reason_code: Literal[
        "verified", "document_invalid", "identity_mismatch", "scan_failed", "profile_changed"
    ]
    confirmed: Literal[True]

    @model_validator(mode="after")
    def decision_reason_match(self) -> VerificationReviewRequest:
        """approve 只能使用 verified，reject 必须使用固定拒绝原因。"""

        # 决定与原因固定配对，防止审计记录出现“通过但拒绝原因”的矛盾事实。
        if (self.decision == "approve") != (self.reason_code == "verified"):
            raise ValueError("decision and reason do not match")
        return self


class LegalEntitySummary(VerificationModel):
    """Workspace 法定主体只返回配置状态、末四位掩码和 CAS revision。"""

    country_code: str
    entity_type: Literal["company", "nonprofit", "government"]
    legal_name_configured: Literal[True]
    registration_number_masked: str
    revision: int = Field(ge=1)
    updated_at: datetime


class EvidenceStatusSummary(VerificationModel):
    """压缩证据扫描状态，禁止返回对象引用、下载地址、文件名或密文。"""

    evidence_count: int = Field(ge=0, le=5)
    malware_status: Literal["passed", "blocked", "pending", "failed", "none"]
    dlp_status: Literal["passed", "blocked", "pending", "failed", "none"]


class VerificationIntentSummary(VerificationModel):
    """返回冻结 revision/hash 与固定状态，不携带登记号或审核内部备注。"""

    business_verification_intent_id: str
    status: Literal["submitted", "in_review", "approved", "rejected", "expired"]
    reason_code: str | None
    profile_revision: int = Field(ge=1)
    profile_sha256: str
    evidence_manifest_sha256: str
    evidence: EvidenceStatusSummary
    revision: int = Field(ge=1)
    submitted_at: datetime
    expires_at: datetime
    reviewed_at: datetime | None


class ContractProfileSummary(VerificationModel):
    """合同投影只暴露不可变版本引用、签约状态和有效期。"""

    contract_profile_id: str
    signing_status: Literal["verified", "signed", "expired"]
    immutable_version_reference: str
    effective_at: datetime
    expires_at: datetime | None


class BillingProfileSummary(VerificationModel):
    """开票资料响应只返回字段是否配置与两个敏感标识的末四位掩码。"""

    invoice_title_configured: Literal[True]
    tax_number_masked: str
    address_configured: bool
    phone_configured: bool
    bank_name_configured: bool
    bank_account_masked: str | None
    verification_status: Literal["unverified", "verified", "stale"]
    revision: int = Field(ge=1)
    updated_at: datetime


class WorkspaceBusinessVerificationResponse(VerificationModel):
    """组合 Workspace 当前法定主体、最新 intent、合同与开票资料的安全摘要。"""

    workspace_id: str
    legal_entity: LegalEntitySummary | None
    verification: VerificationIntentSummary | None
    contract: ContractProfileSummary | None
    billing_profile: BillingProfileSummary | None
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class BillingProfileResponse(VerificationModel):
    """返回开票资料 CAS revision 与掩码字段，不返回任何可恢复明文。"""

    workspace_id: str
    billing_profile: BillingProfileSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class PlatformEvidenceSummary(VerificationModel):
    """平台受控证据动作只获得技术 ID、类型、大小与扫描状态。"""

    verification_evidence_id: str
    evidence_type: Literal[
        "business_license", "registration_certificate", "authorization_letter"
    ]
    mime_type: Literal["application/pdf", "image/png", "image/jpeg"]
    size_bytes: int = Field(ge=1, le=10_485_760)
    malware_status: Literal["passed", "blocked", "pending", "failed"]
    dlp_status: Literal["passed", "blocked", "pending", "failed"]


class PlatformVerificationCaseSummary(VerificationModel):
    """平台审核队列只显示案件、主体类别、扫描聚合和冻结摘要。"""

    business_verification_intent_id: str
    country_code: str
    entity_type: Literal["company", "nonprofit", "government"]
    status: Literal["submitted", "in_review", "approved", "rejected", "expired"]
    reason_code: str | None
    profile_revision: int = Field(ge=1)
    profile_sha256: str
    evidence_manifest_sha256: str
    evidence: EvidenceStatusSummary
    evidence_items: tuple[PlatformEvidenceSummary, ...]
    revision: int = Field(ge=1)
    submitted_at: datetime
    expires_at: datetime


class PlatformVerificationListResponse(VerificationModel):
    """平台审核列表使用有界 offset 分页，不返回 Workspace 或 tenant 目录字段。"""

    cases: tuple[PlatformVerificationCaseSummary, ...]
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    total: int = Field(ge=0)
    request_id: str
    trace_id: str


class VerificationReviewResponse(VerificationModel):
    """审核结果只返回更新后的安全案件摘要和新合同引用。"""

    case: PlatformVerificationCaseSummary
    contract: ContractProfileSummary | None
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class EvidenceDownloadGrantResponse(VerificationModel):
    """一次性证据授权绑定当前 PlatformSession，token 只允许调用方内存中立即消费。"""

    verification_evidence_id: str
    download_token: str
    expires_at: datetime
    intent_revision: int = Field(ge=1)
    request_id: str
    trace_id: str
