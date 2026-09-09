import { icon } from "../public-home/icons.js";

export function renderConsumers(directory) {
  if (!directory) return "";
  if (directory.state === "error") return `<section class="consumer-directory">
    <h3>组织使用者</h3><p role="alert">${escape(directory.message)}</p></section>`;
  const consumers = directory.consumers ?? [];
  return `<section class="consumer-directory" aria-labelledby="consumers-title">
    <div class="consumer-directory-heading"><h3 id="consumers-title">组织使用者</h3>
      <span>${directory.total} 人</span></div>
    <form id="create-consumer-form" class="consumer-form">
      <label><span>姓名</span><input name="displayName" maxlength="160" required /></label>
      <label><span>部门</span><input name="department" maxlength="160" /></label>
      <button type="submit">添加使用者</button>
    </form>
    <div class="consumer-list">${consumers.map((consumer) => `<form class="consumer-form consumer-row"
      data-consumer-form data-consumer-id="${escape(consumer.consumer_id)}" data-revision="${consumer.revision}">
      <label><span>姓名</span><input name="displayName" maxlength="160" required value="${escape(consumer.display_name)}" /></label>
      <label><span>部门</span><input name="department" maxlength="160" value="${escape(consumer.department)}" /></label>
      <label class="consumer-status"><input type="checkbox" name="active" ${consumer.status === "active" ? "checked" : ""} /><span>启用</span></label>
      <span class="consumer-key-count">${consumer.key_count} 把 KEY</span>
      <button type="submit">保存</button>
    </form>`).join("") || '<p class="consumer-empty">暂无使用者</p>'}</div>
    <nav class="consumer-pagination" aria-label="使用者分页">
      <button type="button" data-consumer-page="${Math.max(0, directory.offset - directory.limit)}" ${directory.offset === 0 ? "disabled" : ""} title="上一页" aria-label="上一页">${icon("arrow-right")}</button>
      <span>${directory.total ? directory.offset + 1 : 0} - ${Math.min(directory.total, directory.offset + directory.limit)}</span>
      <button type="button" data-consumer-page="${directory.offset + directory.limit}" ${directory.offset + directory.limit >= directory.total ? "disabled" : ""} title="下一页" aria-label="下一页">${icon("arrow-right")}</button>
    </nav>
  </section>`;
}

export function renderConsumerSelection(directory) {
  if (!directory) return "";
  return `<label><span>使用者</span><select name="consumerId" ${directory.state === "error" ? "disabled" : ""}>
    <option value="">技术 KEY（不绑定员工）</option>
    ${(directory.consumers ?? []).filter((consumer) => consumer.status === "active").map((consumer) =>
      `<option value="${escape(consumer.consumer_id)}">${escape(consumer.display_name)}${consumer.department ? ` / ${escape(consumer.department)}` : ""} / ${escape(consumer.consumer_id.slice(-8))}</option>`).join("")}
  </select></label>`;
}

function escape(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}
