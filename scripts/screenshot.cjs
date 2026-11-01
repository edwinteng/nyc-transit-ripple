// Take a screenshot of the dashboard for the README (used by the Pages workflow).
//   node scripts/screenshot.cjs <url> <out.png>
// Needs the `playwright` npm package and Chromium; the image is published with
// the site and is never committed to git.
"use strict";
const { chromium } = require("playwright");

(async () => {
  const [url = "http://127.0.0.1:8000/", out = "screenshot.png"] = process.argv.slice(2);
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
    page.on("pageerror", (err) => console.error(`page error: ${err.message}`));
    await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
    await page.waitForSelector("#last-update:not(:empty)");
    await page.waitForTimeout(2000); // let map tiles and charts settle
    await page.screenshot({ path: out, fullPage: false });
    console.log(`Saved ${out}`);
  } finally {
    await browser.close();
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
