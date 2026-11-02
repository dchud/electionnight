/* Choropleth of states, from pre-projected TopoJSON. */
(function () {
  "use strict";

  const Monitor = window.Monitor;
  const USPS_BY_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY",
  };

  function drawMap(container, options) {
    const { topology, onClick } = options;
    const d3 = window.d3;
    const features = window.topojson.feature(
      topology,
      topology.objects.states
    ).features;
    const svg = d3
      .select(container)
      .append("svg")
      .attr("class", "map-svg")
      .attr("viewBox", "0 0 975 610")
      .attr("preserveAspectRatio", "xMidYMid meet")
      .attr("role", "img")
      .attr("aria-label", "Map of US states");
    const path = d3.geoPath();
    const states = svg
      .append("g")
      .selectAll("path")
      .data(features, (feature) => feature.id)
      .join("path")
      .attr("class", "map-state")
      .attr("d", path)
      .attr("data-usps", (feature) => USPS_BY_FIPS[feature.id])
      .style("fill", surfaceColor())
      .on("click", (_event, feature) => onClick(USPS_BY_FIPS[feature.id]));
    const outlines = svg.append("g");
    return { svg, states, outlines, path, features };
  }

  function legendFor(metric, color, domain) {
    const stops = d3Stops(metric, color, domain);
    const low = domain[0];
    const high = domain[domain.length - 1];
    const ticks = {
      attention: ["0.25", "1.00", "4.00"],
      volume: ["0", String(Math.round(high / 2)), String(Math.round(high))],
      tone: ["-1", "0", "+1"],
    }[metric];
    const words = {
      attention: ["below normal", "above normal"],
      volume: ["fewer stories", "more stories"],
      tone: ["negative", "positive"],
    }[metric];
    const spans = (values) =>
      values.map((value) => `<span>${value}</span>`).join("");
    return (
      `<span class="map-legend-bar" data-low="${low}" data-high="${high}" ` +
      `style="background:linear-gradient(90deg,${stops})"></span>` +
      `<span class="map-legend-row num">${spans(ticks)}</span>` +
      `<span class="map-legend-row">${spans(words)}</span>`
    );
  }

  /* Attention's scale is logarithmic, so 1.00 falls at the bar's center. */
  function d3Stops(metric, color, domain) {
    const low = domain[0];
    const high = domain[domain.length - 1];
    return Array.from({ length: 9 }, (_, index) => {
      const t = index / 8;
      return color(
        metric === "attention"
          ? low * (high / low) ** t
          : low + (high - low) * t
      );
    }).join(",");
  }

  /* Hover text stays in one place on the card, where a large cursor cannot
     cover it. It fades between places, and lingers while the pointer crosses
     a border, so moving across the map does not flicker. */
  const READOUT_FADE_MS = 110;
  const READOUT_LINGER_MS = 220;

  function makeReadout(slot, hint) {
    let hintText = hint;
    let current = null;
    let drawn = "";
    let swap = 0;
    let linger = 0;

    function render(content) {
      drawn = JSON.stringify(content);
      slot.textContent = "";
      const line = document.createElement("div");
      if (content.name) {
        const name = document.createElement("strong");
        name.textContent = content.name;
        line.appendChild(name);
        line.appendChild(document.createTextNode(` · ${content.text}`));
        slot.appendChild(line);
      }
      if (content.note) {
        const note = document.createElement("div");
        note.className = "map-readout-note";
        note.textContent = content.note;
        slot.appendChild(note);
      }
    }

    function show(key, content) {
      clearTimeout(linger);
      if (key === current) {
        if (JSON.stringify(content) !== drawn) {
          clearTimeout(swap);
          slot.classList.remove("is-changing");
          render(content);
        }
        return;
      }
      current = key;
      clearTimeout(swap);
      slot.classList.add("is-changing");
      swap = setTimeout(() => {
        render(content);
        slot.classList.remove("is-changing");
      }, READOUT_FADE_MS);
    }

    function clear() {
      clearTimeout(linger);
      linger = setTimeout(
        () => show(null, { note: hintText }),
        READOUT_LINGER_MS
      );
    }

    function setHint(text) {
      hintText = text;
      if (current === null) render({ note: hintText });
    }

    render({ note: hintText });
    return { show, clear, setHint };
  }

  function readoutBeside(el, hint) {
    const slot = document.createElement("div");
    slot.className = "map-readout";
    el.parentElement.classList.add("has-readout");
    el.parentElement.insertBefore(slot, el);
    return makeReadout(slot, hint);
  }

  const MAP_HINTS = {
    attention:
      "Attention is a state's share of election coverage now, divided by " +
      "its usual share since noon Nov 1. 1.00 is normal; 1.32 is 32% above " +
      "normal.",
    volume: "Stories naming each state in this window.",
    tone:
      "Mean tone of news stories naming each state, from -2 to +2. Opinion " +
      "is left out.",
  };

  function attentionWords(value) {
    const percent = Math.round(Math.abs(value - 1) * 100);
    if (percent < 1) return "normal";
    return `${percent}% ${value > 1 ? "above" : "below"} normal`;
  }

  function storyCount(count, label = "") {
    const noun = count === 1 ? "story" : "stories";
    return `${count} ${label}${noun}`;
  }

  function scaleFor(metric) {
    const d3 = window.d3;
    if (metric === "volume") {
      return (peak) => ({
        color: d3
          .scaleSequential(d3.interpolateViridis)
          .domain([0, Math.max(peak, 1)]),
        domain: [0, Math.max(peak, 1)],
      });
    }
    if (metric === "tone") {
      return () => {
        const domain = [-1, 0, 1];
        return {
          color: d3
            .scaleDiverging((t) => d3.interpolatePuOr(1 - t))
            .domain(domain),
          domain,
        };
      };
    }
    return () => {
      const domain = [0.25, 1, 4];
      return {
        color: d3
          .scaleDivergingLog((t) => d3.interpolatePuOr(1 - t))
          .domain(domain),
        domain,
      };
    };
  }

  function start() {
    const container = document.getElementById("us-map");
    if (!container) return;
    let metric = "attention";
    let map = null;
    let running = null;
    let again = false;
    let shownQs;
    const readoutSlot = document.getElementById("map-readout");
    const readout = makeReadout(readoutSlot, MAP_HINTS[metric]);
    const legend = document.createElement("div");
    legend.className = "map-legend";
    const legendSlot = document.getElementById("map-legend");
    (legendSlot || container).appendChild(legend);

    function navigate(usps) {
      const query = Monitor.params().toString();
      window.location.href = `/state/${usps}?${query}`;
    }

    async function refreshOnce() {
      if (!map) {
        const topology = await (
          await fetch("/static/topo/states-albers-10m.json")
        ).json();
        map = drawMap(container, { topology, onClick: navigate });
      }
      const qs = document.body.dataset.qs;
      const shownMetric = metric;
      const params = Monitor.params();
      params.set("metric", shownMetric);
      const data = await (await fetch(`/api/data/map?${params}`)).json();
      /* The choice changed during the request, so this answer is stale. */
      if (qs !== document.body.dataset.qs || shownMetric !== metric) {
        again = true;
        return;
      }
      paint(data);
      Monitor.mapData = data;
      shownQs = qs;
      Monitor.staleNote(container, false);
    }

    /* A map that kept an earlier filter's data says so. */
    function noteIfStale() {
      const qs = document.body.dataset.qs;
      if (shownQs !== undefined && shownQs !== qs) {
        Monitor.staleNote(container, true);
      }
    }

    /* A refresh asked for during another runs after it. */
    function refresh() {
      if (running) {
        again = true;
        return running;
      }
      running = (async () => {
        do {
          again = false;
          try {
            await refreshOnce();
          } catch (error) {
            console.warn("map refresh failed", error);
            noteIfStale();
          }
        } while (again);
        if (map) map.svg.classed("map-painted", true);
        running = null;
      })();
      return running;
    }

    function valueOf(row) {
      if (!row) return null;
      if (metric === "attention") return row.attention;
      if (metric === "volume") return row.n;
      return row.tone;
    }

    function stateWords(row) {
      const value = valueOf(row);
      const stories = storyCount(row ? row.n : 0);
      if (metric === "tone") {
        const toned = (row ? row.tone_n : 0) || 0;
        if (value === null || value === undefined) {
          return `Under 5 toned stories · ${stories}`;
        }
        const count = storyCount(toned, "toned ");
        const low = row.tone_low_n ? " · low n" : "";
        const shown = Number(value).toFixed(2);
        return `Mean tone ${shown} · ${count}${low}`;
      }
      if (metric === "volume" || value === null || value === undefined) {
        return stories;
      }
      const shown = Number(value).toFixed(2);
      return `Attention ${shown}, ${attentionWords(value)} · ${stories}`;
    }

    function paint(data) {
      const byState = new Map(data.states.map((row) => [row.usps, row]));
      const peak = Math.max(0, ...data.states.map((row) => row.n));
      const { color, domain } = scaleFor(metric)(peak);
      const empty = getComputedStyle(document.documentElement)
        .getPropertyValue("--surface-2")
        .trim();
      const focus = data.focus_states ? new Set(data.focus_states) : null;
      map.states
        .style("fill", (feature) => {
          const value = valueOf(byState.get(USPS_BY_FIPS[feature.id]));
          return value === null || value === undefined ? empty : color(value);
        })
        .classed(
          "map-dim",
          (feature) => focus && !focus.has(USPS_BY_FIPS[feature.id])
        )
        .on("pointerenter pointermove", (_event, feature) => {
          const usps = USPS_BY_FIPS[feature.id];
          const row = byState.get(usps);
          readout.show(usps, {
            name: feature.properties.name,
            text: stateWords(row),
            note:
              row && row.top_storyline
                ? `Top story: ${row.top_storyline}`
                : "",
          });
        })
        .on("pointerleave", readout.clear);
      const battleground = new Set(data.battleground);
      map.outlines
        .selectAll("path")
        .data(
          map.features.filter((feature) =>
            battleground.has(USPS_BY_FIPS[feature.id])
          ),
          (feature) => feature.id
        )
        .join("path")
        .attr("class", "map-outline")
        .attr("d", map.path);
      legend.innerHTML = legendFor(metric, color, domain);
    }

    const metrics = document.getElementById("map-metric");
    if (metrics) {
      metrics.addEventListener("click", (event) => {
        const button = event.target.closest("button");
        if (!button) return;
        metric = button.dataset.metric;
        readout.setHint(MAP_HINTS[metric]);
        metrics
          .querySelectorAll("button")
          .forEach((b) =>
            b.setAttribute("aria-pressed", String(b === button))
          );
        refresh();
      });
    }
    Monitor.bus.addEventListener("refresh", refresh);
    Monitor.bus.addEventListener("theme", refresh);
    Monitor.switchers.push(() =>
      Monitor.dimWhile(container, refresh(), noteIfStale)
    );
    refresh();
  }

  /* The smaller maps on drill-down pages share a volume scale and tooltip. */
  function surfaceColor() {
    return getComputedStyle(document.documentElement)
      .getPropertyValue("--surface-2")
      .trim();
  }

  async function fetchTable(chart, el) {
    const params = Monitor.params();
    new URLSearchParams(el.dataset.params || "").forEach((value, key) =>
      params.set(key, value)
    );
    const response = await fetch(`/api/data/${chart}?${params}`);
    if (!response.ok) throw new Error(`${chart}: ${response.status}`);
    return (await response.json()).table || [];
  }

  function volumeFill(rows, key) {
    const counts = new Map(rows.map((row) => [row[key], row.n]));
    const peak = Math.max(1, ...rows.map((row) => row.n));
    const color = window.d3
      .scaleSequential(window.d3.interpolateViridis)
      .domain([0, peak]);
    return { counts, peak, color };
  }

  function bindVolumeReadout(selection, readout, keyOf, nameOf, countOf) {
    selection
      .on("pointerenter pointermove", (_event, feature) => {
        const count = countOf(feature);
        readout.show(keyOf(feature), {
          name: nameOf(feature),
          text: storyCount(count),
        });
      })
      .on("pointerleave", readout.clear);
  }

  function setNote(el, text) {
    let note = el.parentElement.querySelector(":scope > .empty");
    if (text && !note) {
      note = document.createElement("div");
      note.className = "empty map-note";
      el.parentElement.appendChild(note);
    } else if (!text && note) {
      note.remove();
      return;
    }
    if (note) note.textContent = text;
  }

  function watchMap(refresh) {
    Monitor.bus.addEventListener("refresh", refresh);
    Monitor.bus.addEventListener("theme", refresh);
    refresh();
  }

  function startStateMap(el) {
    let map = null;
    const readout = readoutBeside(el, "Stories in this storyline, by state.");
    async function refresh() {
      try {
        if (!map) {
          const topology = await (
            await fetch("/static/topo/states-albers-10m.json")
          ).json();
          map = drawMap(el, {
            topology,
            onClick: (usps) => {
              window.location.href = `/state/${usps}?${Monitor.params()}`;
            },
          });
        }
        const rows = await fetchTable("story_map", el);
        const { counts, color } = volumeFill(rows, "usps");
        const empty = surfaceColor();
        map.states.style("fill", (feature) => {
          const n = counts.get(USPS_BY_FIPS[feature.id]);
          return n ? color(n) : empty;
        });
        map.svg.classed("map-painted", true);
        bindVolumeReadout(
          map.states,
          readout,
          (feature) => feature.id,
          (feature) => feature.properties.name,
          (feature) => counts.get(USPS_BY_FIPS[feature.id]) || 0
        );
      } catch (error) {
        console.warn("state map refresh failed", error);
      }
    }
    watchMap(refresh);
  }

  function startCountyMap(el) {
    const d3 = window.d3;
    const fips = el.dataset.fips;
    let map = null;
    const readout = readoutBeside(el, "Stories naming each county.");
    async function draw() {
      const topology = await (
        await fetch("/static/topo/counties-albers-10m.json")
      ).json();
      const features = window.topojson
        .feature(topology, topology.objects.counties)
        .features.filter((feature) => feature.id.slice(0, 2) === fips);
      if (!features.length) return { features };
      const path = d3.geoPath();
      const collection = { type: "FeatureCollection", features };
      const [[x0, y0], [x1, y1]] = path.bounds(collection);
      const pad = Math.max(x1 - x0, y1 - y0) * 0.03;
      const svg = d3
        .select(el)
        .append("svg")
        .attr("class", "map-svg")
        .attr(
          "viewBox",
          `${x0 - pad} ${y0 - pad} ${x1 - x0 + 2 * pad} ${y1 - y0 + 2 * pad}`
        )
        .attr("preserveAspectRatio", "xMidYMid meet")
        .attr("role", "img")
        .attr("aria-label", `Map of counties in ${el.dataset.usps}`);
      const counties = svg
        .append("g")
        .selectAll("path")
        .data(features, (feature) => feature.id)
        .join("path")
        .attr("class", "map-county")
        .attr("d", path)
        .attr("data-fips", (feature) => feature.id)
        .style("fill", surfaceColor())
        .on("click", (_event, feature) => {
          Monitor.openEvidence(`kind=county&key=${feature.id}`);
        });
      return { features, counties, svg };
    }
    async function refresh() {
      try {
        if (!map) map = await draw();
        if (!map.features.length) {
          setNote(el, `No county map for ${el.dataset.usps}.`);
          return;
        }
        const rows = await fetchTable("state_counties", el);
        const { counts, color } = volumeFill(rows, "fips");
        const empty = surfaceColor();
        map.counties.style("fill", (feature) => {
          const n = counts.get(feature.id);
          return n ? color(n) : empty;
        });
        map.svg.classed("map-painted", true);
        bindVolumeReadout(
          map.counties,
          readout,
          (feature) => feature.id,
          (feature) => feature.properties.name,
          (feature) => counts.get(feature.id) || 0
        );
        setNote(
          el,
          rows.length ? "" : "No stories name a county here for these filters."
        );
      } catch (error) {
        console.warn("county map refresh failed", error);
      }
    }
    watchMap(refresh);
  }

  function startAll() {
    start();
    document.querySelectorAll("[data-state-map]").forEach(startStateMap);
    document.querySelectorAll("[data-county-map]").forEach(startCountyMap);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startAll);
  } else {
    startAll();
  }
})();
