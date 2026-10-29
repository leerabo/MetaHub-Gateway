import { writeFile } from "node:fs/promises";

// Regenerate only on an intentional upstream update; production never fetches a CDN.
const url = "https://cdn.jsdelivr.net/npm/lucide-static@0.468.0/icon-nodes.json";
const response = await fetch(url);
if (!response.ok) throw new Error(`Lucide download failed: ${response.status}`);
const upstream = await response.json();
const licenseResponse = await fetch("https://cdn.jsdelivr.net/npm/lucide-static@0.468.0/LICENSE");
if (!licenseResponse.ok) throw new Error("Lucide license download failed");
const license = await licenseResponse.text();
const names = ["arrow-right", "arrow-left", "key-round", "layers", "git-branch",
  "shield-check", "chart-no-axes-combined", "cpu", "users", "activity", "pause",
  "play", "eye", "eye-off", "rotate-cw", "copy", "search", "eraser", "grip-vertical"];
const selected = Object.fromEntries(names.map((name) => {
  if (!Array.isArray(upstream[name])) throw new Error(`Missing upstream icon: ${name}`);
  return [name, upstream[name]];
}));
await writeFile(new URL("../src/vendor/lucide.js", import.meta.url),
  `/* Lucide 0.468.0\n${license.trim()}\n*/\nexport const iconNodes = ${JSON.stringify(selected, null, 2)};\n`);
