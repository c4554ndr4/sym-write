"use strict";
const $ = (id) => document.getElementById(id);
const examples = [
  {
    id: "avalon",
    title: "An invitation to Avalon",
    kind: "INVITATION",
    text: "First draft:\n\nJoin Avalon for a night of revelry in honor of one of the great masters of the English language. Drinking, declamations of sonnets, duels, dancing, and many other forms of merriment to be provided. Costumes mandatory! Come as a Roman, a faerie or a nymph, medieval nobility, in a carnival mask, or anything else drawn from his settings or characters. Those without a costume will be turned away, be sure to tell anyone you invite!\n\nSonnets and memorized monologues will be presented around 9:30. If you would like to prepare and present something, let me know!\n\n(Notes for second draft: there will be a fiddle and a duel.)\n\nSecond draft:\n\nWELCOME ONE AND ALL TO AVALON!",
  },
];
const retiredExamples = [
  {
    id: "agency",
    title: "Where the decision happens",
    kind: "RESEARCH NOTE",
    text: "I keep returning to a small question about writing assistants: at what point does a continuation become a decision?\n\nThe obvious answer is when I accept it. But that leaves out the choices already made on my behalf: which earlier passages were retrieved, which possibilities were generated, and which one was presented as the natural next thought.\n\nI want to study the review step as part of the architecture. If the writer can see the sources and compare several directions, then",
  },
  {
    id: "memory",
    title: "The shape of a memory",
    kind: "DESIGN NOTE",
    text: "I do not want a writing assistant to treat everything I have written as equally relevant to what I am trying to say now.\n\nA notebook contains abandoned hypotheses, observations that only made sense in their original setting, and sentences whose confidence I would no longer defend. Retrieving a passage preserves its words. It does not necessarily preserve those qualifications.\n\nThe useful question is not simply whether the system can remember more. It is whether",
  },
  {
    id: "evaluation",
    title: "What acceptance can tell us",
    kind: "OPEN QUESTION",
    text: "A high acceptance rate would be easy to report and difficult to interpret.\n\nA writer might accept a suggestion because it expresses something they already meant. They might also accept it because it is fluent, arrives at a convenient moment, or makes an unresolved question sound settled. Those interactions look similar in a log, but they describe rather different relationships with the tool.\n\nI would begin an evaluation by",
  },
];
let access = null,
  challengeWidget = null,
  challengePromise = null;
let profile,
  docs = [],
  activeId,
  storageKey,
  storageBlocked = false;
let revision = 0,
  pending = null,
  result = null,
  snapshot = null,
  selected = 0,
  undo = null;
let caret = { start: 0, end: 0 };
const active = () => docs.find((doc) => doc.id === activeId);
const copy = (value) => JSON.parse(JSON.stringify(value));

function renderAccess() {
  if (!access) return;
  $("allowance").textContent = access.signed_in
    ? `${access.remaining} left today`
    : `${access.remaining} trial continuations`;
  $("allowance").title =
    "Started requests count, including cancelled or failed generations.";
  $("account").textContent = access.signed_in ? "Sign out" : "Sign in";
  $("account").disabled =
    !!pending || (!access.signed_in && !access.sign_in_ready);
  $("account").title = access.sign_in_ready
    ? ""
    : "Sign-in is not configured on this installation yet.";
  $("trial-gate").hidden = !access.sign_in_required;
  $("idle").hidden = !!result || !!pending || access.sign_in_required;
  $("trial-message").textContent = access.sign_in_ready
    ? `Sign in for ${access.daily_limit} continuations a day.`
    : "Sign-in is not configured on this installation yet. Your drafts are still available.";
  $("sign-in").hidden = !access.sign_in_ready;
}
async function refreshAccess() {
  const response = await fetch("/api/access");
  if (!response.ok)
    throw new Error(
      "Could not verify your writing allowance. Reload to try again.",
    );
  access = await response.json();
  renderAccess();
}
async function signIn() {
  if (!access || pending) return;
  save();
  if (storageBlocked || $("save-status").textContent !== "Saved") {
    setError("Export your draft before leaving this page to sign in.");
    return;
  }
  try {
    const response = await fetch("/api/auth/google", {
      method: "POST",
      headers: { "X-CSRF-Token": access.csrf_token },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Sign-in is unavailable.");
    location.assign(data.url);
  } catch (error) {
    setError(error.message);
  }
}
function challenge() {
  if (!access.turnstile_site_key) return Promise.resolve("");
  if (!challengePromise) {
    challengePromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src =
        "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
      script.onload = () => resolve();
      script.onerror = () => {
        script.remove();
        challengePromise = null;
        reject(new Error("Verification could not load. Please try again."));
      };
      document.head.append(script);
    });
  }
  return challengePromise.then(
    () =>
      new Promise((resolve, reject) => {
        $("verification").hidden = false;
        let settled = false;
        const finish = (token, error) => {
          if (settled) return;
          settled = true;
          $("verification").hidden = true;
          if (error) reject(new Error(error));
          else resolve(token);
        };
        if (challengeWidget !== null) window.turnstile.remove(challengeWidget);
        challengeWidget = window.turnstile.render("#verification", {
          sitekey: access.turnstile_site_key,
          action: "continue",
          theme: "dark",
          size: "flexible",
          execution: "execute",
          appearance: "interaction-only",
          callback: (token) => finish(token),
          "error-callback": () =>
            finish(null, "Verification failed. Please try again."),
          "expired-callback": () =>
            finish(null, "Verification expired. Please try again."),
          "timeout-callback": () =>
            finish(null, "Verification timed out. Please try again."),
        });
        window.turnstile.execute(challengeWidget);
      }),
  );
}
function setError(message) {
  $("error").textContent = message;
  $("error").hidden = !message;
}
function save() {
  active().title = $("title").value;
  active().text = $("editor").value;
  if (storageBlocked) {
    $("save-status").textContent = "Not saved — export";
    return;
  }
  try {
    localStorage.setItem(storageKey, JSON.stringify({ activeId, docs }));
    $("save-status").textContent = "Saved";
  } catch {
    $("save-status").textContent = "Not saved — export";
    setError(
      "Browser storage is unavailable or full. Keep this tab open and export your draft.",
    );
  }
}
function wordCount() {
  const words = $("editor").value.trim().match(/\S+/g) || [];
  $("word-count").textContent = `${words.length} words`;
}
function renderDocuments() {
  $("documents").replaceChildren();
  docs.forEach((doc, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("aria-current", String(doc.id === activeId));
    const number = document.createElement("small");
    number.textContent = `0${index + 1}`.slice(-2);
    const title = document.createElement("strong");
    title.textContent = doc.title || "Untitled";
    button.append(number, title);
    button.addEventListener("click", () => switchDocument(doc.id));
    $("documents").append(button);
  });
}
function invalidate(message = "") {
  revision++;
  if (pending) {
    pending.controller.abort();
    clearTimeout(pending.timer);
    pending = null;
  }
  $("verification").hidden = true;
  result = null;
  snapshot = null;
  $("result").hidden = true;
  $("request-progress").hidden = true;
  $("idle").hidden = false;
  $("request-state").textContent = "Ready";
  $("branch-count").textContent = "";
  updateGenerate();
  if (message) $("accepted-note").textContent = message;
}
function closeMenus() {
  document.querySelectorAll("[data-nav-menu]").forEach((menu) => {
    menu.open = false;
  });
}
function switchDocument(id, reveal = true) {
  if (reveal) showView("writing");
  closeMenus();
  if (active()) save();
  invalidate();
  activeId = id;
  undo = null;
  $("title").value = active().title;
  $("editor").value = active().text;
  $("document-number").textContent =
    `DRAFT ${String(docs.findIndex((d) => d.id === id) + 1).padStart(2, "0")}`;
  $("accepted-note").replaceChildren();
  setError("");
  caret = { start: $("editor").value.length, end: $("editor").value.length };
  $("editor").setSelectionRange(caret.start, caret.end);
  renderDocuments();
  wordCount();
  save();
  updateGenerate();
}
function updateGenerate() {
  $("generate").disabled =
    !profile ||
    !access ||
    !!pending ||
    !$("editor").value.slice(0, caret.start).trim();
  if (access) renderAccess();
}
function captureCaret() {
  const next = {
    start: $("editor").selectionStart,
    end: $("editor").selectionEnd,
  };
  if (next.start !== caret.start || next.end !== caret.end) {
    if (result || pending) invalidate("");
    caret = next;
    updateGenerate();
  }
}
function renderResult() {
  $("idle").hidden = true;
  $("request-progress").hidden = true;
  $("result").hidden = false;
  $("request-state").textContent = "Ready";
  $("suggestion-tabs").replaceChildren();
  $("branch-count").textContent =
    `${selected + 1} / ${result.suggestions.length}`;
  result.suggestions.forEach((suggestion, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "continuation-card";
    button.setAttribute("aria-label", suggestion.label);
    button.setAttribute("aria-pressed", String(index === selected));
    button.title = suggestion.label;
    const number = document.createElement("span");
    number.className = "continuation-number";
    number.textContent =
      suggestion.stage === "candidate"
        ? suggestion.label.replace("Candidate ", "")
        : "R";
    number.setAttribute("aria-hidden", "true");
    const text = document.createElement("span");
    text.className = "continuation-copy";
    text.textContent = suggestion.text;
    text.id = index === selected ? "suggestion-text" : `continuation-${index}`;
    button.setAttribute("aria-describedby", text.id);
    button.append(number, text);
    button.addEventListener("click", () => {
      selected = index;
      const scroll = $("result").closest(".continuation-body");
      const position = scroll.scrollTop;
      renderResult();
      scroll.scrollTop = position;
      $("suggestion-tabs").children[index].focus({ preventScroll: true });
    });
    $("suggestion-tabs").append(button);
  });
  $("generation-note").textContent =
    `${result.candidate_count} candidates · ${(result.elapsed_ms / 1000).toFixed(1)}s · ${result.retrieval} retrieval`;
  $("warning").hidden = !result.warning;
  $("warning").textContent = result.warning || "";
  $("context-count").textContent = String(result.context.length);
  $("context-list").replaceChildren();
  result.context.forEach((source, index) => {
    const detail = document.createElement("details");
    detail.className = "source-card";
    detail.open = false;
    const summary = document.createElement("summary");
    summary.textContent = source.title;
    const text = document.createElement("p");
    text.textContent = source.text;
    const kind = document.createElement("small");
    kind.textContent =
      source.kind === "fact" ? "Approved fact" : "Writing excerpt";
    detail.append(summary, text, kind);
    $("context-list").append(detail);
  });
  if (!result.context.length) {
    const text = document.createElement("p");
    text.className = "muted";
    text.textContent = "No matching sources.";
    $("context-list").append(text);
  }
}
async function generate() {
  if (pending || !profile || !access) return;
  // A UTC-day reset or sign-in in another tab can restore an exhausted allowance.
  if (access.available === 0) {
    try {
      await refreshAccess();
    } catch (error) {
      setError(error.message);
      return;
    }
    if (pending) return;
  }
  if (access.sign_in_required) {
    renderAccess();
    $("trial-gate").scrollIntoView({ block: "nearest" });
    if (access.sign_in_ready) $("sign-in").focus();
    return;
  }
  if (access.available === 0) {
    setError("Today's writing allowance is used. It resets at midnight UTC.");
    return;
  }
  const editor = $("editor");
  if (caret.start !== caret.end) {
    setError(
      "Place the cursor where you want to continue. Selected text will not be replaced.",
    );
    return;
  }
  if (!editor.value.slice(0, caret.start).trim()) {
    setError("Write a few words before requesting a continuation.");
    return;
  }
  invalidate();
  setError("");
  $("accepted-note").replaceChildren();
  undo = null;
  const requestId = crypto.randomUUID();
  snapshot = {
    revision,
    text: editor.value,
    cursor: caret.start,
    docId: activeId,
  };
  const captured = snapshot;
  const controller = new AbortController();
  const timer = setTimeout(() => {
    if (pending?.id === requestId) {
      invalidate();
      setError(
        "The request took too long. Your draft is safe; please try again.",
      );
    }
  }, 95000);
  pending = { id: requestId, controller, timer };
  updateGenerate();
  $("idle").hidden = true;
  $("request-progress").hidden = false;
  $("request-state").textContent = "Writing…";
  $("progress-copy").textContent = "Writing…";
  try {
    const token = await challenge();
    if (pending?.id !== requestId) return;
    // Browser selections count UTF-16 code units; the API uses Unicode code points.
    const cursor = Array.from(captured.text.slice(0, captured.cursor)).length;
    const response = await fetch("/api/complete", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": access.csrf_token,
        "X-Turnstile-Token": token,
      },
      signal: controller.signal,
      body: JSON.stringify({
        text: captured.text,
        cursor,
        request_id: requestId,
        profile_version: profile.profile_version,
        length: $("length").value,
        mode: $("mode").value,
      }),
    });
    const data = await response.json();
    if (data.access) {
      access = { ...access, ...data.access };
      renderAccess();
    }
    if (!response.ok) await refreshAccess();
    if (!response.ok)
      throw new Error(
        typeof data.detail === "string"
          ? data.detail
          : "The request could not be completed. Your draft has been kept.",
      );
    if (
      pending?.id !== requestId ||
      revision !== captured.revision ||
      activeId !== captured.docId ||
      editor.value !== captured.text ||
      data.request_id !== requestId
    )
      return;
    if (!Array.isArray(data.suggestions) || !data.suggestions.length)
      throw new Error("No usable suggestion was returned. Please try again.");
    clearTimeout(timer);
    pending = null;
    result = data;
    selected = 0;
    renderResult();
    updateGenerate();
  } catch (error) {
    if (error.name === "AbortError" || pending?.id !== requestId) return;
    clearTimeout(timer);
    pending = null;
    invalidate();
    setError(error.message || "Could not reach the writing service.");
  } finally {
    // Cancellation can still incur provider cost; refresh the server's durable count.
    refreshAccess().catch(() => {});
  }
}
function insertText(before, suggestion, after) {
  const lead =
    before && !/\s$/.test(before) && !/^[\s.,!?;:)/\]}]/.test(suggestion)
      ? " "
      : "";
  const tail =
    after && !/^\s|^[.,!?;:)/\]}]/.test(after) && !/\s$/.test(suggestion)
      ? " "
      : "";
  return lead + suggestion + tail;
}
function accept() {
  if (!result || !snapshot) return;
  const editor = $("editor");
  if (
    snapshot.revision !== revision ||
    snapshot.text !== editor.value ||
    snapshot.docId !== activeId ||
    caret.start !== snapshot.cursor
  ) {
    invalidate("Draft changed.");
    return;
  }
  const before = editor.value.slice(0, snapshot.cursor),
    after = editor.value.slice(snapshot.cursor);
  const insertion = insertText(
    before,
    result.suggestions[selected].text,
    after,
  );
  const original = editor.value,
    cursor = snapshot.cursor;
  editor.setRangeText(insertion, cursor, cursor, "end");
  caret = { start: editor.selectionStart, end: editor.selectionEnd };
  const inserted = editor.value;
  invalidate();
  undo = { text: original, cursor, inserted };
  save();
  wordCount();
  $("accepted-note").replaceChildren(document.createTextNode(""));
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "Undo";
  button.addEventListener("click", undoAccept);
  $("accepted-note").append(button);
  editor.focus();
}
function undoAccept() {
  if (!undo || $("editor").value !== undo.inserted) return;
  $("editor").value = undo.text;
  $("editor").setSelectionRange(undo.cursor, undo.cursor);
  caret = { start: undo.cursor, end: undo.cursor };
  undo = null;
  invalidate();
  save();
  wordCount();
  $("accepted-note").textContent = "";
  $("editor").focus();
}
function exportDraft() {
  const blob = new Blob(
    [`# ${$("title").value || "Untitled note"}\n\n${$("editor").value}\n`],
    { type: "text/markdown;charset=utf-8" },
  );
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download =
    ($("title").value || "untitled")
      .replace(/[^a-zA-Z0-9_-]+/g, "-")
      .slice(0, 100) + ".md";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function start() {
  try {
    await refreshAccess();
    const response = await fetch("/api/status");
    if (!response.ok) throw new Error("Could not load your writing profile.");
    profile = await response.json();
    $("profile-name").textContent = profile.profile;
    $("profile-label").textContent =
      profile.demo || profile.synthetic
        ? "Example profile"
        : "Personal profile";
    $("profile-style").textContent = profile.style;
    $("model-info").textContent =
      `Candidates: ${profile.candidate.model} (${profile.candidate.provider}). Synthesis: ${profile.synthesis.model} (${profile.synthesis.provider}). Retrieval: ${profile.retrieval}.`;
    const ready = profile.generation_ready && profile.retrieval_ready;
    $("connection-dot").classList.toggle("ready", ready);
    $("connection-status").textContent = ready
      ? "Configured"
      : !profile.retrieval_ready
        ? "Retrieval needs attention"
        : "API key required";
    storageKey = "symwrite.drafts.v2." + profile.profile_id;
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const stored = JSON.parse(raw);
        if (
          !Array.isArray(stored.docs) ||
          !stored.docs.length ||
          !stored.docs.every(
            (d) =>
              typeof d.id === "string" &&
              typeof d.title === "string" &&
              typeof d.text === "string",
          ) ||
          !stored.docs.some((d) => d.id === stored.activeId)
        )
          throw new Error("Invalid saved draft");
        docs = stored.docs;
        activeId = stored.activeId;
      }
    } catch {
      storageBlocked = true;
      setError(
        "Saved drafts could not be read. They have not been overwritten. Export your current work before clearing browser storage.",
      );
    }
    // Retire only untouched, automatically seeded examples; preserve all edited drafts.
    if (profile.demo || profile.synthetic) {
      docs = docs.filter(
        (doc) =>
          !retiredExamples.some(
            (example) =>
              doc.id === example.id &&
              doc.title === example.title &&
              doc.text === example.text,
          ),
      );
      $("examples-menu").hidden = false;
      examples.forEach((example) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = example.title;
        button.addEventListener("click", () => {
          const existing = docs.find(
            (doc) => doc.id === `example-${example.id}`,
          );
          if (existing) {
            switchDocument(existing.id);
            $("examples-menu").open = false;
            return;
          }
          const next = { ...copy(example), id: `example-${example.id}` };
          if (docs.length === 1 && !active().text && !active().title) {
            docs = [next];
            activeId = null;
          } else {
            save();
            docs.push(next);
          }
          switchDocument(next.id);
          $("examples-menu").open = false;
        });
        $("example-list").append(button);
      });
    }
    if (!docs.length)
      docs = [{ id: crypto.randomUUID(), title: "", kind: "NOTE", text: "" }];
    if (!docs.some((doc) => doc.id === activeId)) activeId = docs[0].id;
    // No save before initial document contents are restored.
    const id = activeId;
    activeId = null;
    switchDocument(id, false);
    ["title", "editor", "new-note", "export"].forEach(
      (id) => ($(id).disabled = false),
    );
    if (storageBlocked)
      setError(
        "Saved drafts could not be read. They have not been overwritten. Export your work before clearing browser storage.",
      );
    if (new URLSearchParams(location.search).get("signin") === "failed") {
      setError("Sign-in could not be verified. Please try again.");
      history.replaceState(null, "", "/");
    }
    const sourceResponse = await fetch("/api/sources");
    if (sourceResponse.ok) {
      const library = await sourceResponse.json();
      if (library.profile_version !== profile.profile_version)
        throw new Error(
          "The writing profile changed. Reload to view its current sources.",
        );
      library.sources.forEach((source) => {
        const detail = document.createElement("details");
        detail.className = "library-item";
        const summary = document.createElement("summary");
        summary.textContent = source.title;
        const text = document.createElement("p");
        text.textContent = source.text;
        detail.append(summary, text);
        $("source-library").append(detail);
      });
    }
  } catch (error) {
    setError(error.message || "Could not connect to SymWrite.");
    $("connection-status").textContent = "Service unavailable";
    $("save-status").textContent = "Not connected";
  }
}
$("sign-in").addEventListener("click", signIn);
$("account").addEventListener("click", async () => {
  if (!access?.signed_in) return signIn();
  try {
    const response = await fetch("/api/auth/logout", {
      method: "POST",
      headers: { "X-CSRF-Token": access.csrf_token },
    });
    if (!response.ok)
      throw new Error("Could not sign out. Please reload and try again.");
    await refreshAccess();
  } catch (error) {
    setError(error.message);
  }
});
window.addEventListener("focus", () => {
  if (access) refreshAccess().catch(() => {});
});
document.querySelectorAll("[data-nav-menu]").forEach((menu) => {
  menu.addEventListener("toggle", () => {
    if (menu.open)
      document.querySelectorAll("[data-nav-menu]").forEach((other) => {
        if (other !== menu) other.open = false;
      });
  });
});
document.addEventListener("click", (event) => {
  if (!event.target.closest("[data-nav-menu]")) closeMenus();
});
$("generate").addEventListener("click", generate);
$("accept").addEventListener("click", accept);
$("dismiss").addEventListener("click", () => {
  invalidate("");
  $("editor").focus();
});
$("cancel").addEventListener("click", () => invalidate("Cancelled"));
$("export").addEventListener("click", exportDraft);
$("new-note").addEventListener("click", () => {
  if (!profile) return;
  save();
  const doc = {
    id: crypto.randomUUID(),
    title: "",
    kind: "NOTE",
    text: "",
  };
  docs.push(doc);
  switchDocument(doc.id);
  $("title").focus();
  $("title").select();
});
$("editor").addEventListener("input", () => {
  undo = null;
  invalidate();
  $("accepted-note").replaceChildren();
  caret = { start: $("editor").selectionStart, end: $("editor").selectionEnd };
  save();
  wordCount();
  updateGenerate();
});
$("title").addEventListener("input", () => {
  if (!profile) return;
  save();
  renderDocuments();
});
["click", "keyup", "select"].forEach((event) =>
  $("editor").addEventListener(event, captureCaret),
);
$("editor").addEventListener("blur", captureCaret);
document.addEventListener("selectionchange", () => {
  if (document.activeElement === $("editor")) captureCaret();
});
["length", "mode"].forEach((id) =>
  $(id).addEventListener("change", () => invalidate()),
);
document.addEventListener("keydown", (event) => {
  if (
    event.key === "Escape" &&
    document.querySelector("[data-nav-menu][open]")
  ) {
    const menu = document.querySelector("[data-nav-menu][open]");
    closeMenus();
    menu.querySelector("summary").focus();
    event.preventDefault();
    return;
  }
  if (currentView !== "writing") return;
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    generate();
  }
  if (event.key === "Escape" && (pending || result)) {
    event.preventDefault();
    invalidate("");
  }
  if (
    (event.metaKey || event.ctrlKey) &&
    event.key.toLowerCase() === "z" &&
    !event.shiftKey &&
    undo &&
    document.activeElement === $("editor") &&
    $("editor").value === undo.inserted
  ) {
    event.preventDefault();
    undoAccept();
  }
});
// View changes keep the editor DOM, cursor, pending request, and review state intact.
let currentView = null;
const viewScroll = { writing: 0, about: 0 };
function showView(view, updateHistory = true) {
  if (view === currentView) return;
  if (currentView) viewScroll[currentView] = window.scrollY;
  closeMenus();
  currentView = view;
  for (const name of ["writing", "about"]) {
    const selected = name === view;
    $(name + "-tab").setAttribute("aria-selected", String(selected));
    $(name + "-tab").tabIndex = selected ? 0 : -1;
    $(name + "-panel").hidden = !selected;
  }
  document.title = view === "about" ? "What is this? · SymWrite" : "SymWrite";
  if (updateHistory) history.pushState(null, "", "#" + view);
  window.scrollTo(0, viewScroll[view]);
}
const viewTabs = [$("writing-tab"), $("about-tab")];
viewTabs.forEach((tab, index) => {
  tab.addEventListener("click", () => showView(index ? "about" : "writing"));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - index;
    viewTabs[next].focus();
    showView(next ? "about" : "writing");
  });
});
function viewFromLocation() {
  const focusWasInPanel = document.activeElement.closest('[role="tabpanel"]');
  showView(location.hash === "#about" ? "about" : "writing", false);
  // History and in-page links must not leave focus in the now-hidden panel.
  if (focusWasInPanel?.hidden)
    $(currentView + "-tab").focus({ preventScroll: true });
}
window.addEventListener("hashchange", viewFromLocation);
viewFromLocation();
start();

window.addEventListener("storage", (event) => {
  if (event.key === storageKey) {
    storageBlocked = true;
    $("save-status").textContent = "Another tab changed this notebook";
    setError(
      "This notebook changed in another tab. Export any unsaved work here, then reload to recover the latest saved drafts.",
    );
  }
});
