import { readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";

const suite = process.argv[2];
if (suite !== "unit" && suite !== "fixture") {
  console.error("usage: node scripts/run-tests.mjs <unit|fixture>");
  process.exit(2);
}

const relativeDir = suite === "unit" ? "dist/tests" : "dist/fixture-tests";
const absoluteDir = path.resolve(relativeDir);
let files;
try {
  files = readdirSync(absoluteDir)
    .filter((name) => name.endsWith(".test.js"))
    .sort()
    .map((name) => path.join(absoluteDir, name));
} catch (error) {
  console.error(`test output directory missing: ${relativeDir}`);
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}

if (files.length === 0) {
  console.error(`no compiled ${suite} tests found in ${relativeDir}`);
  process.exit(1);
}

const result = spawnSync(process.execPath, ["--test", ...files], {
  cwd: process.cwd(),
  env: process.env,
  encoding: "utf8",
  stdio: "inherit",
});
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
