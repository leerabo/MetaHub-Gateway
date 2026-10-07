/**
 * 普通管理员的单条件表单只生成 policy-dsl-v1 的固定白名单比较式。
 * 无法无损表示的旧规则留在高级编辑，不在浏览器侧求值或改写。
 */
export const GUIDED_CONDITION_FIELDS = Object.freeze([
  { value: "env", label: "环境", kind: "text" },
  { value: "project", label: "项目", kind: "text" },
  { value: "data_class", label: "数据分类", kind: "text" },
  { value: "budget_remaining", label: "剩余预算（元）", kind: "number" },
]);

const TEXT_OPERATORS = Object.freeze(["==", "!="]);
const NUMBER_OPERATORS = Object.freeze(["<", "<=", ">", ">=", "==", "!="]);
const DECIMAL = /^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,12})?$/;
const STRING_COMPARISON = /^(env|project|data_class)\s*(==|!=)\s*("(?:\\.|[^"\\])*")$/;
const NUMBER_COMPARISON = /^(budget_remaining)\s*(<=|>=|==|!=|<|>)\s*((?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,12})?)$/;

export function emptyGuidedCondition() {
  return { field: "", operator: "==", value: "" };
}

export function guidedConditionOperators(field) {
  return field === "budget_remaining" ? NUMBER_OPERATORS : TEXT_OPERATORS;
}

export function normalizeGuidedCondition(value) {
  const field = GUIDED_CONDITION_FIELDS.some((item) => item.value === value?.field)
    ? value.field : "";
  const operator = guidedConditionOperators(field).includes(value?.operator) ? value.operator : "==";
  return { field, operator, value: String(value?.value ?? "").trim() };
}

export function buildGuidedConditionExpression(value) {
  const condition = normalizeGuidedCondition(value);
  if (!condition.field || !condition.value) return "";
  if (condition.field === "budget_remaining") {
    return DECIMAL.test(condition.value)
      ? `${condition.field} ${condition.operator} ${condition.value}` : "";
  }
  if (condition.value.length > 128 || /[\u0000-\u001f\u007f]/.test(condition.value)) return "";
  return `${condition.field} ${condition.operator} ${JSON.stringify(condition.value)}`;
}

export function parseGuidedConditionExpression(expression) {
  const source = String(expression ?? "").trim();
  if (!source) return emptyGuidedCondition();
  const numeric = NUMBER_COMPARISON.exec(source);
  if (numeric) return { field: numeric[1], operator: numeric[2], value: numeric[3] };
  const text = STRING_COMPARISON.exec(source);
  if (!text) return null;
  try {
    const value = JSON.parse(text[3]);
    const condition = { field: text[1], operator: text[2], value };
    return buildGuidedConditionExpression(condition) ? condition : null;
  } catch {
    return null;
  }
}
