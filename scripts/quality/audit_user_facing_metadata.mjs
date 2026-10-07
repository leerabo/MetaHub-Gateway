import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const featuresRoot = path.join(root, "console-web", "src", "features");
const patterns = Object.freeze({
  request_id: /\brequest_id\b/giu,
  trace_id: /\btrace_id\b/giu,
  provider_code: /\bprovider_code\b/giu,
  provider_id: /\bprovider_id\b/giu,
  model_id: /\b(?:internal_model_id|provider_model_id|model_id)\b/giu,
  region_code: /\bregion_code\b/giu,
  auth_type: /\bauth_type\b/giu,
  revision: /\b(?:source_revision|catalog_revision|revision)\b/giu,
  hash_or_fingerprint: /\b(?:sha256|sha-?256|fingerprint|digest)\b/giu,
});
const rendererPattern = /(?:^|[\\/])render[^\\/]*\.js$/iu;

function filesUnder(directory) {
  const result = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory()) result.push(...filesUnder(target));
    else if (entry.isFile() && target.endsWith(".js")) result.push(target);
  }
  return result;
}

const files = filesUnder(featuresRoot);
const counts = Object.fromEntries(Object.keys(patterns).map((key) => [key, 0]));
const rendererCounts = Object.fromEntries(Object.keys(patterns).map((key) => [key, 0]));
let rendererFiles = 0;
let rendererCandidateLines = 0;
const candidateExamples = [];

for (const file of files) {
  const source = fs.readFileSync(file, "utf8");
  const isRenderer = rendererPattern.test(file);
  if (isRenderer) rendererFiles += 1;
  for (const [key, pattern] of Object.entries(patterns)) {
    const matches = source.match(pattern) ?? [];
    counts[key] += matches.length;
    if (isRenderer) rendererCounts[key] += matches.length;
  }
  if (isRenderer) {
    for (const [index, line] of source.split(/\r?\n/u).entries()) {
      for (const pattern of Object.values(patterns)) pattern.lastIndex = 0;
      if (!Object.values(patterns).some((pattern) => pattern.test(line))) continue;
      rendererCandidateLines += 1;
      if (candidateExamples.length < 20) {
        candidateExamples.push({
          file: path.relative(root, file).replaceAll(path.sep, "/"),
          line: index + 1,
          text: line.trim().slice(0, 220),
        });
      }
      for (const pattern of Object.values(patterns)) pattern.lastIndex = 0;
    }
  }
  for (const pattern of Object.values(patterns)) pattern.lastIndex = 0;
}

const report = {
  generated_at: new Date().toISOString(),
  scope: "console-web/src/features/**/*.js",
  files_scanned: files.length,
  renderer_files: rendererFiles,
  total_matches: Object.values(counts).reduce((sum, value) => sum + value, 0),
  renderer_matches: Object.values(rendererCounts).reduce((sum, value) => sum + value, 0),
  candidate_renderer_lines: rendererCandidateLines,
  counts,
  renderer_counts: rendererCounts,
  classification: {
    retained_internal_contracts: [
      "API request/trace identifiers and response ownership checks",
      "provider/model identifiers used for routing and idempotency",
      "revision, fingerprint, cursor and generation values used for CAS and authorization",
      "hashes used for manifests, signatures, migration and audit evidence",
    ],
    removed_from_user_projection: [
      "request and trace identifiers in ordinary status/error copy",
      "provider/model database identifiers and catalog metadata in employee-facing views",
      "region/authentication codes, revisions and hashes in setup summaries",
    ],
    note: "Counts are lexical upper bounds. A renderer match may be a hidden form value or a backend contract check; review the renderer before changing the API/state layer.",
  },
  candidate_examples: candidateExamples,
};

process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
