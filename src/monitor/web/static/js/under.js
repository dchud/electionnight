/* Refresh, theme, projector, and section keys for the under-the-hood views. */
(function () {
  "use strict";

  const REFRESH_MS = 30000;
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
        /* The setting lasts until the page reloads. */
      }
    },
  };

  /* Refresh replaces the view's content, and keeps open disclosures open. */
  async function refresh() {
    const main = document.getElementById("under-main");
    const open = new Set(
      [...main.querySelectorAll("details[data-key][open]")].map(
        (element) => element.dataset.key
      )
    );
    const scroll = window.scrollY;
    let text;
    try {
      const response = await fetch(window.location.href, { cache: "no-store" });
      if (!response.ok) return;
      text = await response.text();
    } catch (error) {
      return;
    }
    const page = new DOMParser().parseFromString(text, "text/html");
    const fresh = page.getElementById("under-main");
    if (!fresh) return;
    fresh.querySelectorAll("details[data-key]").forEach((element) => {
      if (open.has(element.dataset.key)) element.open = true;
    });
    main.replaceChildren(...fresh.childNodes);
    const stamp = page.getElementById("u-snapshot");
    if (stamp) document.getElementById("u-snapshot").textContent = stamp.textContent;
    if (window.htmx) window.htmx.process(main);
    window.scrollTo(0, scroll);
  }

  function busy() {
    const active = document.activeElement;
    if (active && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName)) {
      return !active.matches("[data-under-auto]");
    }
    const selection = window.getSelection();
    return Boolean(selection && selection.toString());
  }

  const auto = document.querySelector("[data-under-auto]");
  const allowed = body.dataset.autoRefresh === "1";
  if (auto) {
    auto.disabled = !allowed;
    auto.checked = allowed && store.get("under-auto") !== "0";
    auto.addEventListener("change", () => {
      store.set("under-auto", auto.checked ? "1" : "0");
    });
  }
  setInterval(() => {
    if (auto && auto.checked && !document.hidden && !busy()) refresh();
  }, REFRESH_MS);

  function toggleProjector() {
    if (root.dataset.projector === "1") {
      delete root.dataset.projector;
      store.set("projector", "0");
    } else {
      root.dataset.projector = "1";
      store.set("projector", "1");
    }
  }

  function toggleTheme() {
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    store.set("theme", next);
  }

  /* Move a whole section to the top of the screen. */
  function moveSection(step) {
    const sections = [...document.querySelectorAll(".u-section")];
    if (!sections.length) return;
    const header = document.querySelector(".u-header");
    const offset = header ? header.getBoundingClientRect().bottom : 0;
    const current = sections.findIndex(
      (section) => section.getBoundingClientRect().bottom > offset + 8
    );
    const from = current < 0 ? sections.length - 1 : current;
    const target = sections[Math.min(sections.length - 1, Math.max(0, from + step))];
    target.scrollIntoView({ block: "start" });
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-under-refresh]")) refresh();
    if (event.target.closest("[data-under-projector]")) toggleProjector();
    if (event.target.closest("[data-under-theme]")) toggleTheme();
  });

  document.addEventListener("keydown", (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName)) return;
    if (event.key === "p") toggleProjector();
    if (event.key === "r") refresh();
    if (event.key === "j") moveSection(1);
    if (event.key === "k") moveSection(-1);
  });

  /* A chart's hover detail: the whole column is the target. */
  const tip = document.getElementById("tip");
  document.addEventListener("mousemove", (event) => {
    const mark = event.target.closest && event.target.closest("[data-tip]");
    if (!mark || !tip) {
      if (tip) tip.hidden = true;
      return;
    }
    tip.textContent = mark.dataset.tip;
    tip.hidden = false;
    const width = tip.offsetWidth;
    tip.style.left =
      Math.min(window.innerWidth - width - 8, event.clientX + 14) + "px";
    tip.style.top = event.clientY + 14 + "px";
  });
})();
