// Default: local screenshots only. --live explicitly sends the invitation to the
// configured model provider (up to six calls); --recorded redraws an existing response.
const { chromium } = require("playwright");
const fs = require("node:fs/promises");
const path = require("node:path");
const assert = require("node:assert/strict");
const base = process.env.SYMWRITE_URL || "http://127.0.0.1:8001";
const root = path.resolve(__dirname, "..");
const recorded = process.argv.includes("--recorded");
const live = process.argv.includes("--live");
(async () => {
  const status = await (await fetch(`${base}/api/status`)).json();
  assert(
    status.demo,
    "Capture requires the exact author-approved demo profile.",
  );
  if (live)
    assert(
      status.generation_ready && status.retrieval_ready,
      "Configure generation and retrieval first.",
    );
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROME_PATH
      ? { executablePath: process.env.CHROME_PATH }
      : {}),
  });
  try {
    const page = await browser.newPage({
      viewport: { width: 1500, height: 1080 },
      deviceScaleFactor: 1,
    });
    const browserErrors = [];
    page.on("pageerror", (e) => browserErrors.push(e.message));
    if (!live && !recorded) {
      await page.route("**/api/complete", (route) => {
        browserErrors.push(
          "Unexpected generation request during input-only capture",
        );
        return route.abort();
      });
    }
    let record;
    if (recorded) {
      record = JSON.parse(
        await fs.readFile(path.join(root, "docs/examples/avalon.json"), "utf8"),
      );
      assert.equal(
        record.request.profile_version,
        status.profile_version,
        "Saved response belongs to another profile version.",
      );
      await page.route("**/api/complete", (route) =>
        route.fulfill({
          json: {
            ...record.response,
            request_id: route.request().postDataJSON().request_id,
          },
        }),
      );
    }
    await page.goto(base);
    await page.locator("#editor:not([disabled])").waitFor();
    await page.locator("#examples-menu summary").click();
    await page
      .getByRole("button", { name: "An invitation to Avalon", exact: true })
      .click();
    const text = await page.locator("#editor").inputValue();
    const title = await page.locator("#title").inputValue();
    if (!live && !recorded) {
      await page.locator("#editor").evaluate((el) => {
        el.scrollTop = el.scrollHeight;
      });
      await page.screenshot({
        path: path.join(root, "docs/screenshots/avalon.png"),
        fullPage: true,
      });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.locator("#editor").evaluate((el) => {
        el.scrollTop = el.scrollHeight;
      });
      await page.evaluate(() => scrollTo(0, 0));
      assert(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      );
      await page.screenshot({
        path: path.join(root, "docs/screenshots/avalon-mobile.png"),
        fullPage: true,
      });
      assert.deepEqual(browserErrors, []);
      console.log(
        "Captured author-provided Avalon draft on desktop and mobile; no model requests.",
      );
      return;
    }

    const pending = page.waitForResponse(
      (r) => r.url().endsWith("/api/complete"),
      { timeout: 95000 },
    );
    await page.locator("#generate").click();
    const response = await pending;
    const data = await response.json();
    assert.equal(response.status(), 200, JSON.stringify(data));
    assert.equal(data.synthesis_status, "complete");
    assert(
      data.candidate_count >= 1,
      "At least one usable candidate is required.",
    );
    await page.locator("#result").waitFor({ state: "visible" });
    if (!recorded) {
      record = {
        captured_at: new Date().toISOString(),
        profile: status.profile,
        synthetic: status.synthetic,
        provenance:
          "Author-provided Avalon invitation and revision note, selected for the demo.",
        title,
        input: text,
        request: response.request().postDataJSON(),
        response: data,
        displayed_suggestion: 0,
        candidate_screenshot_suggestion: 1,
      };
      await fs.writeFile(
        path.join(root, "docs/examples/avalon.json"),
        JSON.stringify(record, null, 2) + "\n",
      );
      await fs.writeFile(
        path.join(root, "docs/examples/summary.json"),
        JSON.stringify(
          [
            {
              id: "avalon",
              title,
              seconds: data.elapsed_ms / 1000,
              candidate_count: data.candidate_count,
              failed_candidates: data.failed_candidates,
              candidate_model: data.models.candidate,
              synthesis_model: data.models.synthesis,
              context: data.context.map((x) => x.title),
              suggestion: data.suggestions[0].text,
            },
          ],
          null,
          2,
        ) + "\n",
      );
    }
    // Keep the continuation point visible in the textarea for every capture.
    await page.locator("#editor").evaluate((el) => {
      el.scrollTop = el.scrollHeight;
    });
    await page.screenshot({
      path: path.join(root, "docs/screenshots/avalon-generated.png"),
      fullPage: true,
    });
    await page.locator("#suggestion-tabs button").nth(1).click();
    await page.screenshot({
      path: path.join(root, "docs/screenshots/avalon-candidates.png"),
      fullPage: true,
    });
    await page.locator("#suggestion-tabs button").nth(0).click();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("#editor").evaluate((el) => {
      el.scrollTop = el.scrollHeight;
    });
    await page.evaluate(() => scrollTo(0, 0));
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    await page.screenshot({
      path: path.join(root, "docs/screenshots/avalon-generated-mobile.png"),
      fullPage: true,
    });
    assert.deepEqual(browserErrors, []);
    console.log(
      JSON.stringify({
        recorded,
        seconds: record.response.elapsed_ms / 1000,
        candidates: record.response.candidate_count,
        failed: record.response.failed_candidates,
        context: record.response.context.map((s) => s.title),
      }),
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
