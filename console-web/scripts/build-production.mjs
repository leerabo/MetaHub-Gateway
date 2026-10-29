import { createHash } from "node:crypto";
import { mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { productionAssetBytes } from "./production-asset-bytes.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(await readFile(path.join(root, "production-surface-manifest.json"), "utf8"));
const output = path.resolve(root, manifest.output_directory);

if (path.dirname(output) !== root || path.basename(output) !== manifest.output_directory) {
  throw new Error("production output must be a direct child of console-web");
}

const javascript = await reachableJavascript(manifest.entrypoints);
const assets = [...new Set([
  ...manifest.html_entrypoints,
  ...manifest.style_entrypoints,
  ...javascript,
])].sort();

await rm(output, { recursive: true, force: true });
const records = [];
for (const relative of assets) {
  const source = path.resolve(root, relative);
  if (!source.startsWith(`${root}${path.sep}`)) throw new Error(`asset escapes console-web: ${relative}`);
  const sourceStat = await stat(source);
  if (!sourceStat.isFile()) throw new Error(`production asset is not a file: ${relative}`);
  const destination = path.join(output, relative);
  await mkdir(path.dirname(destination), { recursive: true });
  const content = productionAssetBytes(relative, await readFile(source));
  await writeFile(destination, content);
  records.push(Object.freeze({
    path: normalize(relative),
    bytes: content.byteLength,
    sha256: createHash("sha256").update(content).digest("hex"),
  }));
}

const assetSetSha256 = createHash("sha256")
  .update(JSON.stringify(records))
  .digest("hex");
if (assetSetSha256 !== manifest.production_asset_set_sha256) {
  throw new Error(
    `production asset set drift: expected ${manifest.production_asset_set_sha256}, observed ${assetSetSha256}`,
  );
}

await writeFile(path.join(output, "production-assets.json"), `${JSON.stringify({
  manifest_version: manifest.version,
  manifest_sha256: createHash("sha256")
    .update(JSON.stringify(manifest, Object.keys(manifest).sort()))
    .digest("hex"),
  asset_set_sha256: assetSetSha256,
  assets: records,
}, null, 2)}\n`, "utf8");

console.log(`production build ok (${records.length} assets)`);

async function reachableJavascript(entrypoints) {
  const visited = new Set();
  const queue = [...entrypoints];
  while (queue.length > 0) {
    const relative = normalize(queue.shift());
    if (visited.has(relative)) continue;
    visited.add(relative);
    const absolute = path.join(root, relative);
    const source = await readFile(absolute, "utf8");
    for (const specifier of importSpecifiers(source)) {
      if (!specifier.startsWith(".")) continue;
      const resolved = normalize(path.relative(root, path.resolve(path.dirname(absolute), specifier)));
      if (!resolved.startsWith("src/") || !resolved.endsWith(".js")) {
        throw new Error(`production import escapes JavaScript source graph: ${relative} -> ${specifier}`);
      }
      queue.push(resolved);
    }
  }
  return [...visited];
}

function importSpecifiers(source) {
  const results = [];
  const patterns = [
    /(?:import|export)\s+(?:[^"'`]*?\s+from\s+)?["']([^"']+)["']/gu,
    /import\s*\(\s*["']([^"']+)["']\s*\)/gu,
  ];
  for (const pattern of patterns) {
    for (const match of source.matchAll(pattern)) results.push(match[1]);
  }
  return results;
}

function normalize(value) {
  return String(value).replaceAll("\\", "/");
}
