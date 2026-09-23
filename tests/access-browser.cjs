// Quota/UI integration using controlled responses; never calls a model or identity service.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const base = process.env.SYMWRITE_URL || "http://127.0.0.1:8001";
(async () => {
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROME_PATH
      ? { executablePath: process.env.CHROME_PATH }
      : {}),
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  let access = {
    signed_in: false,
    remaining: 1,
    limit: 10,
    available: 1,
    sign_in_required: false,
    capacity_available: true,
    reset_at: 1900000000,
    csrf_token: "test-csrf",
    sign_in_ready: true,
    turnstile_site_key: null,
    daily_limit: 10,
  };
  let modelCalls = 0;
  await context.route("**/api/access", (r) => r.fulfill({ json: access }));
  await context.route("**/api/complete", (r) => {
    modelCalls++;
    assert.equal(r.request().headers()["x-csrf-token"], "test-csrf");
    const body = r.request().postDataJSON();
    access = { ...access, remaining: 0, available: 0, sign_in_required: true };
    return r.fulfill({
      json: {
        request_id: body.request_id,
        suggestions: [
          {
            label: "Refined",
            stage: "synthesis",
            text: "the writer should be able to keep the question open.",
          },
        ],
        context: [],
        candidate_count: 5,
        elapsed_ms: 1000,
        retrieval: "semantic",
        access,
      },
    });
  });
  await page.goto(base);
  await page.locator("#editor:not([disabled])").waitFor();
  await page.locator("#title").fill("A question about authorship");
  await page
    .locator("#editor")
    .fill("If assistance changes what feels available to say, then");
  await page.locator("#generate").click();
  await page.locator("#result").waitFor({ state: "visible" });
  await page.locator("#trial-gate").waitFor({ state: "visible" });
  assert.equal(
    await page.locator("#allowance").textContent(),
    "0 trial continuations",
  );
  await page.locator("#generate").click();
  assert.equal(modelCalls, 1);
  await page.locator("#accept").click();
  assert.match(
    await page.locator("#editor").inputValue(),
    /keep the question open/,
  );
  console.log(
    "PASS: last trial accepted, eleventh click gated, existing suggestion remains usable",
  );
  // Same-origin sign-in failure must keep the draft available.
  await context.route("**/api/auth/google", (r) =>
    r.fulfill({
      status: 503,
      json: { detail: "Sign-in is temporarily unavailable." },
    }),
  );
  await page.locator("#sign-in").click();
  await page
    .getByText("Sign-in is temporarily unavailable.", { exact: true })
    .waitFor();
  assert.match(
    await page.locator("#editor").inputValue(),
    /keep the question open/,
  );
  await page.reload();
  await page.locator("#editor:not([disabled])").waitFor();
  assert.match(
    await page.locator("#editor").inputValue(),
    /keep the question open/,
  );
  console.log("PASS: sign-in failure and reload preserve local drafts");
  await fs.mkdir("test-results", { recursive: true });
  await page.screenshot({
    path: "test-results/trial-gate-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "test-results/trial-gate-mobile.png",
    fullPage: true,
  });
  assert(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  );
  assert(await page.locator("#sign-in").isVisible());
  console.log("PASS: trial gate and account controls fit mobile layout");
  access = {
    ...access,
    signed_in: true,
    remaining: 0,
    available: 0,
    sign_in_required: false,
  };
  await page.reload();
  await page.locator("#editor:not([disabled])").waitFor();
  assert.equal(await page.locator("#allowance").textContent(), "0 left today");
  assert(await page.locator("#trial-gate").isHidden());
  await page.locator("#generate").click();
  await page
    .getByText(
      "Today's writing allowance is used. It resets at midnight UTC.",
      { exact: true },
    )
    .waitFor();
  assert.equal(modelCalls, 1);
  access = { ...access, remaining: 10, available: 10 };
  await page.reload();
  await page.locator("#editor:not([disabled])").waitFor();
  assert.equal(await page.locator("#account").textContent(), "Sign out");
  await context.route("**/api/auth/logout", (r) => {
    assert.equal(r.request().headers()["x-csrf-token"], "test-csrf");
    access = {
      ...access,
      signed_in: false,
      remaining: 0,
      available: 0,
      sign_in_required: true,
    };
    return r.fulfill({ json: { ok: true } });
  });
  await page.locator("#account").click();
  await page.locator("#trial-gate").waitFor({ state: "visible" });
  assert.match(
    await page.locator("#editor").inputValue(),
    /keep the question open/,
  );
  console.log(
    "PASS: daily allowance, sign-out and persistent anonymous trial exhaustion",
  );
  assert.deepEqual(errors, []);
  await browser.close();
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
