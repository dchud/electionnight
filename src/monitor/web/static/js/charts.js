/* Vega charts. A spec is embedded once; later updates only replace data. */
(function () {
  "use strict";

  const Monitor = window.Monitor;
  const charts = [];
  const THROTTLE_MS = 3000;

  /* Six validated hues that avoid party blue and red. Slots after the sixth
     rely on direct labels and hover for identity. */
  const SERIES = {
    light: [
      "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6",
      "#a0713a", "#0d93b5", "#b24aa8", "#7f9a1e",
    ],
    dark: [
      "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9",
      "#b08245", "#2a9fc0", "#c565bb", "#93ae2c",
    ],
  };

  function css(name) {
    return getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
  }

  function themeConfig() {
    const label = parseInt(css("--chart-label"), 10) || 13;
    const strong = css("--text-1");
    const soft = css("--text-2");
    const line = css("--line");
    const titleWeight = parseInt(css("--weight-label"), 10) || 500;
    const strokeScale = parseFloat(css("--stroke-scale")) || 1;
    return {
      background: null,
      font: "Inter Variable, ui-sans-serif, system-ui, sans-serif",
      view: { stroke: null },
      axis: {
        labelFontSize: label,
        titleFontSize: label,
        labelColor: soft,
        titleColor: soft,
        titleFontWeight: titleWeight,
        gridColor: line,
        domainColor: line,
        tickColor: line,
      },
      legend: {
        labelFontSize: label,
        titleFontSize: label,
        labelColor: soft,
        titleColor: soft,
        titleFontWeight: titleWeight,
      },
      header: { labelFontSize: label, labelColor: soft, titleColor: soft },
      text: { fontSize: label, color: strong },
      range: { heatmap: Monitor.heatStops() },
      line: { strokeWidth: 1.5 * strokeScale },
      circle: { size: 20 * strokeScale * strokeScale },
      rule: { color: soft },
      bar: { fill: soft },
      signals: [],
    };
  }

  function themeName() {
    return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  }

  /* Each storyline keeps its color for the life of the page. */
  const assigned = new Map();
  function colorFor(label) {
    if (label === "Other") return css("--series-other");
    if (!assigned.has(label)) assigned.set(label, assigned.size);
    const palette = SERIES[themeName()];
    return palette[assigned.get(label) % palette.length];
  }

  function applySignals(spec) {
    const params = spec.params ? [...spec.params] : [];
    const values = {
      partyDem: css("--party-dem"),
      partyRep: css("--party-rep"),
      partyOther: css("--party-other"),
      toneAccent: css("--accent"),
      ink: css("--text-1"),
      textSoft: css("--text-2"),
    };
    Object.entries(values).forEach(([name, value]) => {
      params.push({ name, value });
    });
    return { ...spec, params };
  }

  const ROW_MIN_WIDTH = 80;
  const ROW_MIN_HEIGHT = 44;
  // The gap under an inline SVG would otherwise make the cell scroll.
  const ROW_FIT_MARGIN = 8;

  function withSeriesColors(spec, rows) {
    if (!rows || !spec.layer) return spec;
    const labels = [...new Set(rows.map((row) => row.label))];
    const domain = labels.sort((a, b) =>
      a === "Other" ? 1 : b === "Other" ? -1 : 0
    );
    const copy = JSON.parse(JSON.stringify(spec));
    copy.layer.forEach((layer) => {
      const color = layer.encoding && layer.encoding.color;
      if (color && color.field === "label") {
        color.scale = { domain, range: domain.map(colorFor) };
      }
    });
    return copy;
  }

  async function fetchData(chart) {
    const params = Monitor.params();
    new URLSearchParams(chart.el.dataset.params || "").forEach((value, key) =>
      params.set(key, value)
    );
    const url = `/api/data/${chart.name}?${params.toString()}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`${url}: ${response.status}`);
    return response.json();
  }

  function binQuery(datum) {
    if (!datum.bin_start) return "";
    const start = new Date(datum.bin_start).toISOString();
    const minutes = datum.bin_minutes || 5;
    return `&bin_start=${encodeURIComponent(start)}&bin_minutes=${minutes}`;
  }

  function clickQuery(kind, datum, base) {
    if (kind === "evidence") return datum.evidence || null;
    if (kind === "bin" && datum.bin_start) return base + binQuery(datum);
    if (kind === "level_bin" && datum.bin_start && datum.level) {
      return `${base}&also=primary:${datum.level}${binQuery(datum)}`;
    }
    if (kind === "lean" && datum.bucket) {
      return datum.bucket === "All" ? base : `${base}&bucket=${datum.bucket}`;
    }
    if (kind === "bucket" && datum.key) return `kind=bucket&key=${datum.key}`;
    if (kind === "storyline" && datum.storyline_id) {
      if (datum.storyline_id === "other") return null;
      window.location.href =
        `/story/${datum.storyline_id}?${Monitor.params().toString()}`;
      return null;
    }
    if (kind === "matrix" && datum.storyline_id) {
      if (!datum.share) return null;
      const bucket = datum.bucket === "Non-US" ? "NA" : datum.bucket;
      // Coverage counts every outlet's copy, so its evidence does too.
      return `kind=storyline&key=${datum.storyline_id}&bucket=${bucket}&copies=all`;
    }
    return null;
  }

  /* Vega-Lite cannot size a chart split into rows to its container. The plots
     take what the cell leaves after the axes and the gaps between rows. */
  async function fitRows(chart) {
    const view = chart.view;
    if (!view) return;
    const row = chart.spec.encoding && chart.spec.encoding.row;
    const cell = chart.el.parentElement;
    if (!row || !cell.clientWidth || !cell.clientHeight) return;
    const rows = (chart.rows || []).map((each) => each[row.field]);
    const count = new Set(rows).size;
    if (!count) return;
    /* A new size can move the axis labels, so a second pass settles it. */
    for (let pass = 0; pass < 2; pass += 1) {
      const svg = view.container().querySelector("svg");
      const drawn = svg.getBoundingClientRect();
      const width = view.signal("child_width");
      const height = view.signal("child_height");
      const nextWidth = Math.floor(
        cell.clientWidth - (drawn.width - width) - ROW_FIT_MARGIN
      );
      const nextHeight = Math.floor(
        (cell.clientHeight - (drawn.height - count * height) - ROW_FIT_MARGIN) /
          count
      );
      const fitWidth = Math.max(ROW_MIN_WIDTH, nextWidth);
      const fitHeight = Math.max(ROW_MIN_HEIGHT, nextHeight);
      if (fitWidth === width && fitHeight === height) return;
      view.signal("child_width", fitWidth).signal("child_height", fitHeight);
      await view.resize().runAsync();
    }
  }

  async function embed(chart, data) {
    const specUrl = chart.variant
      ? `/api/spec/${chart.name}?variant=${chart.variant}`
      : `/api/spec/${chart.name}`;
    if (!chart.spec) {
      chart.spec = await (await fetch(specUrl)).json();
    }
    let spec = applySignals(chart.spec);
    spec = withSeriesColors(spec, data.table);
    spec.config = { ...(spec.config || {}), ...themeConfig() };
    delete spec.config.signals;
    /* The new chart draws out of sight, so the old one stays until it is
       ready. */
    const stage = document.createElement("div");
    stage.className = chart.stage
      ? "chart-stage chart-stage-next"
      : "chart-stage";
    chart.el.appendChild(stage);
    const result = await window.vegaEmbed(stage, spec, {
      actions: false,
      renderer: "svg",
      // Far enough from the pointer that a large cursor does not cover it.
      tooltip: {
        theme: themeName() === "dark" ? "dark" : "light",
        offsetX: 48,
        offsetY: 48,
      },
      mode: "vega-lite",
      logLevel: window.vega.Error,
    });
    chart.view = result.view;
    chart.labels = new Set((data.table || []).map((row) => row.label));
    const kind = chart.el.dataset.click;
    if (kind) {
      result.view.addEventListener("click", (_event, item) => {
        if (!item || !item.datum) return;
        const query = clickQuery(
          kind,
          item.datum.datum || item.datum,
          chart.el.dataset.baseQuery || ""
        );
        if (query) Monitor.openEvidence(query);
      });
    }
    attachCrosshair(chart);
    await pushData(chart, data);
    if (chart.spec.encoding && chart.spec.encoding.row && !chart.fitWatch) {
      chart.fitWatch = new ResizeObserver(() => fitRows(chart));
      chart.fitWatch.observe(chart.el.parentElement);
    }
    if (chart.stage) {
      chart.stage.remove();
      stage.classList.remove("chart-stage-next");
    } else {
      stage.classList.add("panel-arrive");
    }
    if (chart.shown) chart.shown.finalize();
    chart.stage = stage;
    chart.shown = result.view;
  }

  function sameLabels(chart, rows) {
    const labels = new Set(rows.map((row) => row.label));
    return (
      labels.size === chart.labels.size &&
      [...labels].every((label) => chart.labels.has(label))
    );
  }

  async function pushData(chart, data) {
    const view = chart.view;
    if (!view) return;
    const dataNames = ["table", "rules", "span"];
    for (const name of dataNames) {
      if (name in data) {
        try {
          view.data(name, data[name] || []);
        } catch (error) {
          /* The chart has no layer using this dataset. */
        }
      }
    }
    await view.resize().runAsync();
    if ("table" in data) chart.rows = data.table;
    await fitRows(chart);
  }

  async function refreshOnce(chart) {
    const qs = document.body.dataset.qs;
    const data = await fetchData(chart);
    /* The filters changed during the request, so this answer is stale. */
    if (qs !== document.body.dataset.qs) {
      chart.again = true;
      return;
    }
    const needsEmbed =
      !chart.view ||
      (chart.name === "stream" && !sameLabels(chart, data.table || []));
    if (needsEmbed) {
      await embed(chart, data);
    } else {
      await pushData(chart, data);
    }
    showEmpty(chart, data);
    chart.shownQs = qs;
    Monitor.staleNote(chart.el.parentElement, false);
  }

  /* A chart that kept an earlier filter's data says so. */
  function noteIfStale(chart) {
    const qs = document.body.dataset.qs;
    if (chart.shownQs !== undefined && chart.shownQs !== qs) {
      Monitor.staleNote(chart.el.parentElement, true);
    }
  }

  /* A refresh asked for during another runs after it. */
  function refresh(chart) {
    if (chart.running) {
      chart.again = true;
      return chart.running;
    }
    chart.running = (async () => {
      do {
        chart.again = false;
        try {
          await refreshOnce(chart);
        } catch (error) {
          console.warn("chart refresh failed", chart.name, error);
          noteIfStale(chart);
        }
      } while (chart.again);
      chart.running = null;
    })();
    return chart.running;
  }

  function showEmpty(chart, data) {
    const empty = !(data.table && data.table.length);
    chart.el.style.visibility = empty ? "hidden" : "visible";
    const holder = chart.el.parentElement;
    let note = holder.querySelector(":scope > .empty");
    if (empty && !note) {
      /* The note fills its panel, and only its panel. */
      if (getComputedStyle(holder).position === "static") {
        holder.style.position = "relative";
      }
      note = document.createElement("div");
      note.className = "empty";
      note.style.position = "absolute";
      note.style.inset = "0";
      note.textContent =
        chart.el.dataset.empty || "No data for these filters yet.";
      holder.appendChild(note);
    } else if (!empty && note) {
      note.remove();
    }
  }

  /* Crosshair: one time across every time chart. */
  function attachCrosshair(chart) {
    if (!chart.el.dataset.timeAxis || chart.crosshair) return;
    const line = document.createElement("div");
    line.style.cssText =
      "position:absolute;top:0;bottom:0;width:1px;background:var(--text-2);" +
      "pointer-events:none;opacity:.7;display:none";
    chart.el.parentElement.appendChild(line);
    chart.crosshair = line;
    chart.el.addEventListener("pointermove", (event) => {
      const scale = chart.view && chart.view.scale("x");
      if (!scale) return;
      const box = chart.el.getBoundingClientRect();
      const origin = chart.view.origin()[0] + chart.view.padding().left;
      const time = scale.invert(event.clientX - box.left - origin);
      Monitor.bus.dispatchEvent(
        new CustomEvent("crosshair", { detail: { time } })
      );
    });
    chart.el.addEventListener("pointerleave", () => {
      Monitor.bus.dispatchEvent(
        new CustomEvent("crosshair", { detail: { time: null } })
      );
    });
  }
  Monitor.bus.addEventListener("crosshair", (event) => {
    const { time } = event.detail;
    charts.forEach((chart) => {
      if (!chart.crosshair || !chart.view) return;
      const scale = chart.view.scale("x");
      if (!time || !scale) {
        chart.crosshair.style.display = "none";
        return;
      }
      const origin = chart.view.origin()[0] + chart.view.padding().left;
      const offset = chart.el.offsetLeft + origin + scale(time);
      chart.crosshair.style.left = offset + "px";
      chart.crosshair.style.display = "block";
    });
  });

  function start() {
    document.querySelectorAll("[data-chart]").forEach((el) => {
      const chart = { el, name: el.dataset.chart, variant: "", view: null };
      charts.push(chart);
      refresh(chart);
    });
    const modes = document.getElementById("stream-mode");
    if (modes) {
      modes.addEventListener("click", (event) => {
        const button = event.target.closest("button");
        if (!button) return;
        modes
          .querySelectorAll("button")
          .forEach((b) =>
            b.setAttribute("aria-pressed", String(b === button))
          );
        const chart = charts.find((item) => item.name === "stream");
        chart.variant = button.dataset.variant;
        chart.spec = null;
        chart.view = null;
        refresh(chart);
      });
    }
    Monitor.charts = charts;
    Monitor.switchers.push(() =>
      charts.forEach((chart) =>
        Monitor.dimWhile(chart.el, refresh(chart), () => noteIfStale(chart))
      )
    );
    let last = 0;
    Monitor.bus.addEventListener("refresh", () => {
      const now = Date.now();
      if (now - last < THROTTLE_MS && !Monitor.replay.active) return;
      last = now;
      charts.forEach(refresh);
    });
    Monitor.bus.addEventListener("theme", () => {
      charts.forEach((chart) => {
        chart.view = null;
        refresh(chart);
      });
    });
    let resizeTimer;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        charts.forEach((chart) => {
          if (chart.view) chart.view.resize().runAsync();
        });
      }, 150);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
