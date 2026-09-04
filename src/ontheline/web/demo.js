"use strict";
/* Guided demonstration.
 *
 * Loaded on every page but inert unless the URL carries ?demo=1. It drives the
 * real console the way a person would — it types into the real forms, clicks the
 * real controls, and every state change comes back from the real API. Nothing is
 * mocked and nothing is drawn over the product except the caption bar and the
 * spotlight ring. Press Escape at any point to stop it and take the product over.
 *
 * demo_video/narrate.py speaks each beat's caption and writes the measured
 * per-beat durations into window.__OTL_TIMING, so the visuals pace themselves
 * to the voice-over and both share one clock. Without that array the demo falls
 * back to a reading-speed estimate.
 */
(function () {
  if (new URLSearchParams(location.search).get("demo") !== "1") return;

  const CREDENTIALS = { username: "judge", password: "ontheline-demo-2026" };
  const WORDS_PER_MINUTE = 168;
  const MINIMUM_BEAT_MS = 3200;
  const READING_PAD_MS = 900;

  let stopped = false;
  let overlay = null;
  let caption = null;
  let spotlight = null;
  let cursor = null;
  let card = null;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
  const timing = () => (Array.isArray(window.__OTL_TIMING) ? window.__OTL_TIMING : null);

  let callLengths = {};

  function estimate(beat) {
    /* A beat that plays a call is as long as the call, not as long as its caption. */
    if (beat.call && callLengths[beat.call]) {
      return callLengths[beat.call] * 1000 + READING_PAD_MS;
    }
    const spoken = (beat.caption || "").trim().split(/\s+/).filter(Boolean).length;
    return Math.max(MINIMUM_BEAT_MS, (spoken / WORDS_PER_MINUTE) * 60_000 + READING_PAD_MS);
  }

  async function loadCallLengths() {
    try {
      const manifest = await (await fetch("/assets/call/manifest.json")).json();
      const fast = Boolean(window.__OTL_CALL_FAST);
      for (const [scenario, turns] of Object.entries(manifest)) {
        callLengths[scenario] = turns.reduce(
          (total, turn) => total + (fast ? 0.85 : turn.seconds + 0.35), 4,
        );
      }
    } catch { callLengths = {}; }
  }

  function build() {
    overlay = document.createElement("div");
    overlay.id = "demo-overlay";
    overlay.innerHTML = `
      <div id="demo-card" class="demo-card" popover="manual">
        <div class="demo-card-inner">
          <p class="eyebrow" id="demo-card-kicker"></p>
          <h1 id="demo-card-title"></h1>
          <p id="demo-card-body"></p>
          <img id="demo-card-image" alt="On the Line system architecture" hidden>
        </div>
      </div>
      <div id="demo-spotlight" class="demo-spotlight" popover="manual"></div>
      <div id="demo-cursor" class="demo-cursor"></div>
      <div id="demo-caption" class="demo-caption" popover="manual">
        <span class="demo-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M8 12h8"/><circle cx="4.6" cy="12" r="3.2" fill="currentColor" stroke="none"/><circle cx="19.4" cy="12" r="3.2" fill="currentColor" stroke="none"/></svg></span>
        <p id="demo-caption-text"></p>
        <span class="demo-hint"><b id="demo-progress"></b> · Esc to take over</span>
      </div>`;
    document.body.append(overlay);
    /* Closing a <dialog> clears the top layer, taking the ring but not the cursor with it. */
    for (const dialog of document.querySelectorAll("dialog")) {
      dialog.addEventListener("close", () => hideSpotlight());
    }
    card = overlay.querySelector("#demo-card");
    spotlight = overlay.querySelector("#demo-spotlight");
    cursor = overlay.querySelector("#demo-cursor");
    caption = overlay.querySelector("#demo-caption");
    caption.showPopover();
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") stop();
    }, true);
  }

  function stop() {
    if (stopped) return;
    stopped = true;
    overlay?.remove();
    document.documentElement.classList.remove("demo-running");
  }

  /* A dialog opened with showModal() enters the top layer above anything shown
     earlier, so the caption and spotlight are re-shown to sit back on top. */
  function raise() {
    for (const element of [spotlight, caption]) {
      if (!element || !element.matches(":popover-open")) continue;
      element.hidePopover();
      element.showPopover();
    }
  }

  function setCaption(index, total, text) {
    overlay.querySelector("#demo-caption-text").textContent = text;
    overlay.querySelector("#demo-progress").textContent = `${index + 1}/${total}`;
  }

  function showCard({ kicker, title, body, image }) {
    overlay.querySelector("#demo-card-kicker").textContent = kicker || "";
    overlay.querySelector("#demo-card-title").textContent = title || "";
    overlay.querySelector("#demo-card-body").textContent = body || "";
    const picture = overlay.querySelector("#demo-card-image");
    picture.hidden = !image;
    if (image) picture.src = image;
    card.classList.toggle("with-image", Boolean(image));
    hideSpotlight();
    card.showPopover();
    raise();
  }

  function hideCard() {
    if (card.matches(":popover-open")) card.hidePopover();
  }

  function hideSpotlight() {
    if (spotlight.matches(":popover-open")) spotlight.hidePopover();
    cursor.classList.remove("visible");
  }

  async function waitFor(selector, { timeout = 15_000, text = null } = {}) {
    const deadline = performance.now() + timeout;
    while (performance.now() < deadline) {
      if (stopped) throw new Error("demonstration stopped");
      const candidates = [...document.querySelectorAll(selector)];
      const match = text
        ? candidates.find((node) => node.textContent.includes(text))
        : candidates[0];
      if (match && match.getClientRects().length) return match;
      await sleep(70);
    }
    throw new Error(`demo could not find ${selector}${text ? ` containing “${text}”` : ""}`);
  }

  function frame(element) {
    const box = element.getBoundingClientRect();
    const pad = 10;
    Object.assign(spotlight.style, {
      top: `${box.top - pad}px`,
      left: `${box.left - pad}px`,
      width: `${box.width + pad * 2}px`,
      height: `${box.height + pad * 2}px`,
    });
    if (!spotlight.matches(":popover-open")) spotlight.showPopover();
    Object.assign(cursor.style, {
      top: `${box.top + box.height / 2}px`,
      left: `${box.left + Math.min(box.width / 2, 120)}px`,
    });
    cursor.classList.add("visible");
    raise();
  }

  async function point(selector, options) {
    const element = await waitFor(selector, options);
    element.scrollIntoView({ block: "center", behavior: "smooth" });
    await sleep(260);
    frame(element);
    await sleep(320);
    return element;
  }

  async function click(selector, options) {
    const element = await point(selector, options);
    element.click();
    await sleep(280);
    /* A click that navigates leaves the ring framing something the viewer can no longer see. */
    if (!element.isConnected || !element.getClientRects().length) hideSpotlight();
    raise();
    return element;
  }

  async function type(selector, value) {
    const field = await point(selector);
    field.focus();
    field.value = "";
    for (const character of value) {
      if (stopped) return field;
      field.value += character;
      field.dispatchEvent(new Event("input", { bubbles: true }));
      await sleep(26);
    }
    return field;
  }

  async function choose(selector, value) {
    const select = await point(selector);
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await sleep(260);
    return select;
  }

  const beats = [
    {
      caption: "On the Line is a community resource directory that phones the food banks and clinics it lists, so that nobody travels to a door that closed in July. It confirms what is still true, and asks a person only when the answer needs judgment.",
      run: async () => showCard({
        kicker: "AWS Agents for Humans · Good Neighbor",
        title: "On the Line",
        body: "It phones the organizations it lists, publishes only what they confirmed out loud, and stops for a person when the answer needs judgment.",
      }),
    },
    {
      caption: "It is built for the people who hold a community's directory together: 211 teams, food bank networks and small nonprofits, and for the neighbours who rely on what they publish.",
      run: async () => showCard({
        kicker: "Who it is for",
        title: "The volunteers keeping a community's directory true, and the neighbours who depend on it.",
        body: "211 teams · food bank networks · libraries and small nonprofits · and the organizations they list",
      }),
    },
    {
      caption: "Nineteen million referrals a year run on directories like this one, and keeping them current is repetitive work almost nobody is funded to do. So the cost of a wrong listing lands on the person who can least afford a wasted morning.",
      run: async () => showCard({
        kicker: "The problem, and why it matters",
        title: "A stale listing is a wasted morning for the person who can least afford one.",
        body: "United Way 211, 2025: 19 million referrals across 1.6 million locally curated services.",
      }),
    },
    {
      caption: "This is the curator console, signing in to the running application. Everything after this is the product driving its own API.",
      run: async () => {
        hideCard();
        await type("#login-form input[name=username]", CREDENTIALS.username);
        await type("#login-form input[name=password]", CREDENTIALS.password);
        await click("#login-form button[type=submit]");
        await waitFor("#app-view:not([hidden]) .stats");
      },
    },
    {
      caption: "The shift starts on one sentence, counted in doors rather than records: how many are confirmed open, and how many calls are yours to make.",
      run: async () => { await point(".hero h2"); },
    },
    {
      caption: "Start where the harm actually shows up. This is the search a help-seeker uses; the public page at slash help serves exactly this without an account.",
      run: async () => {
        await click(".nav-item[data-view=intake]");
        await waitFor("#intake-form");
      },
    },
    {
      caption: "They ask for food near their postal code. Results are ranked by freshness, and every card says how recently the record was checked.",
      run: async () => {
        await type("#intake-form input[name=need]", "food groceries");
        await type("#intake-form input[name=postal_code]", "02038");
        await click("#intake-form button[type=submit]");
        await point(".result-card");
      },
    },
    {
      caption: "Taking the referral leaves a receipt, so what happens next can be traced back to this exact record.",
      run: async () => { await click(".result-card [data-referral-service]"); },
    },
    {
      caption: "The pantry has moved its hours, and the seeker finds a locked door. They report it. No name, no contact details, and what they type is redacted before it is stored.",
      run: async () => {
        await click(".result-card [data-report-service]");
        await waitFor("#form-dialog[open]");
        await choose("#form-dialog select[name=reason]", "wrong_hours");
        await type("#form-dialog textarea[name=details]", "Arrived Wednesday morning and the door was locked.");
      },
    },
    {
      caption: "That report becomes the next call, inside the same database transaction. Priority one thousand: it outranks every staleness score in the queue.",
      run: async () => {
        await click("#form-dialog-submit");
        await click(".nav-item[data-view=dashboard]");
        await point(".queue-item", { text: "Reported closed by a neighbour" });
      },
    },
    {
      caption: "Now the outbound half. The organization has given consent for this window, so On the Line places the call itself.",
      run: async () => {
        await click(".queue-item [data-run-task]");
        await waitFor("#demo-dialog[open]");
        await choose("#scenario-select", "hours_changed");
        await point(".policy-preview");
      },
    },
    {
      caption: "Listen to the call.",
      call: "hours_changed",
      run: async () => {
        await click("#run-demo-button");
        /* The dialog grows as the transcript builds, so a ring on the button would drift. */
        hideSpotlight();
        await waitFor("#call-verdict:not([hidden])", { timeout: 120_000 });
      },
    },
    {
      caption: "The model only proposes. A policy in plain Python decides, and it needs all five: an allowlisted field, an explicit statement, a confirmed readback, a canonical value, and complete coverage of what was asked. This call has them, so it applies by itself, with the provider's own words attached.",
      run: async () => {
        await point("#call-verdict");
        await sleep(8500);   /* hold the outcome while the policy is being explained */
        await click("#run-demo-button");
        await waitFor(".stats");
      },
    },
    {
      caption: "The directory now reads Wednesday, ten to one, assured today, with the provider's own words behind it.",
      run: async () => {
        await click(".nav-item[data-view=directory]");
        await point(".data-table tr", { text: "Weekly grocery pickup" });
      },
    },
    {
      caption: "The next call goes differently. The legal clinic says its tenant rights program closed at the end of July, and a closure is never the model's to publish.",
      run: async () => {
        await click(".nav-item[data-view=dashboard]");
        await click(".queue-item [data-run-task]");
        await waitFor("#demo-dialog[open]");
        await choose("#scenario-select", "program_closed");
        window.__OTL_CALL_FAST = true;
        await click("#run-demo-button");
        await waitFor("#call-verdict:not([hidden])", { timeout: 120_000 });
        await click("#run-demo-button");
      },
    },
    {
      caption: "A removal can strand somebody, so no model output is ever allowed to remove a service. It waits here instead, in a queue that survives a restart, with the current value, the proposed value, and the quote it came from.",
      run: async () => {
        await waitFor(".decision-card");
        await point(".decision-card", { text: "Tenant rights clinic" });
      },
    },
    {
      caption: "The curator decides. Approve, edit, or reject — each one writes its own event, and the note goes with it.",
      run: async () => {
        const card = await waitFor(".decision-card", { text: "Tenant rights clinic" });
        const approve = card.querySelector("[data-decision=approve]");
        frame(approve);
        await sleep(320);
        approve.click();
        await waitFor("#form-dialog[open]");
        raise();
        await type("#form-dialog textarea[name=note]", "Confirmed with the clinic director by email.");
        await click("#form-dialog-submit");
      },
    },
    {
      caption: "Every automatic change and every human decision lands in one append-only chain. Rows here cannot be edited or deleted — the database triggers refuse.",
      run: async () => {
        await click(".nav-item[data-view=activity]");
        await point(".timeline");
      },
    },
    {
      caption: "One call fixes the listing for three people at once. The neighbour who needs the service, the volunteer who maintains it, and the organization that would otherwise be phoned by twelve directories asking the same question, because the answer is published in the open Referral HSDS standard that any of them can read.",
      run: async () => showCard({
        kicker: "One call, three neighbours",
        title: "Call once. Publish for everyone.",
        body: "Open Referral HSDS 3.2, so any other directory can consume the same confirmed answer.",
      }),
    },
    {
      caption: "Underneath it: a Strands graph reads the transcript for safety, then extracts a schema-valid proposal that a goal loop holds to the domain rules. The graph can run in Bedrock AgentCore. The policy, the directory and the human decisions all live outside the model's reach.",
      run: async () => showCard({
        kicker: "Architecture",
        title: "The model proposes. Python and a person decide.",
        image: "/assets/architecture.svg",
      }),
    },
    {
      caption: "Someone found it closed. Now nobody else will. That is On the Line.",
      run: async () => showCard({
        kicker: "On the Line",
        title: "Someone found it closed. Now nobody else will.",
        body: "Open Referral HSDS 3.2 · Strands Agents · Amazon Nova 2 · MIT licensed",
      }),
    },
  ];

  document.addEventListener("otl:call-turn", (event) => {
    if (!overlay) return;
    const who = event.detail.role === "assistant" ? "On the Line" : "Provider";
    overlay.querySelector("#demo-caption-text").textContent = `${who}: ${event.detail.text}`;
  });

  async function play() {
    for (let index = 0; index < beats.length; index += 1) {
      if (stopped) return;
      const beat = beats[index];
      const started = performance.now();
      hideSpotlight();
      setCaption(index, beats.length, beat.caption);
      try {
        await beat.run();
      } catch (error) {
        if (stopped) return;
        console.warn(`demo beat ${index + 1} could not complete: ${error.message}`);
      }
      const clock = timing();
      const target = clock && clock[index] != null ? clock[index] * 1000 : estimate(beat);
      const spent = performance.now() - started;
      if (spent > target) console.warn(`demo beat ${index + 1} overran its narration by ${Math.round(spent - target)}ms`);
      await sleep(target - spent);
    }
    window.__OTL_DEMO_COMPLETE = true;
  }

  async function start() {
    document.documentElement.classList.add("demo-running");
    build();
    await loadCallLengths();
    await waitFor("#login-view:not([hidden]), #app-view:not([hidden])");
    await sleep(500);
    await play();
  }

  window.OnTheLineDemo = {
    start, stop,
    beats: beats.map((beat) => beat.caption),
    script: beats.map((beat) => ({ caption: beat.caption, call: beat.call || null })),
  };
  /* demo_video/narrate.py reads the captions without playing anything. */
  if (!window.__OTL_DEMO_MANUAL) start();
})();
