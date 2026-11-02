/* Marks the section in view in the Methods navigation. */
(function () {
  "use strict";

  const MOVED_PIXELS = 2;
  const nav = document.querySelector("[data-doc-nav]");
  if (!nav) return;
  const links = [...nav.querySelectorAll("a[href^='#']")];
  const targets = links
    .map((link) => ({
      link,
      element: document.getElementById(link.hash.slice(1)),
    }))
    .filter((target) => target.element);

  function offset() {
    const header = document.querySelector(".app-header");
    return header ? header.getBoundingClientRect().bottom : 0;
  }

  /* A section chosen in the navigation stays marked until the page moves from
     where the jump left it, because a short section at the end of the page
     cannot reach the top. */
  let chosen = null;
  let leftAt = null;

  function choose(target) {
    chosen = target;
    leftAt = null;
    mark();
    window.requestAnimationFrame(() => {
      leftAt = window.scrollY;
    });
  }

  function inView() {
    const line = offset() + 40;
    let current = targets[0];
    for (const target of targets) {
      if (target.element.getBoundingClientRect().top <= line) {
        current = target;
      }
    }
    const atEnd =
      window.innerHeight + window.scrollY >=
      document.documentElement.scrollHeight - 2;
    return atEnd ? targets[targets.length - 1] : current;
  }

  function mark() {
    const current = chosen || inView();
    for (const { link } of targets) {
      if (link === current.link) {
        link.setAttribute("aria-current", "true");
      } else {
        link.removeAttribute("aria-current");
      }
    }
    const box = nav.getBoundingClientRect();
    const place = current.link.getBoundingClientRect();
    if (place.top < box.top || place.bottom > box.bottom) {
      nav.scrollTop += place.top - box.top - box.height / 3;
    }
  }

  function setTop() {
    document.documentElement.style.setProperty(
      "--doc-top",
      `${Math.round(offset()) + 8}px`
    );
  }

  nav.addEventListener("click", (event) => {
    const link = event.target.closest("a");
    const target = targets.find((entry) => entry.link === link);
    if (target) choose(target);
  });
  /* The Back and Forward buttons change the hash without a click. */
  window.addEventListener("hashchange", () => {
    const hash = window.location.hash.slice(1);
    const target = targets.find((entry) => entry.link.hash.slice(1) === hash);
    if (target) {
      choose(target);
    } else {
      chosen = null;
      mark();
    }
  });

  let waiting = false;
  window.addEventListener(
    "scroll",
    () => {
      if (waiting) return;
      waiting = true;
      window.requestAnimationFrame(() => {
        waiting = false;
        if (
          chosen &&
          leftAt !== null &&
          Math.abs(window.scrollY - leftAt) > MOVED_PIXELS
        ) {
          chosen = null;
        }
        mark();
      });
    },
    { passive: true }
  );
  window.addEventListener("resize", () => {
    setTop();
    mark();
  });
  /* The header grows when its figures load. The navigation stays below it. */
  const header = document.querySelector(".app-header");
  if (header && window.ResizeObserver) {
    new ResizeObserver(setTop).observe(header);
  }
  setTop();
  mark();
})();
