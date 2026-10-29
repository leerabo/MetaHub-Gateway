import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const featuresRoot = path.join(root, "console-web", "src", "features");
const explanatory = /默认|自动(?:应用|发现|生成)?|不需要|无需|属于同一个|按工具要求|环境变量|具体客户端|用于|请先|只显示一次|复制保存/iu;
const necessary = /错误|失败|重试|权限|撤权|失效|只显示一次|复制保存|不支持|无法|不可用/iu;
const htmlLine = /<(?:p|small|span|strong|li|dt|dd)[\s>]/iu;
const chinese = /[\u4e00-\u9fff]/u;

function filesUnder(directory) {
  const result = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory()) result.push(...filesUnder(target));
    else if (entry.isFile() && /^render.*\.js$/iu.test(entry.name)) result.push(target);
  }
  return result;
}

const candidates = [];
for (const file of filesUnder(featuresRoot)) {
  const lines = fs.readFileSync(file, "utf8").split(/\r?\n/u);
  lines.forEach((line, index) => {
    const text = line.trim();
    if (text.startsWith("//") || text.startsWith("*") || text.startsWith("/*") || text.startsWith("/**")) return;
    if (!htmlLine.test(text) || !chinese.test(text) || !explanatory.test(text)) return;
    const plain = text.replace(/<[^>]+>/gu, " ").replace(/\$\{[^}]+\}/gu, " ").replace(/\s+/gu, " ").trim();
    if (!plain) return;
    candidates.push({
      file: path.relative(root, file).replaceAll(path.sep, "/"),
      line: index + 1,
      text: plain.slice(0, 240),
      classification: necessary.test(plain) ? "necessary" : "review",
    });
  });
}

const counts = candidates.reduce((result, item) => {
  result[item.classification] += 1;
  return result;
}, { necessary: 0, review: 0 });
process.stdout.write(`${JSON.stringify({
  generated_at: new Date().toISOString(),
  scope: "console-web/src/features/**/render*.js",
  definition: "HTML text lines with Chinese explanatory markers; comments, hidden fields and non-renderers are excluded. Necessary recovery, permission, revocation and one-time-secret copy is retained for review.",
  candidate_count: candidates.length,
  counts,
  candidates,
}, null, 2)}\n`);
