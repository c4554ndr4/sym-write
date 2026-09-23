// Browser regressions use controlled HTTP responses, never paid generation.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const base = process.env.SYMWRITE_URL || "http://127.0.0.1:8001";
const responseFor = (body) => ({
  request_id: body.request_id,
  cursor: body.cursor,
  suggestions: [
    {
      label: "Refined",
      text: "a continuation worth reviewing.",
      stage: "synthesis",
    },
    {
      label: "Candidate 1",
      text: "another direction for the draft.",
      stage: "candidate",
    },
  ],
  context: [
    {
      id: "source-1",
      title: "Selected writing",
      text: "Preserve uncertainty in the draft.",
      kind: "writing",
      score: 0.8,
    },
  ],
  candidate_count: 5,
  failed_candidates: 0,
  elapsed_ms: 1000,
  retrieval: "semantic",
  warning: null,
});
(async () => {
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROME_PATH
      ? { executablePath: process.env.CHROME_PATH }
      : {}),
  });
  const context = await browser.newContext({
    viewport: { width: 1500, height: 1000 },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  let completions = 0;
  await page.route("**/api/complete", async (route) => {
    completions++;
    await route.fulfill({ json: responseFor(route.request().postDataJSON()) });
  });
  await page.goto(base);
  await page.locator("#editor:not([disabled])").waitFor();
  const editor = page.locator("#editor");
  const original = await editor.inputValue();
  await editor.fill("An idea 🌱 begins here. Keep this ending.");
  const before = await editor.inputValue();
  const cursor = before.indexOf(" Keep");
  await editor.evaluate((el, pos) => {
    el.focus();
    el.setSelectionRange(pos, pos);
    el.dispatchEvent(new Event("select"));
  }, cursor);
  let request;
  page.once("request", (r) => {
    if (r.url().endsWith("/api/complete")) request = r.postDataJSON();
  });
  await page.locator("#generate").click();
  await page.locator("#result").waitFor({ state: "visible" });
  assert.equal(request.cursor, Array.from(before.slice(0, cursor)).length);
  await page.locator("#suggestion-tabs button").nth(1).click();
  await page.getByRole("tab", { name: "What is this?" }).click();
  assert.equal(await page.locator("#writing-panel").isHidden(), true);
  assert.equal(await page.locator("#about-panel").isVisible(), true);
  assert.equal(
    await page.locator("#about-tab").getAttribute("aria-selected"),
    "true",
  );
  await page.keyboard.press("Control+Enter");
  await page.keyboard.press("Escape");
  await page.locator("#about-tab").focus();
  await page.keyboard.press("ArrowLeft");
  assert.equal(
    await page.locator("#writing-tab").getAttribute("aria-selected"),
    "true",
  );
  assert.equal(await editor.inputValue(), before);
  assert.equal(await editor.evaluate((el) => el.selectionStart), cursor);
  assert.equal(
    await page
      .locator("#suggestion-tabs button")
      .nth(1)
      .getAttribute("aria-pressed"),
    "true",
  );
  assert.equal(completions, 1);
  await page.goBack();
  assert.equal(await page.locator("#about-panel").isVisible(), true);
  await page.goForward();
  assert.equal(await page.locator("#result").isVisible(), true);
  console.log(
    "PASS: view navigation and browser history preserve draft, cursor, and selected suggestion; help shortcuts do not generate or dismiss",
  );
  await page.locator("#accept").click();
  assert.equal(
    await editor.inputValue(),
    "An idea 🌱 begins here. another direction for the draft. Keep this ending.",
  );
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  assert.equal(await editor.inputValue(), before);
  console.log(
    "PASS: candidate selection, Unicode cursor, suffix preservation, acceptance, undo",
  );
  await page.reload();
  await page.locator("#editor:not([disabled])").waitFor();
  assert.equal(await editor.inputValue(), before);
  await page.locator("#new-note").click();
  await page.locator("#title").fill("Recovered note");
  await editor.fill("A new draft survives reload.");
  await page.reload();
  await page.locator("#editor:not([disabled])").waitFor();
  assert.equal(await page.locator("#title").inputValue(), "Recovered note");
  assert.equal(await editor.inputValue(), "A new draft survives reload.");
  console.log("PASS: per-document draft persistence and reload");
  const downloadEvent = page.waitForEvent("download");
  await page.locator("#export").click();
  const download = await downloadEvent;
  assert.equal(download.suggestedFilename(), "Recovered-note.md");
  const contents = await fs.readFile(await download.path(), "utf8");
  assert.equal(contents, "# Recovered note\n\nA new draft survives reload.\n");
  console.log("PASS: Markdown export preserves draft");
  await editor.focus();
  await page.keyboard.press("Tab");
  assert.notEqual(
    await page.evaluate(() => document.activeElement.id),
    "editor",
  );
  console.log("PASS: Tab leaves the editor");
  await editor.fill(
    'Text <img src=x onerror="window.bad=true"> is plain writing.',
  );
  await page.locator("#generate").click();
  await page.locator("#result").waitFor({ state: "visible" });
  assert.equal(await page.evaluate(() => window.bad), undefined);
  assert.equal(await page.locator("#writing-panel img").count(), 0);
  await page.locator("#dismiss").click();
  assert.equal(await page.locator("#result").isHidden(), true);
  console.log("PASS: draft HTML stays inert and dismissal works");
  // Hold a response long enough to edit, move, dismiss, or switch the draft deterministically.
  await page.unroute("**/api/complete");
  let release;
  await page.route("**/api/complete", async (route) => {
    const data = responseFor(route.request().postDataJSON());
    await new Promise((resolve) => {
      release = resolve;
    });
    await route.fulfill({ json: data }).catch(() => {});
  });
  async function begin() {
    release = null;
    await editor.fill("An editable draft.");
    await page.locator("#generate").click();
    await page.waitForRequest(() => false, { timeout: 30 }).catch(() => {});
    while (!release) await new Promise((r) => setTimeout(r, 10));
  }
  await begin();
  await page.locator("#about-tab").click();
  release();
  await page.locator("#writing-tab").click();
  await page.locator("#result").waitFor({ state: "visible" });
  assert.equal(await editor.inputValue(), "An editable draft.");
  console.log(
    "PASS: a requested continuation can finish while the explanation is open",
  );
  await begin();
  await editor.fill("A newer thought.");
  release();
  await page.waitForTimeout(80);
  assert.equal(await page.locator("#result").isHidden(), true);
  assert.equal(await editor.inputValue(), "A newer thought.");
  await begin();
  await page.keyboard.press("Escape");
  release();
  await page.waitForTimeout(80);
  assert.equal(await page.locator("#result").isHidden(), true);
  await begin();
  await editor.evaluate((el) => {
    el.focus();
    el.setSelectionRange(2, 2);
    el.dispatchEvent(new Event("select"));
  });
  release();
  await page.waitForTimeout(80);
  assert.equal(await page.locator("#result").isHidden(), true);
  await begin();
  await page.locator("#documents-menu summary").click();
  await page.locator("#documents button").first().click();
  release();
  await page.waitForTimeout(80);
  assert.equal(await page.locator("#result").isHidden(), true);
  console.log(
    "PASS: edits, Escape, cursor movement, and document switching discard late responses",
  );
  await page.unroute("**/api/complete");
  await page.route("**/api/complete", (route) =>
    route.fulfill({
      status: 502,
      json: { detail: "Provider unavailable. Try again." },
    }),
  );
  const kept = await editor.inputValue();
  await page.locator("#generate").click();
  await page.locator("#error").waitFor({ state: "visible" });
  assert.match(
    await page.locator("#error").textContent(),
    /Provider unavailable/,
  );
  assert.equal(await editor.inputValue(), kept);
  console.log("PASS: provider failure is visible and preserves the draft");
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  assert.equal(await page.locator("#generate").isVisible(), true);
  console.log("PASS: narrow viewport has no horizontal overflow");
  // Startup must not accept text before the stored draft is restored.
  const delayed = await context.newPage();
  let continueStatus;
  await delayed.route("**/api/status", async (route) => {
    await new Promise((r) => (continueStatus = r));
    await route.continue();
  });
  await delayed.goto(base, { waitUntil: "domcontentloaded" });
  assert.equal(await delayed.locator("#editor").isDisabled(), true);
  while (!continueStatus) await new Promise((r) => setTimeout(r, 10));
  continueStatus();
  await delayed.locator("#editor:not([disabled])").waitFor();
  console.log("PASS: startup input waits for draft recovery");
  // Separate tabs share a notebook but cannot silently replace each other's new draft.
  await delayed.locator("#editor").fill("Saved by a second tab.");
  await page.locator("#error").waitFor({ state: "visible" });
  assert.match(await page.locator("#error").textContent(), /another tab/);
  assert.deepEqual(errors, []);
  console.log("PASS: cross-tab write conflict is visible; no browser errors");
  // A direct explanation link stays open after asynchronous notebook restoration.
  const helpContext = await browser.newContext();
  const help = await helpContext.newPage();
  await help.goto(base + "#about");
  await help.locator("#editor:not([disabled])").waitFor({ state: "attached" });
  assert.equal(await help.locator("#about-panel").isVisible(), true);
  await help.reload();
  await help.locator("#editor:not([disabled])").waitFor({ state: "attached" });
  assert.equal(await help.locator("#about-panel").isVisible(), true);
  for (const width of [1440, 900, 781, 600, 390, 320]) {
    await help.setViewportSize({ width, height: 900 });
    assert.equal(
      await help.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
      `Help overflow at ${width}px`,
    );
    const diagram = help.locator(".pipeline-figure img");
    await diagram.evaluate((el) => el.decode());
    assert.equal(
      await diagram.evaluate((el) =>
        el.currentSrc.includes("pipeline-mobile.svg"),
      ),
      width <= 600,
    );
  }
  await help.locator("#new-note").click();
  assert.equal(await help.locator("#writing-panel").isVisible(), true);
  assert.equal(await help.locator("#title").inputValue(), "");
  console.log(
    "PASS: explanation deep link, reload, responsive diagrams, and return to writing from New note",
  );
  await helpContext.close();
  await browser.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
