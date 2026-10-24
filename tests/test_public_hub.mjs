import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const declarations = source.split("\n").filter(line => /^(const HUB_VIEW_ORIGIN|const HUB_URL_RE)/u.test(line)).join("\n");
const start = source.indexOf("function detectHubUrls(text)");
const end = source.indexOf("\nfunction hubAnchor", start);
assert.ok(start > 0 && end > start);
const sandbox = { URL, location: { origin: "https://hub.example.test" } };
vm.createContext(sandbox);
vm.runInContext(declarations + "\n" + source.slice(start, end) + "\nthis.detect = detectHubUrls;", sandbox);
const valid = ["https://hub.example.test/docs/sample.html", "https://hub.example.test/mobile/", "https://HUB.example.test:443/docs/sample.html"];
for (const url of valid) assert.equal(sandbox.detect("資料 " + url + " 終了").length, 1);
for (const url of ["https://other.example.test/docs/sample.html", "https://hub.example.test/not-docs", "https://user:pass@hub.example.test/docs/sample.html"])
  assert.equal(sandbox.detect(url).length, 0);
console.log("PASS: 6 public hub link cases; network=false");
