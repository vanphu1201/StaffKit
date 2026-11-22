// Reusable 0-token Visual & Runtime Browser Smoke Check using Playwright.
// Usage: node scripts/smoke_web_playwright.mjs --url http://127.0.0.1:5173 [--min-height 250] [--screenshot out.png]

function parseArgs(argv) {
  const opts = { url: "", minHeight: 250, timeout: 15000, screenshot: "" };
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    if (k === "--url" && argv[i + 1]) opts.url = argv[++i];
    else if (k === "--min-height" && argv[i + 1]) opts.minHeight = Number(argv[++i]);
    else if (k === "--timeout" && argv[i + 1]) opts.timeout = Number(argv[++i]);
    else if (k === "--screenshot" && argv[i + 1]) opts.screenshot = argv[++i];
  }
  return opts;
}

async function main() {
  const opts = parseArgs(process.argv);
  if (!opts.url) {
    console.error("Missing required --url argument");
    process.exit(2);
  }

  let playwright;
  try {
    playwright = await import("playwright");
  } catch {
    console.error("Playwright package is not installed (`npm install -D playwright`).");
    process.exit(1);
  }

  const browser = await playwright.chromium.launch({ headless: true });
  const consoleErrors = [];
  const pageErrors = [];

  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });
    page.on("pageerror", (err) => {
      pageErrors.push(String(err));
    });

    await page.goto(opts.url, { waitUntil: "domcontentloaded", timeout: opts.timeout });
    await page.waitForTimeout(800);

    const metrics = await page.evaluate(() => {
      const brokenImages = Array.from(document.querySelectorAll("img"))
        .filter((img) => !img.complete || img.naturalWidth === 0)
        .map((img) => img.getAttribute("src") || "(no src)");
      const containers = Array.from(document.querySelectorAll("canvas, main, #root"));
      const maxHeight = containers.reduce((best, el) => {
        const rect = el.getBoundingClientRect();
        return Math.max(best, rect.height || 0);
      }, 0);
      return { brokenImages, maxHeight, bodyTextLen: (document.body?.innerText || "").trim().length };
    });

    if (opts.screenshot) {
      await page.screenshot({ path: opts.screenshot, fullPage: false });
    }

    const failures = [];
    if (consoleErrors.length > 0) failures.push(`console_errors=${JSON.stringify(consoleErrors.slice(0, 5))}`);
    if (pageErrors.length > 0) failures.push(`page_errors=${JSON.stringify(pageErrors.slice(0, 5))}`);
    if (metrics.brokenImages.length > 0) failures.push(`broken_images=${JSON.stringify(metrics.brokenImages.slice(0, 5))}`);
    if (metrics.maxHeight < opts.minHeight) {
      failures.push(`collapsed_layout maxHeight=${metrics.maxHeight}px < minHeight=${opts.minHeight}px`);
    }
    if (metrics.bodyTextLen === 0) failures.push("empty_body_text");

    if (failures.length > 0) {
      console.error(`VISUAL_SMOKE_FAIL ${failures.join(" | ")}`);
      process.exit(1);
    }
    console.log(JSON.stringify({ ok: true, url: opts.url, ...metrics }));
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error("VISUAL_SMOKE_ERROR:", err);
  process.exit(1);
});
