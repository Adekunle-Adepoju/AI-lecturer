/**
 * rovea-visual-renderer.js
 *
 * Parses AI-generated Markdown containing ```json_chart, ```mermaid, and
 * ```svg fenced blocks and renders them inline, dark-mode themed.
 *
 * Contract (must match core/prompt.py VISUAL_BLOCK_PROMPT exactly):
 *   ```json_chart  -> { chartType: "line"|"scatter"|"bar", title, xAxisLabel,
 *                        yAxisLabel, series: [{ label, color?, data:[{x,y}] }] }
 *   ```mermaid     -> raw Mermaid flowchart syntax
 *   ```svg         -> a single <svg ...>...</svg> element with a viewBox
 *
 * Dependencies (load once, before this file):
 *   <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
 *   <script type="module">
 *     import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs";
 *     window.mermaid = mermaid;
 *     mermaid.initialize({ startOnLoad: false, theme: "dark" });
 *   </script>
 *   window.markdownit (or any converter) for plain-text segments — optional,
 *   falls back to a minimal escaper if absent.
 */

(function (global) {
  "use strict";

  // ── Theme tokens — single source of truth, mirrors the backend contract ──
  const THEME = {
    bg: "#0f172a",
    bgAlt: "#1e293b",
    text: "#f8fafc",
    grid: "#334155",
    oil: "#38bdf8",       // primary
    pressure: "#f43f5e",  // secondary / danger
    gas: "#10b981",       // tertiary / success
  };

  const COLOR_ORDER = [THEME.oil, THEME.pressure, THEME.gas, "#a78bfa", "#facc15", "#fb923c"];

  function resolveColor(name, index) {
    if (name && THEME[name]) return THEME[name];
    return COLOR_ORDER[index % COLOR_ORDER.length];
  }

  // ── Fence extraction ──────────────────────────────────────────────────
  // Matches ```json_chart|mermaid|svg ... ``` blocks, non-greedy, multiline.
  const FENCE_RE = /```(json_chart|mermaid|svg)[ \t]*\r?\n([\s\S]*?)```/g;

  /**
   * Splits raw markdown into an ordered list of segments:
   *   { type: "text", content } | { type: "json_chart"|"mermaid"|"svg", raw }
   */
  function splitIntoSegments(markdown) {
    const segments = [];
    let lastIndex = 0;
    let match;

    FENCE_RE.lastIndex = 0;
    while ((match = FENCE_RE.exec(markdown)) !== null) {
      const [full, blockType, rawContent] = match;
      if (match.index > lastIndex) {
        segments.push({ type: "text", content: markdown.slice(lastIndex, match.index) });
      }
      segments.push({ type: blockType, raw: rawContent.trim() });
      lastIndex = match.index + full.length;
    }
    if (lastIndex < markdown.length) {
      segments.push({ type: "text", content: markdown.slice(lastIndex) });
    }
    return segments;
  }

  // ── Text rendering ────────────────────────────────────────────────────
  function renderText(content) {
    const trimmed = content.trim();
    if (!trimmed) return null;
    const el = document.createElement("div");
    el.className = "rovea-text-block";
    if (global.markdownit) {
      el.innerHTML = global.markdownit().render(trimmed);
    } else if (global.markdown && global.markdown.markdown) {
      el.innerHTML = global.markdown.markdown.toHTML(trimmed);
    } else {
      // Minimal safe fallback — escape then convert single newlines to <br>,
      // blank lines to paragraph breaks. Not a full Markdown parser.
      const escaped = trimmed
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
      el.innerHTML = escaped
        .split(/\n\s*\n/)
        .map((p) => `<p>${p.replace(/\n/g, "<br>")}</p>`)
        .join("");
    }
    return el;
  }

  // ── json_chart rendering (Chart.js) ──────────────────────────────────
  function renderJsonChart(raw) {
    const wrapper = document.createElement("div");
    wrapper.className = "rovea-visual-block rovea-chart-block";

    let spec;
    try {
      spec = JSON.parse(raw);
    } catch (e) {
      return renderFallback("Chart could not be displayed (invalid data).");
    }

    const {
      chartType = "line",
      title = "",
      xAxisLabel = "",
      yAxisLabel = "",
      series = [],
    } = spec;

    if (!Array.isArray(series) || series.length === 0) {
      return renderFallback("Chart had no data to display.");
    }

    const canvas = document.createElement("canvas");
    wrapper.appendChild(canvas);
    wrapper.style.background = THEME.bgAlt;
    wrapper.style.borderRadius = "12px";
    wrapper.style.padding = "16px";
    wrapper.style.margin = "12px 0";

    if (!global.Chart) {
      wrapper.appendChild(renderFallback("Chart library not loaded."));
      return wrapper;
    }

    const datasets = series.slice(0, 4).map((s, i) => {
      const color = resolveColor(s.color, i);
      const points = (s.data || []).map((p) => ({ x: p.x, y: p.y }));
      const base = {
        label: s.label || `Series ${i + 1}`,
        data: points,
        borderColor: color,
        backgroundColor: chartType === "bar" ? color : `${color}33`,
      };
      if (chartType === "line") {
        return { ...base, tension: 0.25, pointRadius: 3, borderWidth: 2, fill: false };
      }
      if (chartType === "scatter") {
        return { ...base, showLine: false, pointRadius: 4 };
      }
      return base; // bar
    });

    // eslint-disable-next-line no-new
    new global.Chart(canvas.getContext("2d"), {
      type: chartType === "scatter" ? "scatter" : chartType === "bar" ? "bar" : "line",
      data: { datasets },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        aspectRatio: 1.7,
        plugins: {
          title: { display: !!title, text: title, color: THEME.text, font: { size: 14 } },
          legend: {
            display: datasets.length > 1,
            labels: { color: THEME.text },
          },
        },
        scales: {
          x: {
            type: "linear",
            position: "bottom",
            title: { display: !!xAxisLabel, text: xAxisLabel, color: THEME.text },
            ticks: { color: THEME.text },
            grid: { color: THEME.grid },
          },
          y: {
            title: { display: !!yAxisLabel, text: yAxisLabel, color: THEME.text },
            ticks: { color: THEME.text },
            grid: { color: THEME.grid },
          },
        },
      },
    });

    return wrapper;
  }

  // ── mermaid rendering ────────────────────────────────────────────────
  let mermaidCounter = 0;
  function renderMermaid(raw) {
    const wrapper = document.createElement("div");
    wrapper.className = "rovea-visual-block rovea-mermaid-block";
    wrapper.style.background = THEME.bgAlt;
    wrapper.style.borderRadius = "12px";
    wrapper.style.padding = "16px";
    wrapper.style.margin = "12px 0";
    wrapper.style.overflowX = "auto";

    if (!global.mermaid) {
      wrapper.appendChild(renderFallback("Diagram library not loaded."));
      return wrapper;
    }

    const id = `rovea-mermaid-${Date.now()}-${mermaidCounter++}`;
    const target = document.createElement("div");
    target.className = "mermaid";
    wrapper.appendChild(target);

    // mermaid.render is async — resolve into the wrapper once ready.
    global.mermaid
      .render(id, raw)
      .then(({ svg }) => {
        target.innerHTML = svg;
        const svgEl = target.querySelector("svg");
        if (svgEl) {
          svgEl.removeAttribute("width");
          svgEl.removeAttribute("height");
          svgEl.style.maxWidth = "100%";
          svgEl.style.height = "auto";
        }
      })
      .catch(() => {
        target.replaceWith(renderFallback("Diagram could not be rendered."));
      });

    return wrapper;
  }

  // ── svg rendering (sanitized) ────────────────────────────────────────
  const SVG_ALLOWED_TAGS = new Set([
    "svg", "rect", "circle", "ellipse", "line", "path", "polygon",
    "polyline", "text", "tspan", "g", "defs", "title",
  ]);
  const SVG_FORBIDDEN_ATTR_RE = /^on/i; // strip onclick, onload, etc.

  function sanitizeSvgNode(node) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      const tag = node.tagName.toLowerCase();
      if (!SVG_ALLOWED_TAGS.has(tag)) {
        node.remove();
        return;
      }
      [...node.attributes].forEach((attr) => {
        const name = attr.name.toLowerCase();
        if (
          SVG_FORBIDDEN_ATTR_RE.test(name) ||
          name === "href" ||
          name === "xlink:href" ||
          name === "style" && /url\(/i.test(attr.value)
        ) {
          node.removeAttribute(attr.name);
        }
      });
      [...node.childNodes].forEach(sanitizeSvgNode);
    }
  }

  function renderSvg(raw) {
    const wrapper = document.createElement("div");
    wrapper.className = "rovea-visual-block rovea-svg-block";
    wrapper.style.background = THEME.bgAlt;
    wrapper.style.borderRadius = "12px";
    wrapper.style.padding = "16px";
    wrapper.style.margin = "12px 0";
    wrapper.style.display = "flex";
    wrapper.style.justifyContent = "center";

    let doc;
    try {
      doc = new DOMParser().parseFromString(raw, "image/svg+xml");
      if (doc.querySelector("parsererror")) throw new Error("parse error");
    } catch (e) {
      wrapper.appendChild(renderFallback("Diagram could not be displayed."));
      return wrapper;
    }

    const svgEl = doc.documentElement;
    if (svgEl.tagName.toLowerCase() !== "svg" || !svgEl.getAttribute("viewBox")) {
      wrapper.appendChild(renderFallback("Diagram was missing required sizing info."));
      return wrapper;
    }

    sanitizeSvgNode(svgEl);
    svgEl.removeAttribute("width");
    svgEl.removeAttribute("height");
    svgEl.style.maxWidth = "100%";
    svgEl.style.height = "auto";

    wrapper.appendChild(document.importNode(svgEl, true));
    return wrapper;
  }

  // ── shared fallback ──────────────────────────────────────────────────
  function renderFallback(message) {
    const el = document.createElement("div");
    el.className = "rovea-visual-fallback";
    el.style.color = THEME.text;
    el.style.opacity = "0.7";
    el.style.fontStyle = "italic";
    el.style.padding = "8px 0";
    el.textContent = message;
    return el;
  }

  // ── public API ───────────────────────────────────────────────────────
  const RoveaVisualRenderer = {
    /**
     * Renders `markdown` (raw AI output, may contain fenced visual
     * blocks) into `container` (a DOM element). Clears the container
     * first. Safe to call repeatedly (e.g. as SSE chunks accumulate —
     * just re-render the full accumulated text each time, or call
     * appendSegment for true incremental streaming, see below).
     */
    render(markdown, container) {
      container.innerHTML = "";
      const segments = splitIntoSegments(markdown);
      segments.forEach((seg) => {
        let node = null;
        if (seg.type === "text") node = renderText(seg.content);
        else if (seg.type === "json_chart") node = renderJsonChart(seg.raw);
        else if (seg.type === "mermaid") node = renderMermaid(seg.raw);
        else if (seg.type === "svg") node = renderSvg(seg.raw);
        if (node) container.appendChild(node);
      });
    },

    // Exposed for testing / custom pipelines.
    _internal: { splitIntoSegments, renderText, renderJsonChart, renderMermaid, renderSvg, THEME },
  };

  global.RoveaVisualRenderer = RoveaVisualRenderer;
})(window);