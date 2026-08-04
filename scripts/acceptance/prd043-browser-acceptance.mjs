/**
 * PRD-043 用量 CSV 导出真实浏览器验收。
 *
 * 该脚本只通过公开控制台和控制面接口完成登录、角色可见性、真实下载与响应式布局验收。
 * 账号和密码必须通过环境变量注入，既不写入脚本、截图或标准输出，也不读取浏览器存储中的 Token。
 */

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);
// Playwright 由工作区运行时通过 NODE_PATH 提供，避免把浏览器依赖复制进 Console 生产包。
const { chromium } = require("playwright");

// 服务地址和 Chrome 路径允许由 CI 覆盖，本地默认值与开发环境保持一致。
const CONSOLE_URL = process.env.METAHUB_CONSOLE_URL ?? "http://localhost:5173/#usage";
const CHROME_PATH = process.env.METAHUB_CHROME_PATH
  ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const TENANT_SLUG = process.env.METAHUB_TENANT_SLUG ?? "default";
const ARTIFACT_DIR = path.resolve(process.env.METAHUB_PRD043_ARTIFACT_DIR ?? "artifacts/prd043");

// 固定 42 列必须与后端公开 CSV 合同一致；验收脚本不能只检查“下载了某个非空文件”。
const USAGE_CSV_COLUMNS = Object.freeze([
  "usage_ledger_id", "tenant_id", "request_id", "trace_id", "request_status",
  "request_error_code", "created_at", "updated_at", "environment_id", "environment",
  "environment_name", "project_id", "project_name", "application_id", "application_name",
  "virtual_key_id", "virtual_key_name", "virtual_key_prefix", "provider_id", "provider_code",
  "model_id", "request_model", "resolved_model", "endpoint", "routing_attempt_id",
  "input_tokens", "output_tokens", "total_tokens", "cache_tokens", "cost_amount",
  "cost_cny", "cost_usd", "currency", "price_schema_version", "price_snapshot_id",
  "snapshot_input_price_per_1k", "snapshot_output_price_per_1k", "snapshot_currency",
  "price_plan_id", "price_effective_from", "estimated", "usage_source",
]);

/**
 * 读取必需环境变量。
 *
 * 参数：name 为环境变量名。
 * 返回：去除首尾空白后的字符串。
 * 约束：凭据缺失时立即失败，不能在源码中提供弱口令或默认密码作为回退。
 */
function requiredEnvironment(name) {
  const value = String(process.env[name] ?? "").trim();
  assert.ok(value, `缺少必需环境变量 ${name}`);
  return value;
}

/**
 * 解析严格 RFC 4180 风格 CSV。
 *
 * 参数：text 为已移除 UTF-8 BOM 且只含 CRLF 行结束符的文本。
 * 返回：二维字符串数组。
 * 约束：只允许字段起始引号、双引号转义和 CRLF 记录边界，畸形附件必须让验收失败。
 */
function parseStrictCsv(text) {
  const rows = [];
  let row = [];
  let cell = "";
  let quoted = false;
  let justClosedQuote = false;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    // 引号字段中的逗号和 CRLF 都是正文，只有成对引号或闭合引号具有结构含义。
    if (quoted) {
      // 双引号在引号字段内必须按 RFC 4180 使用两个连续字符转义。
      if (character === '"') {
        // 连续双引号还原为一个正文引号，否则当前引号结束字段。
        if (text[index + 1] === '"') {
          cell += '"';
          index += 1;
        } else {
          quoted = false;
          justClosedQuote = true;
        }
      } else {
        cell += character;
      }
      continue;
    }
    // 引号字段闭合后只能立即遇到字段逗号或记录 CRLF，普通字符说明附件已损坏。
    if (justClosedQuote) {
      assert.ok(character === "," || character === "\r", "CSV 闭合引号后存在非法字符");
    }
    // 引号只能出现在字段开头，避免宽松解析把损坏附件误判为合法。
    if (character === '"') {
      assert.equal(cell, "", "CSV 引号出现在字段中间");
      quoted = true;
      continue;
    }
    // 逗号结束当前字段但不结束记录。
    if (character === ",") {
      row.push(cell);
      cell = "";
      justClosedQuote = false;
      continue;
    }
    // 非引号状态下只接受完整 CRLF 作为记录边界。
    if (character === "\r") {
      assert.equal(text[index + 1], "\n", "CSV 存在孤立 CR 行结束符");
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
      justClosedQuote = false;
      index += 1;
      continue;
    }
    // 裸 LF 会破坏 Windows/Excel 对账合同，不能容错接受。
    if (character === "\n") {
      assert.fail("CSV 存在裸 LF 行结束符");
    }
    cell += character;
  }
  assert.equal(quoted, false, "CSV 引号字段未闭合");
  assert.equal(cell, "", "CSV 末尾缺少 CRLF");
  assert.deepEqual(row, [], "CSV 末尾存在未结束记录");
  return rows;
}

/**
 * 读回浏览器保存的 Usage CSV 并验证完整公开合同。
 *
 * 参数：downloadPath 为保存路径，expectedRowCount 为页面当前筛选的总记录数。
 * 返回：不含 CSV 正文的列数、行数、字节数和 SHA-256 摘要。
 * 安全：校验过程不打印任一单元格；公式首字符必须已被后端防护。
 */
async function validateUsageCsvArtifact(downloadPath, expectedRowCount) {
  const content = await readFile(downloadPath);
  assert.deepEqual([...content.subarray(0, 3)], [0xef, 0xbb, 0xbf], "CSV 缺少 UTF-8 BOM");
  const text = new TextDecoder("utf-8", { fatal: true }).decode(content.subarray(3));
  assert.ok(text.endsWith("\r\n"), "CSV 末尾不是 CRLF");
  assert.doesNotMatch(text.replaceAll("\r\n", ""), /[\r\n]/, "CSV 存在非 CRLF 换行");
  const rows = parseStrictCsv(text);
  assert.deepEqual(rows[0], USAGE_CSV_COLUMNS, "CSV 固定 42 列表头发生漂移");
  assert.equal(rows.length - 1, expectedRowCount, "CSV 数据行数与页面总量不一致");
  for (const row of rows.slice(1)) {
    assert.equal(row.length, USAGE_CSV_COLUMNS.length, "CSV 数据行列数不是 42");
    for (const cellValue of row) {
      const visiblePrefix = cellValue.trimStart();
      assert.doesNotMatch(visiblePrefix, /^[=+\-@]/, "CSV 存在未防护的公式单元格");
    }
  }
  return {
    byteLength: content.length,
    rowCount: rows.length - 1,
    columnCount: rows[0].length,
    utf8Bom: true,
    crlfOnly: true,
    formulaSafe: true,
    sha256: createHash("sha256").update(content).digest("hex"),
  };
}

// 三种角色使用独立浏览器上下文；凭据只驻留当前 Node 进程内存，不进入验收结果。
const ACCOUNTS = Object.freeze({
  admin: Object.freeze({
    email: process.env.METAHUB_ADMIN_EMAIL ?? "admin@metahub.local",
    password: requiredEnvironment("METAHUB_ADMIN_PASSWORD"),
  }),
  developer: Object.freeze({
    email: requiredEnvironment("METAHUB_DEVELOPER_EMAIL"),
    password: requiredEnvironment("METAHUB_DEVELOPER_PASSWORD"),
  }),
  viewer: Object.freeze({
    email: requiredEnvironment("METAHUB_VIEWER_EMAIL"),
    password: requiredEnvironment("METAHUB_VIEWER_PASSWORD"),
  }),
});

/**
 * 收集页面脚本错误和 error 级控制台消息。
 *
 * 参数：page 为当前 Playwright 页面。
 * 返回：可在验收末尾统一断言的错误数组。
 * 安全：只保存浏览器公开错误文本，不读取请求头、请求体、Token 或下载正文。
 */
function captureBrowserErrors(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    // error 级消息代表用户可见页面可能已失效；普通调试输出不应让验收产生噪声。
    if (message.type() === "error") {
      errors.push(`console: ${message.text()}`);
    }
  });
  page.on("response", (response) => {
    // 非成功资源会破坏控制台完整性；仅记录同源公开路径和状态，不读取请求头或响应正文。
    if (response.status() >= 400) {
      const responseUrl = new URL(response.url());
      errors.push(`http ${response.status()}: ${responseUrl.pathname}${responseUrl.search}`);
    }
  });
  return errors;
}

/**
 * 使用登录表单进入指定角色的真实控制台会话。
 *
 * 参数：page 为页面，account 为内存凭据，expectedRole 为预期角色，targetUrl 为本次明确验收入口。
 * 返回：登录提交前实际进入的 hash 证据；成功后页面必须出现后端认证上下文渲染的控制台骨架。
 * 权限：角色文本来自 /auth/me，脚本不通过 DOM 注入或本地存储伪造身份。
 */
async function login(page, account, expectedRole, targetUrl = CONSOLE_URL) {
  await page.goto(targetUrl, { waitUntil: "domcontentloaded" });
  const requestedHash = await page.evaluate(() => window.location.hash);
  await page.locator("#login-form").waitFor({ state: "visible" });
  await page.locator('[name="tenantSlug"]').fill(TENANT_SLUG);
  await page.locator('[name="email"]').fill(account.email);
  await page.locator('[name="password"]').fill(account.password);
  await page.locator('#login-form button[type="submit"]').click();
  await page.waitForFunction(
    () => document.querySelector(".console-shell") || document.querySelector(".login-error"),
    undefined,
    { timeout: 30_000 },
  );
  // 认证失败只读取页面已经脱敏的公开错误和 request_id，不能输出提交的邮箱或密码。
  if (await page.locator(".login-error").count()) {
    throw new Error(`角色登录失败：${await page.locator(".login-error").innerText()}`);
  }
  await page.locator(".console-shell").waitFor({ state: "visible", timeout: 30_000 });
  const userMenu = await page.locator(".user-menu").innerText();
  assert.match(userMenu, new RegExp(`\\b${expectedRole}\\b`), `后端返回角色不是 ${expectedRole}`);
  return { requestedHash };
}

/**
 * 等待控制台完成一次局部刷新并稳定下来。
 *
 * 参数：page 为页面。
 * 返回：无。
 * 原因：导航偏好写入后会替换主应用 DOM，短暂稳定等待可避免对上一版节点继续操作。
 */
async function waitForConsoleStability(page) {
  await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => undefined);
  await page.waitForTimeout(600);
}

/**
 * 打开指定控制台入口，并等待新的主区域 DOM 就绪。
 *
 * 参数：page 为页面，navigationId 为后端导航 ID，readySelector 为目标内容选择器。
 * 返回：无。
 * 关键约束：若目标入口已激活则绝不重复点击；重复点击会触发异步 refresh 替换 DOM，形成旧按钮竞态。
 */
async function openNavigation(page, navigationId, readySelector) {
  const shell = page.locator(".console-shell");
  await shell.waitFor({ state: "visible" });
  const activeNavigation = await shell.getAttribute("data-active-navigation");
  if (activeNavigation !== navigationId) {
    const activeHash = await page.evaluate(() => window.location.hash);
    // hash 会先于异步偏好写入和完整 refresh 更新；已命中目标时只能等待，不能再次点击旧 DOM。
    if (activeHash !== `#${navigationId}`) {
      const navigationButton = page.locator(`.sidebar-link[data-console-nav="${navigationId}"]`);
      await navigationButton.waitFor({ state: "visible" });
      await navigationButton.click();
    }
    await page.waitForFunction(
      (itemId) => document.querySelector(".console-shell")?.dataset.activeNavigation === itemId,
      navigationId,
      { timeout: 30_000 },
    );
  }
  await page.locator(readySelector).waitFor({ state: "visible", timeout: 30_000 });
  await waitForConsoleStability(page);
}

/**
 * 打开 Usage 页面。
 *
 * 参数：page 为页面。
 * 返回：无；完成时筛选表单属于当前稳定 DOM。
 */
async function openUsage(page) {
  await openNavigation(page, "usage", "#usage-filter-form");
}

/**
 * 校验根节点和 Usage 关键元素没有横向溢出。
 *
 * 参数：page 为页面，viewportWidth 为期望 CSS 像素宽度。
 * 返回：页面宽度与越界元素摘要，供最终测试记录引用。
 */
async function assertUsageLayout(page, viewportWidth) {
  const layout = await page.evaluate(() => {
    const root = document.documentElement;
    const body = document.body;
    const selectors = [
      ".usage-heading",
      "#usage-filter-form",
      "[data-usage-export-feedback]",
      ".usage-mini-table",
      ".usage-mini-row",
      ".usage-record-table",
      ".usage-record-row",
      ".usage-pager",
      "#usage-export-csv",
    ];
    const offenders = [...document.querySelectorAll(selectors.join(","))]
      .filter((element) => {
        const style = getComputedStyle(element);
        const rectangle = element.getBoundingClientRect();
        // 九列表允许在自身滚动容器内延伸；这类受控内容不属于页面根节点越界。
        const scrollContainer = element.closest(".usage-record-table");
        const containedByLocalScroll = Boolean(
          scrollContainer
          && scrollContainer !== element
          && ["auto", "scroll"].includes(getComputedStyle(scrollContainer).overflowX),
        );
        return style.display !== "none"
          && style.visibility !== "hidden"
          && rectangle.width > 0
          && !containedByLocalScroll
          && (rectangle.left < -1 || rectangle.right > root.clientWidth + 1);
      })
      .map((element) => ({
        selector: element.id ? `#${element.id}` : `.${element.classList[0] ?? element.tagName.toLowerCase()}`,
        left: Math.round(element.getBoundingClientRect().left),
        right: Math.round(element.getBoundingClientRect().right),
      }));
    const overflowCandidates = [...document.querySelectorAll("body *")]
      .filter((element) => {
        const style = getComputedStyle(element);
        const rectangle = element.getBoundingClientRect();
        const contentEscapesOwnBox = element.scrollWidth > element.clientWidth + 1
          && ["visible", "clip"].includes(style.overflowX);
        // 诊断摘要同样排除表内受控滚动，避免把可访问的宽表误报为根节点来源。
        const scrollContainer = element.closest(".usage-record-table");
        const containedByLocalScroll = Boolean(
          scrollContainer
          && scrollContainer !== element
          && ["auto", "scroll"].includes(getComputedStyle(scrollContainer).overflowX),
        );
        return style.display !== "none"
          && style.visibility !== "hidden"
          && rectangle.width > 0
          && !containedByLocalScroll
          && (rectangle.right > root.clientWidth + 1 || contentEscapesOwnBox);
      })
      .slice(0, 30)
      .map((element) => ({
        element: element.id
          ? `#${element.id}`
          : `${element.tagName.toLowerCase()}.${[...element.classList].join(".")}`,
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        left: Math.round(element.getBoundingClientRect().left),
        right: Math.round(element.getBoundingClientRect().right),
        overflowX: getComputedStyle(element).overflowX,
      }));
    return {
      innerWidth: window.innerWidth,
      clientWidth: root.clientWidth,
      scrollWidth: root.scrollWidth,
      bodyScrollWidth: body.scrollWidth,
      offenders,
      overflowCandidates,
    };
  });
  assert.equal(layout.innerWidth, viewportWidth, "浏览器视口宽度不符合验收规格");
  assert.equal(layout.clientWidth, viewportWidth, "根节点 clientWidth 不符合验收规格");
  assert.ok(layout.scrollWidth <= viewportWidth, `根节点横向溢出：${JSON.stringify(layout)}`);
  assert.ok(layout.bodyScrollWidth <= viewportWidth, `body 横向溢出：${JSON.stringify(layout)}`);
  assert.deepEqual(layout.offenders, [], `Usage 关键元素越界：${JSON.stringify(layout.offenders)}`);
  assert.deepEqual(
    layout.overflowCandidates,
    [],
    `Usage 存在未受控内容溢出：${JSON.stringify(layout.overflowCandidates)}`,
  );
  return layout;
}

/**
 * 校验导出按钮和状态反馈不重叠。
 *
 * 参数：page 为页面。
 * 返回：两个元素的矩形摘要。
 */
async function assertExportFeedbackDoesNotOverlap(page) {
  const rectangles = await page.evaluate(() => {
    const toPlainRectangle = (element) => {
      const rectangle = element.getBoundingClientRect();
      return {
        left: rectangle.left,
        right: rectangle.right,
        top: rectangle.top,
        bottom: rectangle.bottom,
      };
    };
    return {
      button: toPlainRectangle(document.querySelector("#usage-export-csv")),
      feedback: toPlainRectangle(document.querySelector("[data-usage-export-feedback]")),
    };
  });
  const horizontalOverlap = rectangles.button.left < rectangles.feedback.right
    && rectangles.button.right > rectangles.feedback.left;
  const verticalOverlap = rectangles.button.top < rectangles.feedback.bottom
    && rectangles.button.bottom > rectangles.feedback.top;
  assert.ok(!(horizontalOverlap && verticalOverlap), "导出按钮与反馈区域发生重叠");
  return rectangles;
}

/**
 * 校验中窄屏用量记录以九个 data-label 伪元素恢复被隐藏的表头。
 *
 * 参数：page 为不超过 1100 像素的页面。
 * 返回：首条记录标签数量和伪元素内容。
 */
async function assertResponsiveUsageLabels(page) {
  const responsiveState = await page.evaluate(() => {
    const head = document.querySelector(".usage-record-head");
    const firstRecord = document.querySelector(
      ".usage-record-row:not(.usage-record-head):not(.usage-record-empty)",
    );
    const fields = [...firstRecord.querySelectorAll(":scope > [data-label]:not(details)")];
    return {
      headDisplay: getComputedStyle(head).display,
      labels: fields.map((field) => ({
        dataLabel: field.dataset.label,
        pseudoContent: getComputedStyle(field, "::before").content,
      })),
    };
  });
  assert.equal(responsiveState.headDisplay, "none", "中窄屏仍显示桌面 Usage 表头");
  assert.equal(responsiveState.labels.length, 9, "中窄屏首条 Usage 记录不是九个已标注字段");
  for (const label of responsiveState.labels) {
    assert.notEqual(label.pseudoContent, "none", `缺少 ${label.dataLabel} 的 ::before 标签`);
    assert.ok(label.pseudoContent.includes(label.dataLabel), `${label.dataLabel} 伪元素内容不匹配`);
  }
  return responsiveState;
}

/**
 * 校验响应式 Usage 记录已经解除桌面固定宽度，而不是把 1225px 行藏进局部滚动容器。
 *
 * 参数：page 为 900 或 390 像素视口页面。
 * 返回：表容器、首行与根节点的宽度摘要。
 */
async function assertResponsiveRecordFits(page) {
  const widths = await page.evaluate(() => {
    const root = document.documentElement;
    const table = document.querySelector(".usage-record-table");
    const row = document.querySelector(
      ".usage-record-row:not(.usage-record-head):not(.usage-record-empty)",
    );
    const rectangle = row.getBoundingClientRect();
    return {
      tableClientWidth: table.clientWidth,
      tableScrollWidth: table.scrollWidth,
      rowClientWidth: row.clientWidth,
      rowScrollWidth: row.scrollWidth,
      rowLeft: Math.round(rectangle.left),
      rowRight: Math.round(rectangle.right),
      rootClientWidth: root.clientWidth,
      rowMinWidth: getComputedStyle(row).minWidth,
    };
  });
  assert.ok(widths.tableScrollWidth <= widths.tableClientWidth + 1, "响应式 Usage 表仍有内部横向滚动");
  assert.ok(widths.rowScrollWidth <= widths.rowClientWidth + 1, "响应式 Usage 记录仍被桌面宽度撑开");
  assert.ok(widths.rowLeft >= -1 && widths.rowRight <= widths.rootClientWidth + 1, "响应式 Usage 记录越出视口");
  assert.equal(widths.rowMinWidth, "0px", "响应式 Usage 记录没有清除桌面 min-width");
  return widths;
}

/**
 * 生成固定视口截图并遮罩身份区域。
 *
 * 参数：page 为已登录页面，fileName 为 artifacts/prd043 下的文件名。
 * 返回：无。
 * 安全：admin 邮箱、租户名和 Session 到期时间只能驻留页面，不得进入可共享 PNG。
 */
async function takePrivacySafeScreenshot(page, fileName) {
  const userMenu = page.locator(".user-menu");
  assert.equal(await userMenu.count(), 1, "截图前没有找到需遮罩的身份区域");
  await page.screenshot({
    path: path.join(ARTIFACT_DIR, fileName),
    fullPage: false,
    mask: [userMenu],
    maskColor: "#e5e7eb",
  });
}

/**
 * 执行管理员真实下载及桌面、移动布局验收。
 *
 * 参数：browser 为 Chromium 实例。
 * 返回：可公开写入测试记录的状态、文件名、响应头和布局摘要，不含账号或 Token。
 */
async function runAdminAcceptance(browser) {
  const context = await browser.newContext({
    acceptDownloads: true,
    deviceScaleFactor: 1,
    viewport: { width: 1280, height: 720 },
  });
  const page = await context.newPage();
  const browserErrors = captureBrowserErrors(page);
  try {
    await login(page, ACCOUNTS.admin, "admin");
    await openUsage(page);
    assert.equal(await page.locator("#usage-export-csv").count(), 1, "admin 缺少 CSV 导出按钮");
    const usageCountText = await page.locator(".usage-count").innerText();
    const usageTotalMatch = usageCountText.match(/\/\s*(\d+)$/);
    assert.ok(usageTotalMatch, "Usage 页面总记录数格式无效");
    const expectedCsvRows = Number(usageTotalMatch[1]);

    const csvResponsePromise = page.waitForResponse(
      (response) => response.url().includes("/usage/export.csv") && response.request().method() === "GET",
      { timeout: 30_000 },
    );
    const downloadPromise = page.waitForEvent("download", { timeout: 30_000 });
    await page.locator("#usage-export-csv").click();
    const [csvResponse, download] = await Promise.all([csvResponsePromise, downloadPromise]);
    assert.equal(csvResponse.status(), 200, "浏览器同步导出没有返回 HTTP 200");
    const contentDisposition = await csvResponse.headerValue("content-disposition");
    assert.match(contentDisposition ?? "", /^attachment;\s*filename="?[A-Za-z0-9._-]+\.csv"?$/i);

    const downloadPath = path.join(ARTIFACT_DIR, "browser-admin-download.csv");
    await download.saveAs(downloadPath);
    assert.equal(await download.failure(), null, "Chrome 报告 CSV 下载失败");
    const downloadStat = await stat(downloadPath);
    assert.ok(downloadStat.size > 3, "浏览器保存的 CSV 为空");
    const csvContract = await validateUsageCsvArtifact(downloadPath, expectedCsvRows);
    assert.equal(csvContract.byteLength, downloadStat.size, "CSV 读回字节数与保存结果不一致");

    await page.waitForFunction(
      () => document.querySelector("[data-usage-export-feedback]")?.textContent?.includes("已下载"),
      undefined,
      { timeout: 30_000 },
    );
    const feedback = await page.locator("[data-usage-export-feedback]").innerText();
    assert.match(feedback, /request_id:\s*\S+/, "下载反馈缺少 request_id");
    assert.ok(await page.locator("#usage-export-csv").isEnabled(), "下载完成后导出按钮未恢复可用");

    await page.locator(".usage-heading").scrollIntoViewIfNeeded();
    const desktopLayout = await assertUsageLayout(page, 1280);
    await assertExportFeedbackDoesNotOverlap(page);
    await takePrivacySafeScreenshot(page, "browser-desktop-1280x720.png");

    await page.setViewportSize({ width: 900, height: 800 });
    await page.locator(".usage-heading").scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    const intermediateLayout = await assertUsageLayout(page, 900);
    const intermediateLabels = await assertResponsiveUsageLabels(page);
    const intermediateRecordFits = await assertResponsiveRecordFits(page);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator(".usage-heading").scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    const mobileLayout = await assertUsageLayout(page, 390);
    await assertExportFeedbackDoesNotOverlap(page);
    const mobileLabels = await assertResponsiveUsageLabels(page);
    const mobileRecordFits = await assertResponsiveRecordFits(page);
    await takePrivacySafeScreenshot(page, "browser-mobile-390x844.png");

    const firstRecord = page.locator(
      ".usage-record-row:not(.usage-record-head):not(.usage-record-empty)",
    ).first();
    await firstRecord.scrollIntoViewIfNeeded();
    await page.waitForTimeout(200);
    await takePrivacySafeScreenshot(page, "browser-mobile-records-390x844.png");
    assert.deepEqual(browserErrors, [], `admin 浏览器错误：${JSON.stringify(browserErrors)}`);

    return {
      status: "passed",
      httpStatus: csvResponse.status(),
      contentDisposition,
      suggestedFilename: download.suggestedFilename(),
      csvContract,
      feedbackHasRequestId: /request_id:\s*\S+/.test(feedback),
      desktopLayout,
      intermediateLayout,
      intermediateLabelCount: intermediateLabels.labels.length,
      intermediateRecordFits,
      mobileLayout,
      mobileLabelCount: mobileLabels.labels.length,
      mobileRecordFits,
      privacyMaskApplied: true,
      browserErrorCount: browserErrors.length,
    };
  } finally {
    await context.close();
  }
}

/**
 * 验证 developer 可以读取 Usage，但前端没有 admin-only 导出命令。
 *
 * 参数：browser 为 Chromium 实例。
 * 返回：角色可见性摘要。
 * 权限：该断言只验证交互表现，后端 403 仍由集成测试和真实 API 验收独立覆盖。
 */
async function runDeveloperAcceptance(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const page = await context.newPage();
  const browserErrors = captureBrowserErrors(page);
  try {
    await login(page, ACCOUNTS.developer, "developer");
    assert.equal(
      await page.locator('.sidebar-link[data-console-nav="usage"]').count(),
      1,
      "developer 看不到 Usage 导航",
    );
    await openUsage(page);
    assert.equal(await page.locator("#usage-filter-form").count(), 1, "developer 无法读取 Usage 页面");
    assert.equal(await page.locator("#usage-export-csv").count(), 0, "developer 错误获得导出按钮");
    assert.deepEqual(browserErrors, [], `developer 浏览器错误：${JSON.stringify(browserErrors)}`);
    return { status: "passed", usageNavigation: true, exportButton: false, browserErrorCount: 0 };
  } finally {
    await context.close();
  }
}

/**
 * 验证 viewer 不获得 Usage 导航，也不从竞品能力映射绕过该入口。
 *
 * 参数：browser 为 Chromium 实例。
 * 返回：角色可见性摘要。
 */
async function runViewerAcceptance(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const page = await context.newPage();
  const browserErrors = captureBrowserErrors(page);
  try {
    const directUsageUrl = new URL(CONSOLE_URL);
    directUsageUrl.hash = "usage";
    const loginEvidence = await login(page, ACCOUNTS.viewer, "viewer", directUsageUrl.toString());
    assert.equal(loginEvidence.requestedHash, "#usage", "viewer 验收没有实际请求直接 #usage");
    const rejectedHash = await page.evaluate(() => window.location.hash);
    const fallbackNavigation = await page.locator(".console-shell").getAttribute("data-active-navigation");
    assert.equal(rejectedHash, "", "viewer 的越权 #usage hash 未被清理");
    assert.equal(fallbackNavigation, "overview", "viewer 越权 hash 没有回退概览");
    assert.equal(await page.locator("#usage-filter-form").count(), 0, "viewer 通过直接 hash 渲染了 Usage DOM");
    assert.equal(
      await page.locator('.sidebar-link[data-console-nav="usage"]').count(),
      0,
      "viewer 错误获得 Usage 导航",
    );
    await openNavigation(page, "competitor-comparison", "[data-competitor-comparison-root]");
    assert.equal(
      await page.locator('.competitor-nav-link[data-console-nav="usage"]').count(),
      0,
      "viewer 从竞品能力映射获得 Usage 跳转",
    );
    assert.deepEqual(browserErrors, [], `viewer 浏览器错误：${JSON.stringify(browserErrors)}`);
    return {
      status: "passed",
      requestedHash: loginEvidence.requestedHash,
      rejectedHashCleared: rejectedHash === "",
      fallbackNavigation,
      usageDom: false,
      usageNavigation: false,
      usageCapabilityLink: false,
      browserErrorCount: 0,
    };
  } finally {
    await context.close();
  }
}

/**
 * 启动系统 Chrome 并顺序执行三个角色验收。
 *
 * 返回：无；结果以不含凭据的 JSON 输出，任一断言失败都会设置非零退出码。
 */
async function main() {
  // 验收启动前先证明严格解析器拒绝闭合引号后的垃圾字符，并接受标准引号转义。
  assert.throws(() => parseStrictCsv('"safe"junk\r\n'), /闭合引号后存在非法字符/);
  assert.deepEqual(parseStrictCsv('"safe,cell","say ""ok"""\r\n'), [["safe,cell", 'say "ok"']]);
  const browser = await chromium.launch({
    executablePath: CHROME_PATH,
    headless: process.env.METAHUB_BROWSER_HEADED !== "1",
  });
  try {
    const admin = await runAdminAcceptance(browser);
    const developer = await runDeveloperAcceptance(browser);
    const viewer = await runViewerAcceptance(browser);
    console.log(JSON.stringify({ admin, developer, viewer }, null, 2));
  } finally {
    await browser.close();
  }
}

await main();
