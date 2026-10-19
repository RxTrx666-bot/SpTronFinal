// Render each avatar SVG to an exact 640x640 PNG:  node render.mjs
import { createRequire } from "module";
import { readFileSync } from "fs";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const names = ["radar", "bell", "shield"];
const browser = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium" });
const page = await browser.newPage({ viewport: { width: 640, height: 640 } });
for (const name of names) {
  const svg = readFileSync(new URL(`./${name}.svg`, import.meta.url), "utf8");
  await page.setContent(`<html><body style="margin:0">${svg}</body></html>`);
  await page.screenshot({ path: new URL(`./${name}.png`, import.meta.url).pathname, omitBackground: true });
}
await browser.close();
