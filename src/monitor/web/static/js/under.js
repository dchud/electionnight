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
    const openRows = [...main.querySelectorAll("[data-rows-body][data-rows-mode]")];
    main.replaceChildren(...fresh.childNodes);
    openRows.forEach(keepRows);
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

  /* A table's rows open in place. A refresh keeps an open panel as it is, so
     it asks for no new scan. */
  function rowsBody(table) {
    return [...document.querySelectorAll("[data-rows-body]")].find(
      (element) => element.dataset.rowsBody === table
    );
  }

  function markRows(table, mode) {
    document.querySelectorAll("[data-rows-open]").forEach((control) => {
      if (control.dataset.rowsTable !== table) return;
      control.setAttribute(
        "aria-expanded",
        control.dataset.rowsOpen === mode ? "true" : "false"
      );
    });
  }

  function keepRows(old) {
    const table = old.dataset.rowsBody;
    const fresh = rowsBody(table);
    if (!fresh) return;
    fresh.replaceWith(old);
    old.closest("[data-rows-wrap]").hidden = false;
    markRows(table, old.dataset.rowsMode);
  }

  function closeRows(body) {
    delete body.dataset.rowsMode;
    delete body.dataset.rowsTicket;
    body.replaceChildren();
    body.closest("[data-rows-wrap]").hidden = true;
    markRows(body.dataset.rowsBody, "");
  }

  let rowsTickets = 0;

  async function showRows(body, mode, url) {
    const ticket = String(++rowsTickets);
    body.dataset.rowsMode = mode;
    body.dataset.rowsTicket = ticket;
    body.closest("[data-rows-wrap]").hidden = false;
    markRows(body.dataset.rowsBody, mode);
    let html;
    try {
      const response = await fetch(url, { cache: "no-store" });
      html = await response.text();
    } catch (error) {
      html =
        '<p class="banner-warn u-error">The server did not answer, so no ' +
        "rows are shown. Close this and try again.</p>";
    }
    if (body.dataset.rowsTicket !== ticket) return;
    /* The server escapes every value in this fragment. */
    body.innerHTML = html;
  }

  function rowsUrl(table, mode) {
    const query = new URLSearchParams({ table, mode });
    return "/under/rows?" + query.toString();
  }

  function onRowsClick(target) {
    const open = target.closest("[data-rows-open]");
    if (open) {
      const body = rowsBody(open.dataset.rowsTable);
      const mode = open.dataset.rowsOpen;
      if (body.dataset.rowsMode === mode) closeRows(body);
      else showRows(body, mode, rowsUrl(open.dataset.rowsTable, mode));
      return;
    }
    const body = target.closest("[data-rows-body]");
    if (!body) return;
    const go = target.closest("[data-rows-go]");
    if (go) showRows(body, body.dataset.rowsMode, go.dataset.rowsGo);
    if (target.closest("[data-rows-close]")) closeRows(body);
  }

  /* The clipboard API needs a secure context, which a LAN address is not. */
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (error) {
        /* Fall through to a selection. */
      }
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (error) {
      copied = false;
    }
    area.remove();
    return copied;
  }

  async function onCopyClick(button) {
    const block = button.closest(".u-sql-block");
    const copied = await copyText(
      block.querySelector("pre.u-sql").textContent.trim()
    );
    button.focus();
    button.textContent = copied ? "Copied" : "Copy failed";
    window.setTimeout(() => {
      button.textContent = "Copy";
    }, 1500);
  }

  document.addEventListener("click", (event) => {
    onRowsClick(event.target);
    const copy = event.target.closest("[data-copy-sql]");
    if (copy) onCopyClick(copy);
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
