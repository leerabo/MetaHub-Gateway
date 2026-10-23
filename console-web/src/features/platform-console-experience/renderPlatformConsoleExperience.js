import {
  bindConsoleExperienceInteractions,
  hydrateConsoleAssistantDraft,
  renderConsoleExperience,
} from "../console-experience/renderConsoleExperience.js";

export function renderPlatformConsoleExperience(state, view) {
  return renderConsoleExperience(state, {
    domain: "platform",
    view,
    // 平台总览只显示有数据的区域；空数据仍可从底部折叠区查看。
    collapseEmptyOverview: true,
  });
}

export function hydratePlatformConsoleAssistant(root, state) {
  return hydrateConsoleAssistantDraft(root, state);
}

export function bindPlatformConsoleExperience(root, handlers) {
  return bindConsoleExperienceInteractions(root, handlers);
}
