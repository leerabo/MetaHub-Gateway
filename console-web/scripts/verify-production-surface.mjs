import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(await readFile(path.join(root, "production-surface-manifest.json"), "utf8"));
const failures = [];
const visited = new Set();
const queue = [...manifest.entrypoints];
const shippedSources = new Map();

while (queue.length > 0) {
  const relative = normalize(queue.shift());
  if (visited.has(relative)) continue;
  visited.add(relative);
  if (manifest.retired_runtime_modules.some((item) => retiredMatch(relative, item))) {
    failures.push(`${relative}: retired runtime module is reachable from a production entrypoint`);
    continue;
  }
  const absolute = path.join(root, relative);
  let source;
  try {
    source = await readFile(absolute, "utf8");
  } catch {
    failures.push(`${relative}: production import cannot be resolved`);
    continue;
  }
  shippedSources.set(relative, source);
  scanRetiredRuntimeLiterals(relative, source);
  for (const specifier of importSpecifiers(source)) {
    if (!specifier.startsWith(".")) continue;
    const resolved = normalize(path.relative(root, path.resolve(path.dirname(absolute), specifier)));
    if (!resolved.startsWith("src/") || !resolved.endsWith(".js")) {
      failures.push(`${relative}: production import escapes the JavaScript source graph (${specifier})`);
      continue;
    }
    queue.push(resolved);
  }
  for (const route of externalRouteLiterals(source)) {
    if (manifest.retired_external_routes.some((item) => retiredRouteMatch(route, item))) {
      failures.push(`${relative}: retired external route literal remains reachable (${route})`);
    }
  }
}

const htmlScripts = new Set();
const htmlStyles = new Set();
for (const relative of manifest.html_entrypoints) {
  const source = await readFile(path.join(root, relative), "utf8");
  shippedSources.set(relative, source);
  scanRetiredRuntimeLiterals(relative, source);
  for (const match of source.matchAll(/<script\b[^>]*\bsrc=["']([^"']+)["'][^>]*>/gu)) {
    htmlScripts.add(normalizeWebAsset(match[1]));
  }
  for (const match of source.matchAll(/<link\b[^>]*\brel=["']stylesheet["'][^>]*\bhref=["']([^"']+)["'][^>]*>/gu)) {
    htmlStyles.add(normalizeWebAsset(match[1]));
  }
}

compareDeclaredAssets("JavaScript entrypoint", htmlScripts, new Set(manifest.entrypoints));
compareDeclaredAssets("stylesheet entrypoint", htmlStyles, new Set(manifest.style_entrypoints));

for (const relative of manifest.style_entrypoints) {
  const source = await readFile(path.join(root, relative), "utf8");
  shippedSources.set(relative, source);
  scanRetiredRuntimeLiterals(relative, source);
}

for (const replacement of manifest.replacement_modules) {
  if (![...visited].some((item) => retiredMatch(item, replacement))) {
    failures.push(`${replacement}: required replacement module is not reachable`);
  }
}

const dockerfilePath = path.resolve(root, manifest.production_dockerfile);
const dockerfile = await readFile(dockerfilePath, "utf8");
const runtimeStage = dockerfile.split(/^FROM\s+/gmu).at(-1) ?? "";
if (/^COPY\s+console-web\/src\s+/gmu.test(runtimeStage)) {
  failures.push(`${manifest.production_dockerfile}: production image copies the complete source tree`);
}
if (!/RUN\s+npm\s+run\s+build/u.test(dockerfile)) {
  failures.push(`${manifest.production_dockerfile}: verified production build is not executed`);
}
if (!/COPY\s+--from=build\s+\/app\/console-web\/dist\s+\/app\/console-web/u.test(dockerfile)) {
  failures.push(`${manifest.production_dockerfile}: runtime image does not copy only verified dist output`);
}

if (failures.length > 0) {
  console.error([...new Set(failures)].sort().join("\n"));
  process.exit(1);
}

console.log(`production surface ok (${visited.size} modules, ${shippedSources.size} shipped sources, manifest v${manifest.version})`);

function scanRetiredRuntimeLiterals(relative, source) {
  for (const sourceId of manifest.retired_source_ids) {
    const references = [
      `"${sourceId}"`,
      `'${sourceId}'`,
      `\`${sourceId}\``,
      `#${sourceId}`,
      `.${sourceId}`,
      `data-${sourceId}`,
    ];
    if (references.some((reference) => source.includes(reference))) {
      failures.push(`${relative}: retired source id remains reachable (${sourceId})`);
    }
  }
  for (const literal of manifest.retired_runtime_literals) {
    if (source.includes(literal)) {
      failures.push(`${relative}: retired DOM/prefetch/restore/telemetry literal remains (${literal})`);
    }
  }
}

function compareDeclaredAssets(kind, observed, expected) {
  for (const relative of expected) {
    if (!observed.has(relative)) failures.push(`${relative}: declared ${kind} is not linked by production HTML`);
  }
  for (const relative of observed) {
    if (!expected.has(relative)) failures.push(`${relative}: production HTML links an undeclared ${kind}`);
  }
}

function normalizeWebAsset(value) {
  const pathname = String(value).split(/[?#]/u, 1)[0];
  return normalize(pathname.replace(/^\/+/, ""));
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

function externalRouteLiterals(source) {
  const results = [];
  for (const match of source.matchAll(/(?:"([^"\r\n]*)"|'([^'\r\n]*)'|`([^`\r\n]*)`)/gu)) {
    const value = String(match[1] ?? match[2] ?? match[3] ?? "").trim();
    if (value.startsWith("/")) results.push(value.replace(/\$\{[^}]+\}/gu, "{value}"));
  }
  return results;
}

function retiredMatch(candidate, manifestPath) {
  return manifestPath.endsWith("/")
    ? candidate.startsWith(manifestPath)
    : candidate === manifestPath;
}

function retiredRouteMatch(candidate, retired) {
  const route = candidate.split("?", 1)[0];
  if (retired === "/branding") return route === retired;
  if (retired === "/dashboard") return route === retired || route.startsWith(`${retired}/`);
  return route === retired || route.startsWith(`${retired}/`);
}

function normalize(value) {
  return String(value).replaceAll("\\", "/");
}
