import { DEFAULT_LOCALE, SUPPORTED_LOCALES, normalizeLocale } from "./messages.js";

// Language is a presentation preference only. It contains no account, tenant,
// credential, or server state and can safely follow the browser across entry pages.
export const LOCALE_STORAGE_KEY = "metahub.locale";
export const LOCALE_CHANGE_EVENT = "metahub:locale-change";

export function detectBrowserLocale(navigatorObject = globalThis.navigator) {
  const candidates = [
    ...(Array.isArray(navigatorObject?.languages) ? navigatorObject.languages : []),
    navigatorObject?.language,
    navigatorObject?.userLanguage,
  ];
  for (const value of candidates) {
    const language = String(value ?? "").trim().toLowerCase();
    if (language.startsWith("en")) return "en-US";
    if (language.startsWith("zh")) return "zh-CN";
  }
  return DEFAULT_LOCALE;
}

export function readPreferredLocale(storage = globalThis.localStorage) {
  let saved = "";
  try { saved = storage?.getItem?.(LOCALE_STORAGE_KEY) ?? ""; } catch { /* Storage can be disabled. */ }
  return SUPPORTED_LOCALES.includes(saved) ? saved : detectBrowserLocale();
}

export function writePreferredLocale(locale, {
  storage = globalThis.localStorage,
  dispatch = true,
} = {}) {
  const normalized = normalizeLocale(locale);
  try { storage?.setItem?.(LOCALE_STORAGE_KEY, normalized); } catch { /* Keep the active page usable. */ }
  applyDocumentLocale(normalized);
  if (dispatch) {
    globalThis.window?.dispatchEvent?.(new CustomEvent(LOCALE_CHANGE_EVENT, {
      detail: { locale: normalized },
    }));
  }
  return normalized;
}

export function applyDocumentLocale(locale, documentObject = globalThis.document) {
  const normalized = normalizeLocale(locale);
  documentObject?.documentElement?.setAttribute?.("lang", normalized);
  return normalized;
}

export function initializeLocale(documentObject = globalThis.document) {
  const locale = readPreferredLocale();
  applyDocumentLocale(locale, documentObject);
  return locale;
}

export function localeToggleLabel(locale) {
  return normalizeLocale(locale) === "en-US" ? "中文" : "English";
}
