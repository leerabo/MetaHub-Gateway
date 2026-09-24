/**
 * Keep implementation metadata out of ordinary console copy.
 *
 * Request/trace identifiers, database keys, hashes and concurrency terms are
 * still available to the API, logs and audit records. They are deliberately
 * projected out at the DOM boundary so an error response cannot turn into an
 * engineering diagnostic in the user interface.
 */

const INTERNAL_VALUE_PATTERN = /\b(?:request|trace|tenant|workspace|membership|member|session|owner|candidate|target|source|operator|account|user|provider|model|deployment|channel|rule(?:\s+snapshot)?)\s*[_ -]?id\s*[:：=]?\s*[A-Za-z0-9_.:/+-]*/giu;
const INTERNAL_FIELD_PATTERN = /\b(?:request|trace|tenant|workspace|membership|member|session|owner|candidate|target|source|operator|account|user|provider|model|deployment|channel|rule(?:\s+snapshot)?)\s*[_ -]?id\b|\b(?:idempotency[_ -]?key|fingerprint|(?:source|contract|catalog|manifest)\s+sha-?256|sha-?256|nonce|digest|revision|rev|etag|cas|cursor|watermark|generation|source\s+revision|error\s+code|request\s+ids?)\b|(?:请求|追踪|租户|组织|工作区|成员|会话|账户|用户|模型|部署|渠道|规则(?:快照)?)\s*(?:[_ -]?(?:id|ID|标识|编号))|(?:版本|修订|哈希|指纹|游标|水印|代次|错误码|请求\s*ID)/giu;
const INTERNAL_LABEL_PATTERN = /^(?:request|trace|tenant|workspace|membership|member|session|owner|candidate|target|source|operator|account|user|provider|model|deployment|channel|rule(?:\s+snapshot)?)\s*[_ -]?id\s*[:：]?$|^(?:request|trace|tenant|workspace|membership|member|session|owner|candidate|target|source|operator|account|user|provider|model|deployment|channel|rule(?:\s+snapshot)?)\s*[_ -]?id\s*[:：=]?\s*[A-Za-z0-9_.:/+-]+$|^(?:revision|rev|etag|cas|cursor|nonce|digest|fingerprint|(?:source|contract|catalog|manifest)\s+sha-?256|sha-?256|idempotency(?:[_ -]?key)?|error\s+code|request\s+ids?)\s*[:：]?$|^(?:请求|追踪|租户|组织|工作区|成员|会话|账户|用户|模型|部署|渠道|规则(?:快照)?)\s*(?:[_ -]?(?:id|ID|标识|编号))\s*[:：]?$|^(?:版本|修订|哈希|指纹|游标|水印|代次|错误码|请求\s*ID)\s*[:：]?$/iu;
const INTERNAL_CODE_PATTERN = /^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+$/u;
const REVISION_PATTERN = /\b(?:CAS\s*)?(?:Rev(?:ision)?|source\s+revision|generation|watermark)\s*[:#-]?\s*v?\d+\b|\br\d+\b/giu;
const HASH_VALUE_PATTERN = /(?:sha-?256|摘要)\s*[:：=]\s*[a-f0-9]{32,}/giu;
const INTERNAL_TOKEN_PATTERN = /\b(?:req|trc|ten|wsp|mbr|ses|usr|gac|prv|vky|dpl|dep|chn|rul|evt|job|inv|wbh|ast|cse)_[a-z0-9][a-z0-9_-]{3,}\b/giu;
const BARE_HASH_PATTERN = /\b[a-f0-9]{32,64}\b/giu;
const ERROR_CODE_PATTERN = /\b(?:code|error\s+code)\s*[:：=]\s*[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+\b/giu;
const DYNAMIC_ATTRIBUTE_NAMES = ["data-label", "aria-label", "title", "placeholder"];
const DYNAMIC_CONTENT_SELECTOR = [
  "[data-user-content='true']",
  ".user-identity", ".provider-name", ".model-name",
  "[data-dynamic-value='true']", "[data-technical-content='true']",
].join(",");

const WORD_REPLACEMENTS = [
  [/\bControl\s+Plane\b/giu, "管理服务"],
  [/\bControl\s+Console\b/giu, "管理控制台"],
  [/\bData\s+Plane\b/giu, "网关服务"],
  [/\bbackend\b/giu, "系统服务"],
  [/\bruntime\b/giu, "运行环境"],
  [/\bdebug(?:ging)?\b/giu, "排查"],
  [/\bservice\s+account\b/giu, "系统账号"],
  [/\blegacy\b/giu, "旧版"],
  [/\bControl\s+service\b/giu, "管理服务"],
  [/\bGateway\s+Data\s+Plane\b/giu, "网关服务"],
  [/\bProvider\b/giu, "模型服务"],
  [/\bAdapter\b/giu, "接入规范"],
  [/\bObservability\b/giu, "运行监测"],
  [/\bTenant\b/giu, "组织"],
  [/租户/gu, "组织"],
  [/错误码/gu, "错误提示"],
  [/请求追踪/gu, "操作记录"],
  [/\b(?:error\s+)?code\b/giu, "提示"],
  [/调试控制台|调试台/gu, "排查工具"],
  [/内部\s*FinOps/gu, "成本核算"],
  [/分润游标/gu, "分润翻页"],
  [/运行架构/gu, "部署方式"],
];

/** Remove internal metadata from already-rendered user-facing DOM. */
export function sanitizeUserFacingDom(root) {
  if (!root || typeof root.querySelectorAll !== "function") return root;

  sanitizeUserFacingAttributes(root);

  for (const element of root.querySelectorAll(
    "[class*='request-id'], [class*='trace-id'], [data-request-id-label], [data-trace-id-label]",
  )) {
    if (!isTechnicalElement(element)) {
      if (element.matches("button, a")) {
        element.textContent = "查看详情";
      } else {
        element.remove();
      }
    }
  }

  const walker = root.ownerDocument?.createTreeWalker?.(
    root,
    globalThis.NodeFilter?.SHOW_TEXT ?? 4,
  );
  if (!walker) return root;
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);
  for (const node of textNodes) sanitizeTextNode(node);

  for (const element of root.querySelectorAll("label, dt, th, code, small, span, p")) {
    if (isTechnicalElement(element)) continue;
    const text = String(element.textContent ?? "").trim();
    if (!text) {
      if (["CODE", "SMALL"].includes(element.tagName)) element.remove();
      continue;
    }
    if (INTERNAL_LABEL_PATTERN.test(text)) {
      element.hidden = true;
      continue;
    }
    if (INTERNAL_CODE_PATTERN.test(text) && element.closest("[role='alert'], .error-state, .error, [aria-live='polite']")) {
      element.textContent = "操作未完成";
    }
  }
  return root;
}

/** Install a single observer for all async page redraws under the console root. */
export function installUserFacingTextGuard(root) {
  if (!root || typeof MutationObserver === "undefined") return () => {};
  let queued = false;
  const schedule = () => {
    if (queued) return;
    queued = true;
    queueMicrotask(() => {
      queued = false;
      sanitizeUserFacingDom(root);
    });
  };
  sanitizeUserFacingDom(root);
  const observer = new MutationObserver(schedule);
  observer.observe(root, { childList: true, characterData: true, subtree: true });
  return () => observer.disconnect();
}

function sanitizeTextNode(node) {
  const parent = node.parentElement;
  if (!parent || isTechnicalElement(parent)) return;
  let value = String(node.nodeValue ?? "");
  const original = value;
  value = value.replace(INTERNAL_VALUE_PATTERN, "");
  value = value.replace(ERROR_CODE_PATTERN, "");
  value = value.replace(REVISION_PATTERN, "");
  value = value.replace(INTERNAL_FIELD_PATTERN, "");
  value = value.replace(HASH_VALUE_PATTERN, "");
  value = value.replace(INTERNAL_TOKEN_PATTERN, "");
  value = value.replace(BARE_HASH_PATTERN, "");
  if (parent.closest?.("[role='alert'], .error-state, .error, [aria-live='polite']")) {
    value = value.replace(INTERNAL_CODE_PATTERN, "");
  }
  if (!parent.closest(DYNAMIC_CONTENT_SELECTOR)) {
    for (const [pattern, replacement] of WORD_REPLACEMENTS) value = value.replace(pattern, replacement);
  }
  value = value.replace(/[ \t]{2,}/gu, " ");
  value = value.replace(/[ \t]*([·|])[ \t]*/gu, " $1 ");
  value = value.replace(/[ \t]*([：:，,。])[ \t]*/gu, "$1 ");
  value = value.replace(/[ \t]*[·|][ \t]*(?:[·|][ \t]*)+/gu, " ");
  value = value.replace(/^[ \t]*[·|:：][ \t]*|[ \t]*[·|:：][ \t]*$/gu, "").trim();
  if (value !== original) node.nodeValue = value;
}

function sanitizeUserFacingAttributes(root) {
  for (const element of root.querySelectorAll("*")) {
    for (const name of DYNAMIC_ATTRIBUTE_NAMES) {
      if (!element.hasAttribute?.(name)) continue;
      const value = String(element.getAttribute(name) ?? "");
      INTERNAL_FIELD_PATTERN.lastIndex = 0;
      if (!value || !INTERNAL_FIELD_PATTERN.test(value)) continue;
      if (name === "data-label" || name === "placeholder") {
        element.removeAttribute(name);
      } else if (name === "title" || name === "aria-label") {
        element.setAttribute(name, element.matches?.("button, a") ? "查看详情" : "详情");
      }
      INTERNAL_FIELD_PATTERN.lastIndex = 0;
    }
  }
}

function isTechnicalElement(element) {
  if (!element) return true;
  if (["PRE", "TEXTAREA", "SCRIPT", "STYLE"].includes(element.tagName)) return true;
  if (element.tagName === "CODE"
    && element.closest?.("pre, [data-technical-content='true']")) return true;
  return Boolean(element.closest?.("textarea, [data-technical-content='true']"));
}
