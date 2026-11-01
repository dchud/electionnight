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
      .on("click", (_event, feature) => onClick(USPS_BY_FIPS[feature.id]));
    const outlines = svg.append("g");
    return { svg, states, outlines, path, features };
  }

  function legendFor(metric, color, domain) {
    const stops = d3Stops(color, domain);
    const low = domain[0];
    const high = domain[domain.length - 1];
    const words = {
      attention: ["below normal", "above normal"],
      volume: [String(low), String(Math.round(high))],
      tone: ["negative", "positive"],
    }[metric];
    return (
      `<span>${words[0]}</span>` +
      `<span class="map-legend-bar" style="background:linear-gradient(90deg,${stops})"></span>` +
      `<span>${words[1]}</span>`
    );
  }

  function d3Stops(color, domain) {
    const low = domain[0];
    const high = domain[domain.length - 1];
    return Array.from({ length: 8 }, (_, index) =>
      color(low + ((high - low) * index) / 7)
    ).join(",");
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
    let busy = false;
    const tip = document.getElementById("tip");
    const legend = document.createElement("div");
    legend.className = "map-legend";
    const legendSlot = document.getElementById("map-legend");
    (legendSlot || container).appendChild(legend);

    function navigate(usps) {
      const query = Monitor.params().toString();
      window.location.href = `/state/${usps}?${query}`;
    }

    async function refresh() {
      if (busy) return;
      busy = true;
      try {
        if (!map) {
          const topology = await (
            await fetch("/static/topo/states-albers-10m.json")
          ).json();
          map = drawMap(container, { topology, onClick: navigate });
        }
        const params = Monitor.params();
        params.set("metric", metric);
        const data = await (await fetch(`/api/data/map?${params}`)).json();
        paint(data);
      } catch (error) {
        console.warn("map refresh failed", error);
      } finally {
        busy = false;
      }
    }

    function valueOf(row) {
      if (!row) return null;
      if (metric === "attention") return row.attention;
      if (metric === "volume") return row.n;
      return row.tone;
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
        .attr("fill", (feature) => {
          const value = valueOf(byState.get(USPS_BY_FIPS[feature.id]));
          return value === null || value === undefined ? empty : color(value);
        })
        .classed(
          "map-dim",
          (feature) => focus && !focus.has(USPS_BY_FIPS[feature.id])
        )
        .on("pointermove", (event, feature) => {
          const usps = USPS_BY_FIPS[feature.id];
          const row = byState.get(usps);
          tip.hidden = false;
          tip.style.left = event.clientX + 14 + "px";
          tip.style.top = event.clientY + 14 + "px";
          const value = valueOf(row);
          const shown =
            value === null || value === undefined
              ? metric === "tone"
                ? "n under 5"
                : "no stories"
              : Number(value).toFixed(metric === "volume" ? 0 : 2);
          const words = {
            attention: "Attention",
            volume: "Stories",
            tone: "Mean tone",
          }[metric];
          tip.textContent = "";
          const title = document.createElement("strong");
          title.textContent = usps;
          tip.appendChild(title);
          tip.appendChild(
            document.createTextNode(
              ` · ${words} ${shown} · n=${row ? row.n : 0}`
            )
          );
          if (row && row.top_storyline) {
            const story = document.createElement("div");
            story.textContent = row.top_storyline;
            tip.appendChild(story);
          }
        })
        .on("pointerleave", () => {
          tip.hidden = true;
        });
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
    refresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
