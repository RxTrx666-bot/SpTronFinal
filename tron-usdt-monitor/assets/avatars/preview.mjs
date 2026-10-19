// Side-by-side preview of the avatars as Telegram shows them (circle crop): node preview.mjs
import { createRequire } from "module";
import { readFileSync } from "fs";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const items = [["radar", "1 · Radar"], ["bell", "2 · Alert bell"], ["shield", "3 · Pulse shield"]];
const cells = items.map(([n, label]) => {
  const b64 = readFileSync(new URL(`./${n}.png`, import.meta.url)).toString("base64");
  return `<div class="cell"><img src="data:image/png;base64,${b64}"><div class="label">${label}</div>
          <div class="chat"><img src="data:image/png;base64,${b64}"><div><b>USDT Monitor</b><span>✅ USDT TRANSFER DONE</span></div></div></div>`;
}).join("");
const html = `<html><head><style>
  body{margin:0;background:#f1f3f6;font-family:'DejaVu Sans',sans-serif}
  .row{display:flex;gap:48px;padding:48px;justify-content:center}
  .cell{display:flex;flex-direction:column;align-items:center;gap:18px}
  .cell>img{width:260px;height:260px;border-radius:50%;box-shadow:0 10px 30px rgba(0,0,0,.18)}
  .label{font-size:22px;font-weight:bold;color:#1f2937}
  .chat{display:flex;gap:12px;align-items:center;background:#fff;border-radius:14px;padding:10px 16px;width:300px;box-shadow:0 2px 8px rgba(0,0,0,.08)}
  .chat img{width:54px;height:54px;border-radius:50%}
  .chat div{display:flex;flex-direction:column;font-size:15px;color:#111}
  .chat span{color:#6b7280;font-size:13px;margin-top:3px}
</style></head><body><div class="row">${cells}</div></body></html>`;
const browser = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium" });
const page = await browser.newPage({ viewport: { width: 1180, height: 520 } });
await page.setContent(html);
await page.screenshot({ path: new URL("./preview.png", import.meta.url).pathname });
await browser.close();
