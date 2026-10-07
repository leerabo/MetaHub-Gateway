import fs from "node:fs";
import path from "node:path";
import { PNG } from "pngjs";

const root = path.resolve(process.env.METAHUB_QA_SCREENSHOT_ROOT ?? ".artifacts/product-qa");
const prefixes = (process.env.METAHUB_QA_SCREENSHOT_PREFIXES ?? "20260925-screenshot-audit-")
  .split(",").map((value) => value.trim()).filter(Boolean);
const explicitDirectories = (process.env.METAHUB_QA_SCREENSHOT_DIRECTORIES ?? "")
  .split(",").map((value) => value.trim()).filter(Boolean).map((value) => path.resolve(value));

function* walk(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) yield* walk(file);
    else yield file;
  }
}

const rows = [];
const directories = explicitDirectories.length > 0
  ? explicitDirectories
  : fs.readdirSync(root, { withFileTypes: true })
    .filter((directory) => directory.isDirectory() && prefixes.some((prefix) => directory.name.startsWith(prefix)))
    .map((directory) => path.join(root, directory.name));
for (const base of directories) {
  for (const file of walk(base)) {
    if (!file.endsWith(".png")) continue;
    const bytes = fs.readFileSync(file);
    const image = PNG.sync.read(bytes);
    let nonBackground = 0;
    let dark = 0;
    let white = 0;
    for (let offset = 0; offset < image.data.length; offset += 4) {
      if (image.data[offset + 3] < 10) continue;
      const luminance = (image.data[offset] + image.data[offset + 1] + image.data[offset + 2]) / 3;
      if (luminance < 10) dark += 1;
      if (luminance > 245) white += 1;
      if (luminance < 238) nonBackground += 1;
    }
    const pixels = image.width * image.height;
    rows.push({
      file: path.relative(process.cwd(), file),
      width: image.width,
      height: image.height,
      bytes: bytes.length,
      non_background_ratio: Number((nonBackground / pixels).toFixed(4)),
      dark_ratio: Number((dark / pixels).toFixed(4)),
      white_ratio: Number((white / pixels).toFixed(4)),
    });
  }
}

const result = {
  schema: "metahub.prd148.screenshot-visual-audit.v1",
  root: path.relative(process.cwd(), root),
  screenshot_count: rows.length,
  dimensions: [...new Set(rows.map((row) => `${row.width}x${row.height}`))].sort(),
  mobile_screenshots: rows.filter((row) => row.width === 390),
  unexpected_small: rows.filter((row) => row.width < 700 && row.width !== 390 || row.height < 500),
  near_blank: rows.filter((row) => row.non_background_ratio < 0.01),
  very_dark: rows.filter((row) => row.dark_ratio > 0.98),
  screenshots: rows.sort((left, right) => left.file.localeCompare(right.file)),
};
const serialized = `${JSON.stringify(result, null, 2)}\n`;
if (process.env.METAHUB_QA_SCREENSHOT_REPORT) {
  fs.writeFileSync(path.resolve(process.env.METAHUB_QA_SCREENSHOT_REPORT), serialized, "utf8");
}
process.stdout.write(serialized);
