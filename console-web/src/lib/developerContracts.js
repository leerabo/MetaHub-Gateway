/** PRD-133 strict Developer Bearer and Platform Cookie clients. */

import { AuthApiError } from "./auth.js";
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

// Credential domains remain explicit at the transport boundary: legacy
// requests use credentials: "omit" and Workspace requests use credentials: "include".

const IDS = Object.freeze({
  apiVersion: /^apv_[0-9a-f]{32}$/u,
  sdkRelease: /^sdr_[0-9a-f]{32}$/u,
  deprecation: /^dpn_[0-9a-f]{32}$/u,
  webhookSchema: /^wsv_[0-9a-f]{32}$/u,
  event: /^dce_[0-9a-f]{32}$/u,
});
const SEMVER = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/u;
const SHA256 = /^[0-9a-f]{64}$/u;
const ED25519_PUBLIC_KEY = /^[A-Za-z0-9_-]{43}$/u;
const ED25519_SIGNATURE = /^[A-Za-z0-9_-]{86}$/u;
const SAFE_CODE = /^[a-z0-9][a-z0-9._-]{1,79}$/u;
const SAFE_OPERATION = /^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$/u;
const SAFE_TRACE = /^[A-Za-z0-9_.:-]{1,160}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const CURSOR = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/u;
const DOWNLOAD_PATH = /^\/developer\/sdks\/(python|javascript)\/([^/]+)\/download$/u;
const API_VERSION_KEYS = [
  "api_version_id", "version", "registry_revision", "status", "revision",
  "contract_sha256", "compatibility_baseline_sha256", "compatibility_outcome",
  "breaking_reasons", "created_at", "published_at", "deprecated_at", "sunset_at",
];
const SDK_KEYS = [
  "sdk_release_id", "api_version", "language", "sdk_version", "runtime_version",
  "contract_sha256", "artifact_sha256", "artifact_receipt_sha256", "file_manifest",
  "smoke_matrix", "status", "revision", "signing_key_id", "signature_sha256",
  "signature_b64", "public_key_b64", "download_url", "created_at", "published_at",
];
const DEPRECATION_KEYS = [
  "deprecation_notice_id", "api_version_id", "api_version", "replacement_api_version_id",
  "replacement_api_version", "affected_operations", "migration_guide_code", "status",
  "minimum_notice_days", "adoption_watermark", "notification_watermark", "sunset_at",
  "revision", "created_at", "announced_at", "decided_at",
];
const EVENT_METADATA_KEYS = Object.freeze({
  "api.deprecated": [
    "api_version", "migration_guide_code", "notice_id", "operation_count",
    "replacement_version", "sunset_at",
  ],
  "api.sunset_scheduled": [
    "api_version", "notice_id", "notification_watermark", "sunset_at",
  ],
  "sdk.released": ["api_version", "artifact_sha256", "language", "sdk_version"],
  "webhook.schema_published": [
    "api_version", "event_name", "schema_sha256", "schema_version",
  ],
});

export async function getDeveloperContractWorkspace({ controlApiUrl, authentication, token, signal } = {}) {
  const bearer = developerCredentials(authentication, token);
  const [versions, changelog, schemas, python, javascript] = await Promise.all([
    developerJson(controlApiUrl, "/developer/api-versions", bearer, signal),
    developerJson(controlApiUrl, "/developer/changelog?limit=100", bearer, signal),
    developerJson(controlApiUrl, "/developer/webhook-schemas", bearer, signal),
    developerJson(controlApiUrl, "/developer/sdks/python", bearer, signal),
    developerJson(controlApiUrl, "/developer/sdks/javascript", bearer, signal),
  ]);
  const normalizedVersions = apiVersionList(versions);
  const normalizedChangelog = changelogResponse(changelog);
  const normalizedSchemas = webhookSchemaList(schemas);
  const pythonReleases = sdkReleaseList(python, "python");
  const javascriptReleases = sdkReleaseList(javascript, "javascript");
  return Object.freeze({
    versions: normalizedVersions.versions,
    changelog: normalizedChangelog,
    schemas: normalizedSchemas.schemas,
    sdk_releases: Object.freeze([...pythonReleases.releases, ...javascriptReleases.releases]),
    request_ids: Object.freeze([
      normalizedVersions.request_id,
      normalizedChangelog.request_id,
      normalizedSchemas.request_id,
      pythonReleases.request_id,
      javascriptReleases.request_id,
    ]),
  });
}

export async function downloadDeveloperSdk({
  controlApiUrl,
  token,
  authentication,
  language,
  sdkVersion,
  contractSha256,
  artifactSha256,
  artifactReceiptSha256,
  signatureB64,
  signatureSha256,
  publicKeyB64,
  signal,
} = {}) {
  const checkedLanguage = choice(language, ["python", "javascript"], "language", "developer");
  const checkedVersion = clientPattern(sdkVersion, SEMVER, "sdk_version", "developer");
  const expectedArtifact = clientPattern(
    artifactSha256, SHA256, "artifact_sha256", "developer",
  );
  const expectedContract = clientPattern(
    contractSha256, SHA256, "contract_sha256", "developer",
  );
  const expectedReceipt = clientPattern(
    artifactReceiptSha256, SHA256, "artifact_receipt_sha256", "developer",
  );
  const expectedSignature = clientPattern(
    signatureB64, ED25519_SIGNATURE, "signature_b64", "developer",
  );
  const expectedSignatureSha = clientPattern(
    signatureSha256, SHA256, "signature_sha256", "developer",
  );
  const expectedPublicKey = clientPattern(
    publicKeyB64, ED25519_PUBLIC_KEY, "public_key_b64", "developer",
  );
  let response;
  try {
    response = await authenticatedResourceFetch(apiUrl(
      controlApiUrl,
      `/developer/sdks/${checkedLanguage}/${checkedVersion}/download`,
      "developer",
    ), {
      ...developerCredentials(authentication, token),
      csrfRequired: true,
      headers: { Accept: "application/zip" },
      cache: "no-store",
      redirect: "error",
      signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw apiError("developer", 0, "developer_contract.network_unavailable", "Service unavailable");
  }
  if (!response.ok) throw await responseError(response, "developer");
  requirePrivateNoStore(response, "developer");
  const contentType = response.headers.get("Content-Type") ?? "";
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const digest = response.headers.get("Digest") ?? "";
  const receipt = response.headers.get("X-Artifact-Receipt-SHA256") ?? "";
  const filename = `metahub-${checkedLanguage}-sdk-${checkedVersion}.zip`;
  if (!/^application\/zip(?:\s*;|$)/iu.test(contentType)
    || disposition !== `attachment; filename="${filename}"`
    || response.headers.get("X-Content-Type-Options")?.toLowerCase() !== "nosniff"
    || digest !== `sha-256=${hexToBase64(expectedArtifact)}`
    || receipt !== expectedReceipt) invalidResponse("developer", "sdk_download_headers");
  const blob = await response.blob();
  const length = response.headers.get("Content-Length");
  if (blob.size < 1 || (length !== null && Number(length) !== blob.size)
    || await blobSha256(blob) !== expectedArtifact) {
    invalidResponse("developer", "sdk_download_digest");
  }
  const verified = await verifySdkSignature({
    language: checkedLanguage,
    sdkVersion: checkedVersion,
    contractSha256: expectedContract,
    artifactSha256: expectedArtifact,
    signatureB64: expectedSignature,
    signatureSha256: expectedSignatureSha,
    publicKeyB64: expectedPublicKey,
  });
  if (!verified) invalidResponse("developer", "sdk_download_signature");
  return Object.freeze({
    blob,
    filename,
    artifact_sha256: expectedArtifact,
    artifact_receipt_sha256: expectedReceipt,
    request_id: response.headers.get("X-Request-ID") ?? "-",
  });
}

export async function getPlatformDeveloperContracts({ controlApiUrl, signal } = {}) {
  const result = await platformJson(controlApiUrl, "/platform/developer-contracts", { signal });
  return platformWorkspace(result);
}

export async function buildPlatformDeveloperContract({
  controlApiUrl,
  apiVersion,
  pythonSdkVersion,
  javascriptSdkVersion,
  producerRevision,
  confirmed,
  idempotencyKey,
  signal,
} = {}) {
  if (confirmed !== true) invalidRequest("platform", "confirmed");
  const payload = await platformJson(controlApiUrl, "/platform/developer-contracts/build", {
    method: "POST",
    idempotencyKey: clientIdempotency(idempotencyKey, "platform"),
    body: {
      api_version: clientPattern(apiVersion, SEMVER, "api_version", "platform"),
      python_sdk_version: clientPattern(
        pythonSdkVersion, SEMVER, "python_sdk_version", "platform",
      ),
      javascript_sdk_version: clientPattern(
        javascriptSdkVersion, SEMVER, "javascript_sdk_version", "platform",
      ),
      producer_revision: clientInteger(producerRevision, 1, "producer_revision", "platform"),
      confirmed: true,
    },
    signal,
  });
  return buildResponse(payload);
}

export async function approvePlatformDeveloperContract({
  controlApiUrl,
  apiVersionId,
  expectedRevision,
  confirmed,
  idempotencyKey,
  signal,
} = {}) {
  if (confirmed !== true) invalidRequest("platform", "confirmed");
  const id = clientPattern(apiVersionId, IDS.apiVersion, "api_version_id", "platform");
  const payload = await platformJson(
    controlApiUrl,
    `/platform/developer-contracts/${encodeURIComponent(id)}/approve`,
    {
      method: "POST",
      idempotencyKey: clientIdempotency(idempotencyKey, "platform"),
      body: {
        expected_revision: clientInteger(expectedRevision, 1, "expected_revision", "platform"),
        confirmed: true,
      },
      signal,
    },
  );
  return buildResponse(payload);
}

export async function createPlatformDeprecation({
  controlApiUrl,
  apiVersionId,
  replacementApiVersionId,
  affectedOperations,
  migrationGuideCode,
  minimumNoticeDays = 90,
  sunsetAt,
  confirmed,
  idempotencyKey,
  signal,
} = {}) {
  if (confirmed !== true) invalidRequest("platform", "confirmed");
  const sourceId = clientPattern(apiVersionId, IDS.apiVersion, "api_version_id", "platform");
  const replacementId = clientPattern(
    replacementApiVersionId, IDS.apiVersion, "replacement_api_version_id", "platform",
  );
  if (sourceId === replacementId) invalidRequest("platform", "replacement_api_version_id");
  const operations = clientOperations(affectedOperations, "platform");
  const payload = await platformJson(controlApiUrl, "/platform/developer-contracts/deprecations", {
    method: "POST",
    idempotencyKey: clientIdempotency(idempotencyKey, "platform"),
    body: {
      api_version_id: sourceId,
      replacement_api_version_id: replacementId,
      affected_operations: operations,
      migration_guide_code: clientPattern(
        migrationGuideCode, SAFE_CODE, "migration_guide_code", "platform",
      ),
      minimum_notice_days: clientInteger(
        minimumNoticeDays, 90, "minimum_notice_days", "platform", 730,
      ),
      sunset_at: clientTimestamp(sunsetAt, "sunset_at", "platform"),
      confirmed: true,
    },
    signal,
  });
  return mutationResponse(payload);
}

export async function mutatePlatformDeprecation({
  controlApiUrl,
  noticeId,
  action,
  expectedRevision,
  notificationWatermark = null,
  adoptionWatermark = null,
  confirmed,
  idempotencyKey,
  signal,
} = {}) {
  if (confirmed !== true) invalidRequest("platform", "confirmed");
  const id = clientPattern(noticeId, IDS.deprecation, "notice_id", "platform");
  const operation = choice(action, ["announce", "withdraw", "sunset"], "action", "platform");
  const payload = await platformJson(
    controlApiUrl,
    `/platform/developer-contracts/deprecations/${encodeURIComponent(id)}/${operation}`,
    {
      method: "POST",
      idempotencyKey: clientIdempotency(idempotencyKey, "platform"),
      body: {
        expected_revision: clientInteger(expectedRevision, 1, "expected_revision", "platform"),
        notification_watermark: nullableClientInteger(
          notificationWatermark, 0, "notification_watermark", "platform",
        ),
        adoption_watermark: nullableClientInteger(
          adoptionWatermark, 0, "adoption_watermark", "platform",
        ),
        confirmed: true,
      },
      signal,
    },
  );
  return mutationResponse(payload);
}

export async function withdrawPlatformSdk({
  controlApiUrl,
  sdkReleaseId,
  expectedRevision,
  confirmed,
  idempotencyKey,
  signal,
} = {}) {
  if (confirmed !== true) invalidRequest("platform", "confirmed");
  const id = clientPattern(sdkReleaseId, IDS.sdkRelease, "sdk_release_id", "platform");
  const payload = await platformJson(
    controlApiUrl,
    `/platform/developer-contracts/sdks/${encodeURIComponent(id)}/withdraw`,
    {
      method: "POST",
      idempotencyKey: clientIdempotency(idempotencyKey, "platform"),
      body: {
        expected_revision: clientInteger(expectedRevision, 1, "expected_revision", "platform"),
        confirmed: true,
      },
      signal,
    },
  );
  return mutationResponse(payload);
}

function apiVersionList(value) {
  exact(value, ["versions", "request_id", "trace_id"], "api_version_list", "developer");
  array(value.versions, 100, "versions", "developer");
  const versions = value.versions.map(apiVersion);
  unique(versions.map((item) => item.api_version_id), "developer", "versions");
  return Object.freeze({ versions: Object.freeze(versions), ...trace(value, "developer") });
}

function changelogResponse(value) {
  exact(value, ["entries", "next_cursor", "request_id", "trace_id"], "changelog", "developer");
  array(value.entries, 100, "entries", "developer");
  const entries = value.entries.map(changelogEntry);
  unique(entries.map((item) => item.event_id), "developer", "entries");
  const nextCursor = value.next_cursor === null
    ? null : responsePattern(value.next_cursor, CURSOR, "next_cursor", "developer");
  return Object.freeze({
    entries: Object.freeze(entries),
    next_cursor: nextCursor,
    ...trace(value, "developer"),
  });
}

function webhookSchemaList(value) {
  exact(value, ["schemas", "request_id", "trace_id"], "webhook_schema_list", "developer");
  array(value.schemas, 500, "schemas", "developer");
  const schemas = value.schemas.map(webhookSchema);
  unique(schemas.map((item) => item.webhook_schema_version_id), "developer", "schemas");
  return Object.freeze({ schemas: Object.freeze(schemas), ...trace(value, "developer") });
}

function sdkReleaseList(value, expectedLanguage) {
  exact(value, ["releases", "request_id", "trace_id"], "sdk_release_list", "developer");
  array(value.releases, 100, "releases", "developer");
  const releases = value.releases.map(sdkRelease);
  if (releases.some((item) => item.language !== expectedLanguage)) {
    invalidResponse("developer", "sdk_language_owner");
  }
  unique(releases.map((item) => item.sdk_release_id), "developer", "releases");
  return Object.freeze({ releases: Object.freeze(releases), ...trace(value, "developer") });
}

function platformWorkspace(value) {
  exact(value, ["versions", "sdk_releases", "deprecations", "request_id", "trace_id"],
    "platform_workspace", "platform");
  array(value.versions, 100, "versions", "platform");
  array(value.sdk_releases, 200, "sdk_releases", "platform");
  array(value.deprecations, 100, "deprecations", "platform");
  const versions = value.versions.map((item) => apiVersion(item, "platform"));
  const releases = value.sdk_releases.map((item) => sdkRelease(item, "platform"));
  const deprecations = value.deprecations.map(deprecation);
  unique(versions.map((item) => item.api_version_id), "platform", "versions");
  unique(releases.map((item) => item.sdk_release_id), "platform", "sdk_releases");
  unique(deprecations.map((item) => item.deprecation_notice_id), "platform", "deprecations");
  return Object.freeze({
    versions: Object.freeze(versions),
    sdk_releases: Object.freeze(releases),
    deprecations: Object.freeze(deprecations),
    ...trace(value, "platform"),
  });
}

function buildResponse(value) {
  exact(value, ["api_version", "sdk_releases", "idempotent_replay", "request_id", "trace_id"],
    "build_response", "platform");
  array(value.sdk_releases, 2, "sdk_releases", "platform");
  if (value.sdk_releases.length !== 2 || typeof value.idempotent_replay !== "boolean") {
    invalidResponse("platform", "build_response");
  }
  const version = apiVersion(value.api_version, "platform");
  const releases = value.sdk_releases.map((item) => sdkRelease(item, "platform"));
  if (releases.some((item) => item.api_version !== version.version)
    || new Set(releases.map((item) => item.language)).size !== 2) {
    invalidResponse("platform", "build_owner");
  }
  return Object.freeze({
    api_version: version,
    sdk_releases: Object.freeze(releases),
    idempotent_replay: value.idempotent_replay,
    ...trace(value, "platform"),
  });
}

function mutationResponse(value) {
  exact(value, [
    "api_version", "deprecation", "sdk_release", "idempotent_replay", "request_id", "trace_id",
  ], "mutation_response", "platform");
  if (typeof value.idempotent_replay !== "boolean") {
    invalidResponse("platform", "idempotent_replay");
  }
  const api = value.api_version === null ? null : apiVersion(value.api_version, "platform");
  const notice = value.deprecation === null ? null : deprecation(value.deprecation);
  const sdk = value.sdk_release === null ? null : sdkRelease(value.sdk_release, "platform");
  if ([api, notice, sdk].filter(Boolean).length < 1) invalidResponse("platform", "mutation_target");
  return Object.freeze({
    api_version: api,
    deprecation: notice,
    sdk_release: sdk,
    idempotent_replay: value.idempotent_replay,
    ...trace(value, "platform"),
  });
}

function apiVersion(value, domain = "developer") {
  exact(value, API_VERSION_KEYS, "api_version", domain);
  array(value.breaking_reasons, 128, "breaking_reasons", domain);
  const status = responseChoice(
    value.status, ["pending_approval", "published", "deprecated", "sunset"], "status", domain,
  );
  const result = {
    api_version_id: responsePattern(value.api_version_id, IDS.apiVersion, "api_version_id", domain),
    version: responsePattern(value.version, SEMVER, "version", domain),
    registry_revision: responseInteger(value.registry_revision, 1, "registry_revision", domain),
    status,
    revision: responseInteger(value.revision, 1, "revision", domain),
    contract_sha256: responsePattern(value.contract_sha256, SHA256, "contract_sha256", domain),
    compatibility_baseline_sha256: nullablePattern(
      value.compatibility_baseline_sha256, SHA256, "compatibility_baseline_sha256", domain,
    ),
    compatibility_outcome: responseChoice(
      value.compatibility_outcome, ["compatible", "breaking"], "compatibility_outcome", domain,
    ),
    breaking_reasons: Object.freeze(value.breaking_reasons.map((item) => (
      responseText(item, 240, "breaking_reason", domain)
    ))),
    created_at: timestamp(value.created_at, "created_at", domain),
    published_at: nullableTimestamp(value.published_at, "published_at", domain),
    deprecated_at: nullableTimestamp(value.deprecated_at, "deprecated_at", domain),
    sunset_at: nullableTimestamp(value.sunset_at, "sunset_at", domain),
  };
  if (status === "pending_approval" && result.published_at !== null) {
    invalidResponse(domain, "published_at");
  }
  if (status !== "pending_approval" && result.published_at === null) {
    invalidResponse(domain, "published_at");
  }
  return Object.freeze(result);
}

function sdkRelease(value, domain = "developer") {
  exact(value, SDK_KEYS, "sdk_release", domain);
  const language = responseChoice(value.language, ["python", "javascript"], "language", domain);
  const version = responsePattern(value.sdk_version, SEMVER, "sdk_version", domain);
  const status = responseChoice(
    value.status, ["pending_approval", "published", "withdrawn"], "status", domain,
  );
  const manifest = fileManifest(value.file_manifest, domain);
  const smoke = smokeMatrix(value.smoke_matrix, value.api_version, domain);
  const result = {
    sdk_release_id: responsePattern(value.sdk_release_id, IDS.sdkRelease, "sdk_release_id", domain),
    api_version: responsePattern(value.api_version, SEMVER, "api_version", domain),
    language,
    sdk_version: version,
    runtime_version: responseText(value.runtime_version, 64, "runtime_version", domain),
    contract_sha256: responsePattern(value.contract_sha256, SHA256, "contract_sha256", domain),
    artifact_sha256: responsePattern(value.artifact_sha256, SHA256, "artifact_sha256", domain),
    artifact_receipt_sha256: responsePattern(
      value.artifact_receipt_sha256, SHA256, "artifact_receipt_sha256", domain,
    ),
    file_manifest: manifest,
    smoke_matrix: smoke,
    status,
    revision: responseInteger(value.revision, 1, "revision", domain),
    signing_key_id: nullablePattern(value.signing_key_id, SAFE_CODE, "signing_key_id", domain),
    signature_b64: nullablePattern(
      value.signature_b64, ED25519_SIGNATURE, "signature_b64", domain,
    ),
    signature_sha256: nullablePattern(value.signature_sha256, SHA256, "signature_sha256", domain),
    public_key_b64: nullablePattern(
      value.public_key_b64, ED25519_PUBLIC_KEY, "public_key_b64", domain,
    ),
    download_url: nullableText(value.download_url, 180, "download_url", domain),
    created_at: timestamp(value.created_at, "created_at", domain),
    published_at: nullableTimestamp(value.published_at, "published_at", domain),
  };
  const expectedDownload = `/developer/sdks/${language}/${version}/download`;
  if (status === "published") {
    if (result.download_url !== expectedDownload || !DOWNLOAD_PATH.test(result.download_url)
      || result.published_at === null || result.signing_key_id === null
      || result.signature_b64 === null || result.signature_sha256 === null
      || result.public_key_b64 === null) {
      invalidResponse(domain, "published_sdk_proof");
    }
  } else if (result.download_url !== null) {
    invalidResponse(domain, "download_url");
  }
  return Object.freeze(result);
}

function deprecation(value) {
  exact(value, DEPRECATION_KEYS, "deprecation", "platform");
  array(value.affected_operations, 128, "affected_operations", "platform");
  if (value.affected_operations.length < 1) invalidResponse("platform", "affected_operations");
  const operations = value.affected_operations.map((item) => (
    responsePattern(item, SAFE_OPERATION, "affected_operation", "platform")
  ));
  unique(operations, "platform", "affected_operations");
  return Object.freeze({
    deprecation_notice_id: responsePattern(
      value.deprecation_notice_id, IDS.deprecation, "deprecation_notice_id", "platform",
    ),
    api_version_id: responsePattern(
      value.api_version_id, IDS.apiVersion, "api_version_id", "platform",
    ),
    api_version: responsePattern(value.api_version, SEMVER, "api_version", "platform"),
    replacement_api_version_id: responsePattern(
      value.replacement_api_version_id, IDS.apiVersion, "replacement_api_version_id", "platform",
    ),
    replacement_api_version: responsePattern(
      value.replacement_api_version, SEMVER, "replacement_api_version", "platform",
    ),
    affected_operations: Object.freeze(operations),
    migration_guide_code: responsePattern(
      value.migration_guide_code, SAFE_CODE, "migration_guide_code", "platform",
    ),
    status: responseChoice(value.status, [
      "draft", "announced", "sunset_scheduled", "sunset", "withdrawn",
    ], "status", "platform"),
    minimum_notice_days: responseInteger(
      value.minimum_notice_days, 90, "minimum_notice_days", "platform", 730,
    ),
    adoption_watermark: responseInteger(
      value.adoption_watermark, 0, "adoption_watermark", "platform",
    ),
    notification_watermark: responseInteger(
      value.notification_watermark, 0, "notification_watermark", "platform",
    ),
    sunset_at: timestamp(value.sunset_at, "sunset_at", "platform"),
    revision: responseInteger(value.revision, 1, "revision", "platform"),
    created_at: timestamp(value.created_at, "created_at", "platform"),
    announced_at: nullableTimestamp(value.announced_at, "announced_at", "platform"),
    decided_at: nullableTimestamp(value.decided_at, "decided_at", "platform"),
  });
}

function changelogEntry(value) {
  exact(value, [
    "event_id", "event_type", "source_object_type", "source_object_id",
    "source_revision", "metadata", "occurred_at",
  ], "changelog_entry", "developer");
  const type = responseChoice(value.event_type, Object.keys(EVENT_METADATA_KEYS),
    "event_type", "developer");
  const objectType = responseChoice(value.source_object_type, [
    "api_version", "deprecation_notice", "sdk_release", "webhook_schema_version",
  ], "source_object_type", "developer");
  const objectPattern = {
    api_version: IDS.apiVersion,
    deprecation_notice: IDS.deprecation,
    sdk_release: IDS.sdkRelease,
    webhook_schema_version: IDS.webhookSchema,
  }[objectType];
  exact(value.metadata, EVENT_METADATA_KEYS[type], "event_metadata", "developer");
  const metadata = {};
  for (const [key, item] of Object.entries(value.metadata)) {
    if ((typeof item !== "string" && !Number.isSafeInteger(item))
      || (typeof item === "string" && (item.length < 1 || item.length > 240))) {
      invalidResponse("developer", "event_metadata");
    }
    metadata[key] = item;
  }
  if (type === "api.deprecated"
    && !SAFE_CODE.test(String(metadata.migration_guide_code ?? ""))) {
    invalidResponse("developer", "migration_guide_code");
  }
  return Object.freeze({
    event_id: responsePattern(value.event_id, IDS.event, "event_id", "developer"),
    event_type: type,
    source_object_type: objectType,
    source_object_id: responsePattern(
      value.source_object_id, objectPattern, "source_object_id", "developer",
    ),
    source_revision: responseInteger(
      value.source_revision, 1, "source_revision", "developer",
    ),
    metadata: Object.freeze(metadata),
    occurred_at: timestamp(value.occurred_at, "occurred_at", "developer"),
  });
}

function webhookSchema(value) {
  exact(value, [
    "webhook_schema_version_id", "api_version", "event_name", "schema_version",
    "json_schema", "schema_sha256", "sample_sha256", "published_at",
  ], "webhook_schema", "developer");
  const eventName = responseChoice(
    value.event_name, Object.keys(EVENT_METADATA_KEYS), "event_name", "developer",
  );
  validateJson(value.json_schema, 0, "developer");
  return Object.freeze({
    webhook_schema_version_id: responsePattern(
      value.webhook_schema_version_id, IDS.webhookSchema, "webhook_schema_version_id", "developer",
    ),
    api_version: responsePattern(value.api_version, SEMVER, "api_version", "developer"),
    event_name: eventName,
    schema_version: responseInteger(value.schema_version, 1, "schema_version", "developer"),
    json_schema: Object.freeze({ ...value.json_schema }),
    schema_sha256: responsePattern(value.schema_sha256, SHA256, "schema_sha256", "developer"),
    sample_sha256: responsePattern(value.sample_sha256, SHA256, "sample_sha256", "developer"),
    published_at: timestamp(value.published_at, "published_at", "developer"),
  });
}

function fileManifest(value, domain) {
  plain(value, "file_manifest", domain);
  const entries = Object.entries(value);
  if (entries.length < 1 || entries.length > 32) invalidResponse(domain, "file_manifest");
  const result = {};
  for (const [path, digest] of entries) {
    if (!/^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*$/u.test(path)
      || path.includes("..") || !SHA256.test(String(digest ?? ""))) {
      invalidResponse(domain, "file_manifest");
    }
    result[path] = digest;
  }
  return Object.freeze(result);
}

function smokeMatrix(value, apiVersionValue, domain) {
  exact(value, ["contract_versions", "linux", "windows"], "smoke_matrix", domain);
  if (!Array.isArray(value.contract_versions) || value.contract_versions.length !== 2
    || value.contract_versions[0] !== apiVersionValue || value.contract_versions[1] !== "N-1"
    || value.linux !== "passed" || value.windows !== "passed") {
    invalidResponse(domain, "smoke_matrix");
  }
  return Object.freeze({
    contract_versions: Object.freeze([...value.contract_versions]),
    linux: "passed",
    windows: "passed",
  });
}

async function developerJson(controlApiUrl, path, credentials, signal) {
  let response;
  try {
    response = await authenticatedResourceFetch(apiUrl(controlApiUrl, path, "developer"), {
      ...credentials,
      headers: { Accept: "application/json" },
      cache: "no-store",
      redirect: "error",
      signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw apiError("developer", 0, "developer_contract.network_unavailable", "Service unavailable");
  }
  const payload = await readJson(response, "developer");
  if (!response.ok) throw serverError(response, payload, "developer");
  requirePrivateNoStore(response, "developer");
  return payload;
}

async function platformJson(controlApiUrl, path, options = {}) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = readPlatformCsrfCookie();
  }
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path, "platform"), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      redirect: "error",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw apiError("platform", 0, "developer_contract.network_unavailable", "Service unavailable");
  }
  const payload = await readJson(response, "platform");
  if (!response.ok) throw serverError(response, payload, "platform");
  requirePrivateNoStore(response, "platform");
  return payload;
}

async function responseError(response, domain) {
  const payload = await readJson(response, domain);
  return serverError(response, payload, domain);
}

function serverError(response, payload, domain) {
  const code = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u.test(String(payload?.code ?? ""))
    ? payload.code : "developer_contract.request_failed";
  return apiError(
    domain,
    response.status,
    code,
    safeText(payload?.message) || "Request failed",
    safeText(payload?.request_id) || "-",
    safeText(payload?.trace_id) || "-",
  );
}

function requirePrivateNoStore(response, domain) {
  const directives = String(response.headers.get("Cache-Control") ?? "")
    .toLowerCase().split(",").map((item) => item.trim());
  if (!directives.includes("no-store") || !directives.includes("private")) {
    invalidResponse(domain, "cache_control");
  }
}

async function readJson(response, domain) {
  const type = response.headers.get("Content-Type") ?? "";
  if (!/^application\/json(?:\s*;|$)/iu.test(type)) invalidResponse(domain, "content_type");
  try {
    const value = await response.json();
    plain(value, "payload", domain);
    return value;
  } catch (error) {
    if (error instanceof AuthApiError || error instanceof PlatformApiError) throw error;
    invalidResponse(domain, "json");
  }
}

function trace(value, domain) {
  return {
    request_id: responsePattern(value.request_id, SAFE_TRACE, "request_id", domain),
    trace_id: responsePattern(value.trace_id, SAFE_TRACE, "trace_id", domain),
  };
}

function validateJson(value, depth, domain) {
  if (depth > 12) invalidResponse(domain, "json_schema_depth");
  if (value === null || typeof value === "boolean" || typeof value === "string"
    || (typeof value === "number" && Number.isFinite(value))) return;
  if (Array.isArray(value)) {
    if (value.length > 256) invalidResponse(domain, "json_schema_array");
    value.forEach((item) => validateJson(item, depth + 1, domain));
    return;
  }
  plain(value, "json_schema_object", domain);
  if (Object.keys(value).length > 256) invalidResponse(domain, "json_schema_object");
  Object.values(value).forEach((item) => validateJson(item, depth + 1, domain));
}

function clientOperations(value, domain) {
  if (!Array.isArray(value) || value.length < 1 || value.length > 128) {
    invalidRequest(domain, "affected_operations");
  }
  const result = value.map((item) => clientPattern(
    String(item ?? "").trim(), SAFE_OPERATION, "affected_operation", domain,
  ));
  if (new Set(result).size !== result.length) invalidRequest(domain, "affected_operations");
  return result;
}

function developerToken(value) {
  const token = String(value ?? "");
  if (token.length < 16 || token.length > 8192 || /[\s\u0000-\u001f\u007f]/u.test(token)) {
    invalidRequest("developer", "token");
  }
  return token;
}

function developerCredentials(authentication, token) {
  if (authentication === "workspace") {
    if (token !== undefined && token !== null) {
      throw new AuthApiError({ status: 401, code: "workspace_session.credential_conflict", message: "Authentication context is unavailable." });
    }
    return { authentication };
  }
  if (authentication !== undefined && authentication !== "legacy") invalidRequest("developer", "authentication");
  return { authentication, token: developerToken(token) };
}

function clientTimestamp(value, label, domain) {
  const text = String(value ?? "");
  if (text.length > 40 || !Number.isFinite(Date.parse(text))) invalidRequest(domain, label);
  return new Date(text).toISOString();
}

function nullableClientInteger(value, minimum, label, domain) {
  return value === null || value === "" ? null : clientInteger(value, minimum, label, domain);
}

function clientInteger(value, minimum, label, domain, maximum = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidRequest(domain, label);
  }
  return value;
}

function clientIdempotency(value, domain) {
  return clientPattern(value, IDEMPOTENCY, "idempotency_key", domain);
}

function clientPattern(value, pattern, label, domain) {
  const text = String(value ?? "");
  if (!pattern.test(text)) invalidRequest(domain, label);
  return text;
}

function responsePattern(value, pattern, label, domain) {
  const text = responseText(value, 2048, label, domain);
  if (!pattern.test(text)) invalidResponse(domain, label);
  return text;
}

function nullablePattern(value, pattern, label, domain) {
  return value === null ? null : responsePattern(value, pattern, label, domain);
}

function responseChoice(value, choices, label, domain) {
  if (!choices.includes(value)) invalidResponse(domain, label);
  return value;
}

function choice(value, choices, label, domain) {
  if (!choices.includes(value)) invalidRequest(domain, label);
  return value;
}

function responseInteger(value, minimum, label, domain, maximum = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidResponse(domain, label);
  }
  return value;
}

function timestamp(value, label, domain) {
  const text = responseText(value, 40, label, domain);
  if (!Number.isFinite(Date.parse(text))) invalidResponse(domain, label);
  return text;
}

function nullableTimestamp(value, label, domain) {
  return value === null ? null : timestamp(value, label, domain);
}

function responseText(value, maximum, label, domain) {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) {
    invalidResponse(domain, label);
  }
  return value;
}

function nullableText(value, maximum, label, domain) {
  return value === null ? null : responseText(value, maximum, label, domain);
}

function exact(value, keys, label, domain) {
  plain(value, label, domain);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length
    || actual.some((key, index) => key !== expected[index])) invalidResponse(domain, label);
}

function plain(value, label, domain) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) invalidResponse(domain, label);
}

function array(value, maximum, label, domain) {
  if (!Array.isArray(value) || value.length > maximum) invalidResponse(domain, label);
}

function unique(values, domain, label) {
  if (new Set(values).size !== values.length) invalidResponse(domain, label);
}

function apiUrl(baseUrl, path, domain) {
  let base;
  try {
    base = new URL(String(baseUrl ?? ""));
  } catch {
    invalidRequest(domain, "control_api_url");
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password
    || base.search || base.hash || (base.pathname !== "/" && base.pathname !== "")) {
    invalidRequest(domain, "control_api_url");
  }
  return new URL(path, base).toString();
}

async function blobSha256(blob) {
  if (!globalThis.crypto?.subtle) invalidResponse("developer", "crypto_unavailable");
  const digest = await globalThis.crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function verifySdkSignature({
  language,
  sdkVersion,
  contractSha256,
  artifactSha256,
  signatureB64,
  signatureSha256,
  publicKeyB64,
}) {
  if (!globalThis.crypto?.subtle) invalidResponse("developer", "crypto_unavailable");
  try {
    const signature = base64UrlBytes(signatureB64);
    if (await bytesSha256(signature) !== signatureSha256) return false;
    const publicKey = await globalThis.crypto.subtle.importKey(
      "raw",
      base64UrlBytes(publicKeyB64),
      { name: "Ed25519" },
      false,
      ["verify"],
    );
    const payload = new TextEncoder().encode(JSON.stringify({
      artifact_sha256: artifactSha256,
      contract_sha256: contractSha256,
      language,
      purpose: "metahub.public-sdk.release/v1",
      sdk_version: sdkVersion,
    }));
    return await globalThis.crypto.subtle.verify(
      { name: "Ed25519" }, publicKey, signature, payload,
    );
  } catch {
    return false;
  }
}

async function bytesSha256(bytes) {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function base64UrlBytes(value) {
  const padded = value.replace(/-/gu, "+").replace(/_/gu, "/")
    + "=".repeat((4 - value.length % 4) % 4);
  const binary = globalThis.atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function hexToBase64(value) {
  const bytes = value.match(/.{2}/gu).map((pair) => Number.parseInt(pair, 16));
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return globalThis.btoa(binary);
}

function apiError(domain, status, code, message, requestId = "-", traceId = "-") {
  if (domain === "platform") {
    return new PlatformApiError({ status, code, message, requestId });
  }
  return new AuthApiError({ status, code, message, requestId, traceId });
}

function invalidRequest(domain, label) {
  throw apiError(domain, 422, "developer_contract.request_invalid", `Invalid ${label}`);
}

function invalidResponse(domain, label) {
  throw apiError(domain, 502, "developer_contract.response_invalid", `Invalid ${label}`);
}

function safeText(value) {
  return typeof value === "string"
    ? value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240)
    : "";
}
