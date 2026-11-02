/* Global behavior: clock, theme, drawer, footnotes, keyboard, replay, tour. */
(function () {
  "use strict";

  const root = document.documentElement;
  const body = document.body;
  const DIM_TIMEOUT_MS = 8000;
  const STALE_TEXT =
    "Update failed. This panel still shows the previous filters.";

  /* The event stream closes in a hidden tab and reopens on return. Each open
     stream holds one of the browser's six connections to this server, and
     past that limit every request on the page waits. */
  class PausableSource {
    constructor(url, init) {
      this.url = url;
      this.init = init;
      this.listeners = [];
      this.native = null;
      this.onopen = null;
      this.onerror = null;
      if (!document.hidden) this.open();
    }
    open() {
      if (this.native) return;
      const native = new EventSource(this.url, this.init);
      native.onopen = (event) => this.onopen && this.onopen(event);
      native.onerror = (event) => this.onerror && this.onerror(event);
      this.listeners.forEach(([name, listener]) =>
        native.addEventListener(name, listener)
      );
      this.native = native;
    }
    close() {
      if (this.native) this.native.close();
      this.native = null;
    }
    addEventListener(name, listener) {
      this.listeners.push([name, listener]);
      if (this.native) this.native.addEventListener(name, listener);
    }
    removeEventListener(name, listener) {
      this.listeners = this.listeners.filter(
        ([other, kept]) => other !== name || kept !== listener
      );
      if (this.native) this.native.removeEventListener(name, listener);
    }
    get readyState() {
      return this.native ? this.native.readyState : EventSource.CONNECTING;
    }
  }
  let stream = null;
  let openIsExpected = false;
  window.htmx.createEventSource = (url) => {
    if (stream) stream.close();
    stream = new PausableSource(url, { withCredentials: true });
    return stream;
  };
  document.addEventListener("visibilitychange", () => {
    if (!stream) return;
    if (document.hidden) {
      /* The next open is a return, whatever a switch expected of it. */
      openIsExpected = false;
      stream.close();
    } else {
      stream.open();
    }
  });
  const store = {
    get(key) {
      try {
        return localStorage.getItem(key);
      } catch (error) {
        return null;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(key, value);
      } catch (error) {
        /* Storage may be blocked; the setting lasts until reload. */
      }
    },
  };

  /* Replay state, read by chart and fragment requests. */
  const Monitor = (window.Monitor = {
    /* The five stops of the heat scale, low to high. */
    heatStops() {
      const styles = getComputedStyle(document.documentElement);
      return [1, 2, 3, 4, 5].map((step) =>
        styles.getPropertyValue(`--heat-${step}`).trim()
      );
    },
    replay: { active: false, asOf: null, speed: 5, start: null, end: null },
    params() {
      const params = new URLSearchParams(body.dataset.qs || "");
      if (this.replay.active && this.replay.asOf) {
        params.set("as_of", new Date(this.replay.asOf).toISOString());
      }
      return params;
    },
    bus: new EventTarget(),
    /* Charts and maps add a refresh here for filter switches. */
    switchers: [],
    /* Old content stays and dims until `work` finishes, or until the timeout
       if a response never comes. */
    dimWhile(element, work, onTimeout) {
      element.classList.add("panel-dim");
      const clear = () => element.classList.remove("panel-dim");
      const timer = setTimeout(() => {
        clear();
        if (onTimeout) onTimeout();
      }, DIM_TIMEOUT_MS);
      return Promise.resolve(work).finally(() => {
        clearTimeout(timer);
        clear();
      });
    },
    /* Says that a panel still shows data from the previous filters. */
    staleNote(element, shown) {
      const note = element.querySelector(":scope > .stale-note");
      if (!shown) {
        if (note) note.remove();
        return;
      }
      if (note) return;
      if (getComputedStyle(element).position === "static") {
        element.style.position = "relative";
      }
      const added = document.createElement("div");
      added.className = "stale-note";
      added.textContent = STALE_TEXT;
      element.prepend(added);
    },
  });

  function updateClock() {
    const clock = document.getElementById("clock");
    if (!clock) return;
    const when = Monitor.replay.active
      ? new Date(Monitor.replay.asOf)
      : new Date();
    clock.textContent =
      when.toLocaleTimeString("en-US", {
        timeZone: "America/New_York",
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
      }) + " ET";
  }
  setInterval(updateClock, 1000);
  updateClock();

  /* Drawer */
  const drawer = document.getElementById("drawer");
  function openDrawer() {
    drawer.dataset.open = "1";
  }
  function closeDrawer() {
    drawer.dataset.open = "0";
  }
  function loadDrawer(path) {
    openDrawer();
    window.htmx.ajax("GET", path, { target: "#drawer-body", swap: "innerHTML" });
  }
  Monitor.loadDrawer = loadDrawer;

  function evidencePath(query) {
    const params = Monitor.params();
    new URLSearchParams(query).forEach((value, key) => params.set(key, value));
    return "/frag/evidence?" + params.toString();
  }
  Monitor.openEvidence = (query) => loadDrawer(evidencePath(query));

  document.addEventListener("click", (event) => {
    const target = event.target;
    if (target.closest("[data-close-drawer]")) {
      closeDrawer();
      return;
    }
    const footnote = target.closest(".fn");
    if (footnote) {
      event.preventDefault();
      event.stopPropagation();
      loadDrawer("/frag/article/" + footnote.dataset.item);
      return;
    }
    const link = target.closest("a");
    if (link) return;
    /* A count inside a row opens its own evidence, not the row's article. */
    const evidence = target.closest("[data-evidence]");
    if (evidence) {
      Monitor.openEvidence(evidence.dataset.evidence);
      return;
    }
    const article = target.closest("[data-article]");
    if (article) {
      loadDrawer("/frag/article/" + article.dataset.article);
      return;
    }
  });

  /* Sortable tables: click a heading; numbers sort from `data-value`. */
  function cellValue(row, index, numeric) {
    const cell = row.cells[index];
    const text = cell.dataset.value ?? cell.textContent.trim();
    if (!numeric) return text.toLowerCase();
    const number = Number(text);
    return Number.isNaN(number) ? -Infinity : number;
  }
  document.addEventListener("click", (event) => {
    const button = event.target.closest("th[data-sort] button");
    if (!button) return;
    const heading = button.closest("th");
    const table = heading.closest("table");
    const index = heading.cellIndex;
    const numeric = heading.dataset.sort === "number";
    const ascending = heading.getAttribute("aria-sort") !== "ascending";
    const tbody = table.tBodies[0];
    const rows = [...tbody.rows].sort((a, b) => {
      const left = cellValue(a, index, numeric);
      const right = cellValue(b, index, numeric);
      if (left < right) return ascending ? -1 : 1;
      if (left > right) return ascending ? 1 : -1;
      return 0;
    });
    tbody.append(...rows);
    table
      .querySelectorAll("th[aria-sort]")
      .forEach((other) => other.removeAttribute("aria-sort"));
    heading.setAttribute("aria-sort", ascending ? "ascending" : "descending");
  });

  /* Row filters: a text box, and optionally rows whose toggle is pressed. */
  function applyRowFilter(control) {
    const rows = document.querySelector(control.dataset.rowFilter);
    if (!rows) return;
    const text = control.querySelector("[data-filter-text]");
    const pressed = control.querySelector("[data-filter-pressed]");
    const words = (text ? text.value : "").toLowerCase().split(/\s+/);
    [...rows.rows].forEach((row) => {
      const haystack = row.dataset.filterRow.toLowerCase();
      const matches = words.every((word) => haystack.includes(word));
      const on =
        !pressed ||
        !pressed.checked ||
        row.querySelector('[aria-pressed="true"]');
      row.hidden = !(matches && on);
    });
  }
  document.addEventListener("input", (event) => {
    const control = event.target.closest("[data-row-filter]");
    if (control) applyRowFilter(control);
  });

  /* Facets submit their form on change. */
  document.addEventListener("change", (event) => {
    if (event.target.closest("[data-autosubmit]")) {
      event.target.form.requestSubmit();
    }
  });
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form.matches("[data-replay-form]") || !Monitor.replay.active) return;
    let field = form.querySelector('input[name="as_of"]');
    if (!field) {
      field = document.createElement("input");
      field.type = "hidden";
      field.name = "as_of";
      form.appendChild(field);
    }
    field.value = new Date(Monitor.replay.asOf).toISOString();
  });

  document.addEventListener("change", (event) => {
    const select = event.target.closest("[data-nav-select]");
    if (!select) return;
    if (canSwitchInPlace()) {
      switchFilters(new URL(select.value, window.location.href).href, select);
    } else {
      window.location.search = select.value;
    }
  });

  /* Footnote popovers */
  const popover = document.getElementById("popover");
  const citationCache = new Map();
  async function showPopover(element) {
    const id = element.dataset.item;
    if (!citationCache.has(id)) {
      const response = await fetch("/frag/cite/" + id);
      citationCache.set(id, await response.text());
    }
    popover.innerHTML = citationCache.get(id);
    const box = element.getBoundingClientRect();
    popover.hidden = false;
    const width = popover.offsetWidth;
    popover.style.left =
      Math.min(box.left, window.innerWidth - width - 12) + "px";
    popover.style.top = box.bottom + 6 + "px";
  }
  document.addEventListener("mouseover", (event) => {
    const element = event.target.closest(".fn");
    if (element) showPopover(element);
  });
  document.addEventListener("mouseout", (event) => {
    if (event.target.closest(".fn")) popover.hidden = true;
  });

  /* Tone dots use the diverging palette, set from their value. */
  function paintDots(scope) {
    const styles = getComputedStyle(root);
    const negative = styles.getPropertyValue("--tone-neg");
    const middle = styles.getPropertyValue("--tone-mid");
    const positive = styles.getPropertyValue("--tone-pos");
    scope.querySelectorAll("[data-tone]").forEach((dot) => {
      const tone = Number(dot.dataset.tone);
      dot.style.background =
        Math.abs(tone) < 0.25 ? middle : tone < 0 ? negative : positive;
    });
    scope.querySelectorAll("[data-party]").forEach((chip) => {
      const party = (chip.dataset.party || "").toLowerCase();
      chip.style.background = party.startsWith("dem")
        ? styles.getPropertyValue("--party-dem")
        : party.startsWith("rep")
          ? styles.getPropertyValue("--party-rep")
          : styles.getPropertyValue("--party-other");
    });
  }
  Monitor.paintDots = paintDots;

  /* A panel not yet updated for the filters on show says so if it fails. */
  function noteIfAwaitingFilters(panel) {
    if (panel && panel.dataset && panel.dataset.awaitingFilters) {
      Monitor.staleNote(panel, true);
    }
  }

  /* htmx integration */
  document.addEventListener("htmx:afterSwap", (event) => {
    /* SSE swaps carry no target in their detail. */
    const swapped = event.detail.target || event.target;
    paintDots(swapped);
    if (swapped.id === "drawer-body") openDrawer();
    if (swapped.classList) swapped.classList.remove("panel-dim");
    if (swapped.dataset && swapped.dataset.awaitingFilters) {
      delete swapped.dataset.awaitingFilters;
      Monitor.staleNote(swapped, false);
    }
  });
  /* A panel's first content fades in. A refresh replaces content silently. */
  document.addEventListener("htmx:beforeSwap", (event) => {
    const target = event.detail.target;
    if (target && target.matches("[hx-get]") && !target.children.length) {
      target.classList.add("panel-arrive");
    }
  });
  document.addEventListener("animationend", (event) => {
    if (event.animationName === "panel-arrive") {
      event.target.classList.remove("panel-arrive");
    }
  });
  ["htmx:responseError", "htmx:sendError", "htmx:timeout"].forEach((kind) => {
    document.addEventListener(kind, (event) => {
      const target = event.detail.target;
      if (target && target.classList) target.classList.remove("panel-dim");
      noteIfAwaitingFilters(target);
    });
  });
  /* A missing item still explains itself in the drawer. */
  document.addEventListener("htmx:beforeSwap", (event) => {
    if (
      event.detail.xhr.status === 404 &&
      event.detail.target.id === "drawer-body"
    ) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });
  document.addEventListener("htmx:configRequest", (event) => {
    if (Monitor.replay.active && Monitor.replay.asOf) {
      event.detail.parameters.as_of = new Date(
        Monitor.replay.asOf
      ).toISOString();
    }
  });
  document.addEventListener("htmx:beforeRequest", (event) => {
    const trigger = event.detail.requestConfig.triggeringEvent;
    if (Monitor.replay.active && trigger && trigger.type.startsWith("sse:")) {
      event.preventDefault();
    }
  });

  /* The wire pauses while hovered, and catches up on leaving. */
  const wirePanel = document.getElementById("wire-panel");
  const wireLive = document.getElementById("wire-live");
  const WIRE_LIVE_MAX = 100;
  let wirePaused = false;
  let wireMissed = false;
  if (wirePanel) {
    wirePanel.addEventListener("mouseenter", () => {
      wirePaused = true;
    });
    wirePanel.addEventListener("mouseleave", () => {
      wirePaused = false;
      if (wireMissed) {
        wireMissed = false;
        wireLive.replaceChildren();
        body.dispatchEvent(new Event("wire-resume"));
      }
    });
  }
  document.addEventListener("htmx:afterSettle", (event) => {
    if ((event.detail.target || event.target) !== wireLive) return;
    while (wireLive.children.length > WIRE_LIVE_MAX) {
      wireLive.lastElementChild.remove();
    }
  });
  /* Rows the page has both ways appear once. */
  function dropRepeatedRows() {
    const wire = document.getElementById("wire");
    if (!wire || !wireLive) return;
    const live = new Set(
      [...wireLive.children].map((row) => row.dataset.article)
    );
    [...wire.children].forEach((row) => {
      if (live.has(row.dataset.article)) row.remove();
    });
  }
  let wireLiveStale = false;
  document.addEventListener("htmx:afterSettle", (event) => {
    const swapped = event.detail.target || event.target;
    if (swapped && swapped.id === "wire" && wireLiveStale) {
      wireLiveStale = false;
      wireLive.replaceChildren();
    }
    if (swapped === wireLive || (swapped && swapped.id === "wire")) {
      dropRepeatedRows();
    }
  });
  /* The server sends this on every new stream, once it has noted what is
     visible. Reloading the list now loses nothing from a disconnect. */
  function reloadWire() {
    if (Monitor.replay.active) return;
    if (wirePaused) {
      wireMissed = true;
      return;
    }
    wireLive.replaceChildren();
    body.dispatchEvent(new Event("wire-resume"));
  }
  document.addEventListener("htmx:sseBeforeMessage", (event) => {
    if (event.detail.type === "wire-ready") {
      event.preventDefault();
      reloadWire();
      return;
    }
    if (event.detail.type !== "wire") return;
    if (Monitor.replay.active) {
      event.preventDefault();
    } else if (wirePaused) {
      wireMissed = true;
      event.preventDefault();
    }
  });

  /* Theme and projector */
  function setTheme(theme) {
    root.dataset.theme = theme;
    store.set("theme", theme);
    Monitor.bus.dispatchEvent(new Event("theme"));
    paintDots(document);
  }
  function setProjector(on) {
    if (on) {
      root.dataset.projector = "1";
    } else {
      delete root.dataset.projector;
    }
    store.set("projector", on ? "1" : "0");
    Monitor.bus.dispatchEvent(new Event("theme"));
    paintDots(document);
  }
  function toggleProjector() {
    setProjector(root.dataset.projector !== "1");
  }
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-toggle-theme]")) {
      setTheme(root.dataset.theme === "dark" ? "light" : "dark");
    }
    if (event.target.closest("[data-toggle-projector]")) toggleProjector();
    if (event.target.closest("[data-toggle-replay]")) toggleReplay();
    if (event.target.closest("[data-toggle-tour]")) toggleTour();
  });

  /* Replay: a client-driven clock. */
  const TICK_MS = 2000;
  const REPLAY_SPAN_MS = 6 * 3600 * 1000;
  let replayTimer = null;
  function replayTick() {
    const replay = Monitor.replay;
    replay.asOf = Math.min(replay.asOf + TICK_MS * replay.speed, replay.end);
    syncReplayUi();
    body.dispatchEvent(new Event("replay-tick"));
    Monitor.bus.dispatchEvent(new Event("refresh"));
    if (replay.asOf >= replay.end) clearInterval(replayTimer);
  }
  function syncReplayUi() {
    const scrubber = document.getElementById("replay-scrubber");
    const label = document.getElementById("replay-time");
    if (scrubber) scrubber.value = String(Monitor.replay.asOf);
    if (label) {
      label.textContent = new Date(Monitor.replay.asOf).toLocaleString(
        "en-US",
        {
          timeZone: "America/New_York",
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
        }
      );
    }
    updateClock();
    syncCardLinks();
  }
  function startReplay(from) {
    const replay = Monitor.replay;
    replay.active = true;
    if (wireLive) wireLive.replaceChildren();
    replay.end = Date.now();
    replay.start = Math.min(replay.end - REPLAY_SPAN_MS, from || Infinity);
    replay.asOf = from || replay.start;
    root.dataset.replay = "1";
    const replayNote = document.getElementById("replay-note");
    if (replayNote) replayNote.hidden = false;
    const scrubber = document.getElementById("replay-scrubber");
    if (scrubber) {
      scrubber.min = String(replay.start);
      scrubber.max = String(replay.end);
    }
    const pill = document.getElementById("mode-pill");
    if (pill) {
      pill.textContent = "REPLAY";
      pill.className = "pill pill-replay";
    }
    syncReplayUi();
    clearInterval(replayTimer);
    replayTimer = setInterval(replayTick, TICK_MS);
    body.dispatchEvent(new Event("replay-tick"));
    Monitor.bus.dispatchEvent(new Event("refresh"));
  }
  function toggleReplay() {
    if (Monitor.replay.active) {
      clearInterval(replayTimer);
      const live = new URL(window.location.href);
      live.searchParams.delete("as_of");
      live.hash = "";
      window.location.assign(live);
    } else {
      startReplay();
    }
  }
  /* The dashboard opened at a moment, as from a card's page, replays from it. */
  function resumeReplay() {
    if (window.location.pathname !== "/") return;
    const moment = new URLSearchParams(window.location.search).get("as_of");
    const from = moment ? parseMoment(moment) : NaN;
    if (!Number.isNaN(from)) startReplay(from);
  }
  /* The server reads a time with no zone as UTC, so this does too. */
  function parseMoment(text) {
    const iso = text.replace(" ", "+");
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(iso)) return NaN;
    return Date.parse(/(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`);
  }
  resumeReplay();
  document.addEventListener("input", (event) => {
    if (event.target.id === "replay-scrubber") {
      Monitor.replay.asOf = Number(event.target.value);
      syncReplayUi();
      body.dispatchEvent(new Event("replay-tick"));
      Monitor.bus.dispatchEvent(new Event("refresh"));
    }
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-speed]");
    if (!button) return;
    Monitor.replay.speed = Number(button.dataset.speed);
    button.parentElement
      .querySelectorAll("button")
      .forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
  });

  /* Tour: full-screen rotation through the main panels. */
  const TOUR_PANELS = [
    "panel-rising",
    "panel-brief",
    "panel-map",
    "panel-stream",
    "panel-bump",
    "panel-calls",
  ];
  const tour = { on: false, paused: false, index: -1, started: 0, timer: null };
  function tourSeconds() {
    return Number(body.dataset.tourSeconds) || 30;
  }
  function clearTourPanel() {
    document
      .querySelectorAll(".tour-panel")
      .forEach((panel) => panel.classList.remove("tour-panel"));
  }
  function showTourPanel() {
    clearTourPanel();
    tour.index = (tour.index + 1) % TOUR_PANELS.length;
    const panel = document.getElementById(TOUR_PANELS[tour.index]);
    if (panel) panel.classList.add("tour-panel");
    tour.started = Date.now();
    window.dispatchEvent(new Event("resize"));
  }
  function tourFrame() {
    if (!tour.on) return;
    const bar = document.getElementById("tour-bar");
    if (!tour.paused) {
      const elapsed = (Date.now() - tour.started) / 1000;
      bar.style.width = Math.min(elapsed / tourSeconds(), 1) * 100 + "%";
      if (elapsed >= tourSeconds()) showTourPanel();
    }
    tour.timer = requestAnimationFrame(tourFrame);
  }
  function toggleTour() {
    tour.on = !tour.on;
    tour.paused = false;
    if (tour.on) {
      tour.index = -1;
      showTourPanel();
      tourFrame();
    } else {
      cancelAnimationFrame(tour.timer);
      clearTourPanel();
      document.getElementById("tour-bar").style.width = "0";
      window.dispatchEvent(new Event("resize"));
    }
  }
  function pauseTour() {
    if (tour.on) tour.paused = true;
  }
  ["pointerdown", "wheel"].forEach((kind) =>
    document.addEventListener(kind, pauseTour, { passive: true })
  );

  /* Keyboard */
  function cycle(group) {
    const links = [...document.querySelectorAll(`[data-cycle="${group}"] a`)];
    if (!links.length) return;
    const current = links.findIndex(
      (link) => link.getAttribute("aria-current") === "true"
    );
    links[(current + 1) % links.length].click();
  }
  document.addEventListener("keydown", (event) => {
    const tag = (event.target.tagName || "").toLowerCase();
    if (["input", "textarea", "select"].includes(tag) || event.metaKey) return;
    const key = event.key;
    if (key === "Enter" && event.target.matches('[role="button"][tabindex]')) {
      event.target.click();
      return;
    }
    if (key === "Escape") {
      closeDrawer();
      if (tour.on) toggleTour();
      return;
    }
    if (key >= "1" && key <= "5") {
      const band = document.getElementById("band-" + key);
      if (band) band.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    if (tour.on && key !== "t") pauseTour();
    if (key === "f") cycle("focus");
    if (key === "w") cycle("window");
    if (key === "p") toggleProjector();
    if (key === "r") toggleReplay();
    if (key === "t") toggleTour();
    if (key === "/") {
      event.preventDefault();
      window.location.href = "/search?" + (body.dataset.qs || "");
    }
    if (key === "ArrowLeft") {
      const button = document.querySelector('[data-brief="prev"]:not([disabled])');
      if (button) button.click();
    }
    if (key === "ArrowRight") {
      const button = document.querySelector('[data-brief="next"]:not([disabled])');
      if (button) button.click();
    }
  });

  /* Filter controls on the home page switch in place: the old content stays
     until the new content arrives. Any failure falls back to a page load. */
  function canSwitchInPlace() {
    return (
      window.location.pathname === "/" &&
      Boolean(document.getElementById("main"))
    );
  }
  function panelPath(element) {
    const url = element.getAttribute("hx-get");
    return new URL(url, window.location.href).pathname;
  }
  function swapPanels(page) {
    const panels = new Map();
    document.querySelectorAll("[hx-get]").forEach((element) => {
      const path = panelPath(element);
      if (!panels.has(path) && !element.closest("#drawer")) {
        panels.set(path, element);
      }
    });
    page.querySelectorAll("[hx-get]").forEach((model) => {
      const old = panels.get(panelPath(model));
      if (!old) throw new Error(`no panel for ${panelPath(model)}`);
      if (old.getAttribute("hx-get") === model.getAttribute("hx-get")) return;
      /* htmx reads the URL when it processes an element, so a new URL needs a
         new element. It takes over the old content until its response. */
      const fresh = document.importNode(model, false);
      fresh.append(...old.childNodes);
      /* A response still on its way to the old element lands nowhere. */
      old.replaceWith(fresh);
      fresh.dataset.awaitingFilters = "1";
      Monitor.dimWhile(fresh, new Promise(() => {}), () =>
        noteIfAwaitingFilters(fresh)
      );
      window.htmx.process(fresh);
    });
  }
  function replaceControls(page) {
    const active = document.activeElement;
    const focused = active && active.dataset ? active.dataset.filter : "";
    const controls = document.querySelector("nav.controls");
    const next = page.querySelector("nav.controls");
    if (!controls || !next) throw new Error("no controls");
    controls.replaceWith(document.importNode(next, true));
    const title = page.querySelector(".app-title a");
    if (title) {
      document
        .querySelector(".app-title a")
        .setAttribute("href", title.getAttribute("href"));
    }
    if (focused) {
      const control = document.querySelector(`[data-filter="${focused}"]`);
      if (control) control.focus();
    }
  }
  /* A card's link to its SQL carries the filters, and it is no panel, so
     swapping the panels leaves it behind. */
  function updateCardLinks(page) {
    page.querySelectorAll("[data-card-link]").forEach((model) => {
      const link = document.querySelector(
        `[data-card-link="${model.dataset.cardLink}"]`
      );
      if (link) link.setAttribute("href", model.getAttribute("href"));
    });
    syncCardLinks();
  }
  /* In a replay, a card's link carries the moment on screen. */
  function syncCardLinks() {
    if (!Monitor.replay.active || !Monitor.replay.asOf) return;
    const moment = new Date(Monitor.replay.asOf).toISOString();
    document.querySelectorAll("[data-card-link]").forEach((link) => {
      const url = new URL(link.getAttribute("href"), window.location.href);
      url.searchParams.set("as_of", moment);
      link.setAttribute("href", url.pathname + url.search + url.hash);
    });
  }
  /* The control shows the choice at once, and `f` and `w` advance from it. */
  function markChosen(control) {
    if (!control) return;
    const group = control.closest(".control-group");
    if (!group) return;
    group.querySelectorAll("a[aria-current]").forEach((link) => {
      link.setAttribute("aria-current", String(link === control));
    });
  }
  function applySwitch(url, page, addToHistory) {
    if (!page.getElementById("main")) throw new Error("not the home page");
    if (addToHistory) window.history.pushState({}, "", url);
    shownSearch = window.location.search;
    document.title = page.title;
    Object.assign(body.dataset, page.body.dataset);
    wireLiveStale = true;
    reconnectStream(page.body.getAttribute("sse-connect"));
    replaceControls(page);
    updateCardLinks(page);
    swapPanels(page);
    Monitor.switchers.forEach((refresh) => refresh());
  }
  let shownSearch = window.location.search;
  let switchCount = 0;
  let switchRequest = null;
  async function switchFilters(url, control, addToHistory = true) {
    switchCount += 1;
    const mine = switchCount;
    if (switchRequest) switchRequest.abort();
    switchRequest = new AbortController();
    markChosen(control);
    try {
      const response = await fetch(url, {
        signal: switchRequest.signal,
        headers: { Accept: "text/html" },
      });
      if (!response.ok) throw new Error(`${url}: ${response.status}`);
      const text = await response.text();
      if (mine !== switchCount) return;
      const page = new DOMParser().parseFromString(text, "text/html");
      switchRequest = null;
      applySwitch(url, page, addToHistory);
    } catch (error) {
      if (mine !== switchCount) return;
      switchRequest = null;
      console.warn("filter switch fell back to a page load", error);
      window.location.href = url;
    }
  }
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-filter]");
    if (!link || !canSwitchInPlace()) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    switchFilters(link.href, link);
  });
  /* A switch on its way is replaced, even by a return to what is shown. */
  window.addEventListener("popstate", () => {
    if (
      canSwitchInPlace() &&
      (switchRequest || window.location.search !== shownSearch)
    ) {
      switchFilters(window.location.href, null, false);
    }
  });

  /* The server filters the stream by its URL, so a switch reopens it. */
  function reconnectStream(url) {
    body.setAttribute("sse-connect", url);
    if (!stream) return;
    stream.url = url;
    stream.close();
    /* In a hidden tab the stream opens on return, which refreshes panels. */
    if (!document.hidden) {
      openIsExpected = true;
      stream.open();
    }
  }

  /* Events were missed while the stream was down, so refresh every panel. */
  let streamOpened = false;
  document.body.addEventListener("htmx:sseOpen", () => {
    if (!streamOpened) {
      streamOpened = true;
      openIsExpected = false;
      return;
    }
    if (openIsExpected) {
      openIsExpected = false;
      return;
    }
    if (Monitor.replay.active) return;
    ["snapshot", "brief", "status"].forEach((kind) => {
      document
        .querySelectorAll(`[hx-trigger*="sse:${kind}"]`)
        .forEach((panel) => window.htmx.trigger(panel, `sse:${kind}`));
    });
    Monitor.bus.dispatchEvent(new Event("refresh"));
  });

  /* A snapshot refreshes charts; the hub's SSE arrives through htmx. */
  document.body.addEventListener("htmx:sseMessage", (event) => {
    if (event.detail.type === "snapshot" && !Monitor.replay.active) {
      Monitor.bus.dispatchEvent(new Event("refresh"));
    }
  });
  document.addEventListener("DOMContentLoaded", () => paintDots(document));

  /* A story chip says what it means at once, above the chip, where a large
     cursor does not cover it. A title stays in the page until first use, so
     it still works without this. */
  const CHIPS =
    ".badge[title], .chip[title], .glyph[title], .tone-dot[title], [data-tip]";

  function placeChipTip(tip, chip) {
    const box = chip.getBoundingClientRect();
    const gap = 6;
    const left = box.left + box.width / 2 - tip.offsetWidth / 2;
    const above = box.top - gap - tip.offsetHeight;
    tip.style.left =
      Math.max(4, Math.min(left, window.innerWidth - tip.offsetWidth - 4)) +
      "px";
    tip.style.top = (above >= 4 ? above : box.bottom + gap) + "px";
  }

  function hideChipTip() {
    const tip = document.getElementById("tip");
    if (tip && tip.dataset.chip) {
      tip.hidden = true;
      delete tip.dataset.chip;
    }
  }

  document.addEventListener("pointerover", (event) => {
    const chip = event.target.closest && event.target.closest(CHIPS);
    const tip = document.getElementById("tip");
    if (!chip || !tip) return;
    if (chip.title) {
      chip.dataset.tip = chip.title;
      chip.setAttribute("aria-label", chip.title);
      chip.removeAttribute("title");
    }
    tip.textContent = chip.dataset.tip;
    tip.dataset.chip = "1";
    tip.hidden = false;
    placeChipTip(tip, chip);
  });
  document.addEventListener("pointerout", (event) => {
    const chip = event.target.closest && event.target.closest("[data-tip]");
    if (chip && !chip.contains(event.relatedTarget)) hideChipTip();
  });
  document.addEventListener("htmx:beforeSwap", hideChipTip);
  document.addEventListener("scroll", hideChipTip, true);
})();
