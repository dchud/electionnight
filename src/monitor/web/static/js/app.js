/* Global behavior: clock, theme, drawer, footnotes, keyboard, replay, tour. */
(function () {
  "use strict";

  const root = document.documentElement;
  const body = document.body;
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
    replay: { active: false, asOf: null, speed: 5, start: null, end: null },
    params() {
      const params = new URLSearchParams(body.dataset.qs || "");
      if (this.replay.active && this.replay.asOf) {
        params.set("as_of", new Date(this.replay.asOf).toISOString());
      }
      return params;
    },
    bus: new EventTarget(),
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
    const article = target.closest("[data-article]");
    if (article) {
      loadDrawer("/frag/article/" + article.dataset.article);
      return;
    }
    const claims = target.closest("[data-claims]");
    if (claims) {
      Monitor.openEvidence("kind=items&key=" + claims.dataset.claims);
      return;
    }
    const evidence = target.closest("[data-evidence]");
    if (evidence) {
      Monitor.openEvidence(evidence.dataset.evidence);
    }
  });

  document.addEventListener("change", (event) => {
    const select = event.target.closest("[data-nav-select]");
    if (select) window.location.search = select.value;
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

  /* htmx integration */
  document.addEventListener("htmx:afterSwap", (event) => {
    paintDots(event.detail.target);
    if (event.detail.target.id === "drawer-body") openDrawer();
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
    if (event.detail.target !== wireLive) return;
    while (wireLive.children.length > WIRE_LIVE_MAX) {
      wireLive.lastElementChild.remove();
    }
  });
  document.addEventListener("htmx:sseBeforeMessage", (event) => {
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
  }
  function startReplay(from) {
    const replay = Monitor.replay;
    replay.active = true;
    if (wireLive) wireLive.replaceChildren();
    replay.end = Date.now();
    replay.start = replay.end - REPLAY_SPAN_MS;
    replay.asOf = from || replay.start;
    root.dataset.replay = "1";
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
      window.location.reload();
    } else {
      startReplay();
    }
  }
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

  /* A snapshot refreshes charts; the hub's SSE arrives through htmx. */
  document.body.addEventListener("htmx:sseMessage", (event) => {
    if (event.detail.type === "snapshot" && !Monitor.replay.active) {
      Monitor.bus.dispatchEvent(new Event("refresh"));
    }
  });
  document.addEventListener("DOMContentLoaded", () => paintDots(document));
})();
