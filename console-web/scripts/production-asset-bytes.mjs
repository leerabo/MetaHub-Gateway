import path from "node:path";

export function productionAssetBytes(relative, content) {
  if (![".html", ".css", ".js"].includes(path.extname(relative))) return content;
  return Buffer.from(content.toString("utf8").replaceAll("\r\n", "\n"), "utf8");
}
