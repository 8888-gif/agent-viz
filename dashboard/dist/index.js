/**
 * agent-viz — 多 Agent 协作可视化
 * Views: overview (stats+board+agents+activity), topology (SVG dep graph),
 *        flow (message-flow lanes from delegation transcripts).
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const {
    Card, CardHeader, CardTitle, CardContent, Badge, Button, Tabs, TabsList, TabsTrigger,
  } = SDK.components;

  const API = "/api/plugins/agent-viz";
  const REFRESH_MS = 10000;

  /* ── Shared helpers ────────────────────────────────────────── */
  const STATUS_COLOR = {
    triage: "#9ca3af", todo: "#3b82f6", scheduled: "#6366f1", ready: "#14b8a6",
    running: "#10b981", review: "#f59e0b", blocked: "#ef4444", done: "#22c55e", archived: "#71717a",
  };
  const STATUS_LABEL = {
    triage: "待分诊", todo: "待办", scheduled: "已排期", ready: "就绪",
    running: "运行中", review: "待评审", blocked: "阻塞", done: "完成", archived: "已归档",
  };
  const KIND_COLOR = {
    kickoff: "#a78bfa", user: "#f472b6", start: "#60a5fa", think: "#fbbf24",
    tool: "#34d399", result: "#38bdf8", complete: "#22c55e", info: "#9ca3af",
  };

  function usePoll(fn, deps) {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [lastUpdated, setLastUpdated] = useState(null);
    const load = useCallback(function () {
      Promise.resolve(fn())
        .then(function (d) { setData(d); setError(null); setLastUpdated(new Date()); })
        .catch(function (err) { setError(String(err && err.message ? err.message : err)); });
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, deps || []);
    useEffect(function () {
      load();
      const timer = setInterval(load, REFRESH_MS);
      return function () { clearInterval(timer); };
    }, [load]);
    return { data: data, error: error, lastUpdated: lastUpdated, reload: load };
  }

  function RefreshRow({ error, lastUpdated, reload }) {
    return React.createElement("div", { className: "agent-viz-refresh" },
      "每 10 秒自动刷新",
      lastUpdated ? " · " + lastUpdated.toLocaleTimeString() : "",
      React.createElement(Button, { size: "sm", variant: "outline", onClick: reload, className: "ml-2 h-6 px-2 text-[11px]" },
        "刷新"),
      error ? React.createElement("span", { className: "text-xs text-red-500 ml-3" }, "加载失败: " + error) : null,
    );
  }

  /* ── Topology view (adaptive layout) ──────────────────────── */
  const NODE_W = 230, NODE_H = 78, H_GAP = 60, V_GAP = 30, PAD = 30;

  function layoutGraph(nodes, edges) {
    const pos = {};
    // 1. Split: nodes involved in dependencies vs isolated
    const connected = new Set();
    edges.forEach(function (e) { connected.add(e.source); connected.add(e.target); });
    const depNodes = nodes.filter(function (n) { return connected.has(n.id); });
    const isolated = nodes.filter(function (n) { return !connected.has(n.id); });

    let depW = 0, depH = 0;

    // 2. Layered layout for dependency chain (longest-path layering)
    if (depNodes.length) {
      const indegree = {}, adj = {};
      depNodes.forEach(function (n) { indegree[n.id] = 0; adj[n.id] = []; });
      edges.forEach(function (e) {
        if (adj[e.source]) adj[e.source].push(e.target);
        indegree[e.target] = (indegree[e.target] || 0) + 1;
      });
      const layers = {}, queue = [];
      depNodes.forEach(function (n) { if ((indegree[n.id] || 0) === 0) { layers[n.id] = 0; queue.push(n.id); } });
      while (queue.length) {
        const id = queue.shift();
        (adj[id] || []).forEach(function (t) {
          layers[t] = Math.max(layers[t] || 0, (layers[id] || 0) + 1);
          queue.push(t);
        });
      }
      const byLayer = {};
      depNodes.forEach(function (n) {
        const l = layers[n.id] == null ? 0 : layers[n.id];
        (byLayer[l] = byLayer[l] || []).push(n);
      });
      const layerKeys = Object.keys(byLayer).map(Number).sort(function (a, b) { return a - b; });
      const maxLayer = layerKeys.length ? layerKeys[layerKeys.length - 1] : 0;
      depW = (maxLayer + 1) * (NODE_W + H_GAP) - H_GAP + PAD * 2;
      // Per-layer height
      const layerHeights = {};
      layerKeys.forEach(function (l) {
        const list = byLayer[l];
        layerHeights[l] = list.length * (NODE_H + V_GAP) - V_GAP;
      });
      depH = Math.max.apply(null, layerKeys.map(function (l) { return layerHeights[l]; }).concat([0]));
      layerKeys.forEach(function (l) {
        const xs = PAD + l * (NODE_W + H_GAP);
        const list = byLayer[l];
        const startY = (depH - layerHeights[l]) / 2;
        list.forEach(function (n, i) {
          pos[n.id] = { x: xs, y: startY + i * (NODE_H + V_GAP) };
        });
      });
    }

    // 3. Adaptive grid for isolated nodes — columns scale with count
    if (isolated.length) {
      const COLS = isolated.length <= 2 ? isolated.length
        : isolated.length <= 4 ? 2
        : isolated.length <= 6 ? 3
        : 4;
      const rows = Math.ceil(isolated.length / COLS);
      const COL_GAP = 16;
      const isoW = COLS * (NODE_W + COL_GAP) - COL_GAP + PAD * 2;
      const isoH = rows * (NODE_H + 20) - 20;
      const gridW = Math.max(isoW, depW);
      // Center grid horizontally under (or beside) dep area
      const gridX = (gridW - isoW) / 2 + PAD;
      const gridY = depH > 0 ? depH + 50 : 10;
      isolated.forEach(function (n, i) {
        const row = Math.floor(i / COLS), col = i % COLS;
        pos[n.id] = { x: gridX + col * (NODE_W + COL_GAP), y: gridY + row * (NODE_H + 20) };
      });
      return { pos: pos, width: gridW + 8, height: gridY + isoH + PAD };
    }

    return { pos: pos, width: Math.max(depW, 320), height: Math.max(depH, 200) + PAD };
  }

  function TopologyView({ data }) {
    const [selected, setSelected] = React.useState ? React.useState(null) : [null, function () {}];
    const [hovered, setHovered] = React.useState ? React.useState(null) : [null, function () {}];
    const [zoom, setZoom] = React.useState ? React.useState(1) : [1, function () {}];
    const [onlyActive, setOnlyActive] = React.useState ? React.useState(true) : [true, function () {}];

    // Empty state: distinguish "no tasks at all" vs "nothing running"
    if (!data.nodes || data.nodes.length === 0) {
      return React.createElement("div", { className: "agent-viz-empty" },
        "暂无任务 — 创建 kanban 任务并用 `hermes kanban link` 建立依赖关系后这里会显示拓扑图。");
    }
    let visibleNodes = data.nodes;
    if (onlyActive) {
      visibleNodes = data.nodes.filter(function (n) {
        if (n.kind === "agent") return !!n.alive;
        if (n.kind === "task") return n.status === "running";
        return true;
      });
      const baseIds = new Set(visibleNodes.map(function (n) { return n.id; }));
      const connectedDeleg = new Set();
      data.edges.forEach(function (e) {
        if (baseIds.has(e.source) && baseIds.has(e.target)) {
          connectedDeleg.add(e.source); connectedDeleg.add(e.target);
        }
      });
      visibleNodes = visibleNodes.filter(function (n) {
        if (n.kind === "deleg") return connectedDeleg.has(n.id);
        return true;
      });
    }
    if (visibleNodes.length === 0) {
      return React.createElement("div", { className: "agent-viz-empty" },
        onlyActive
          ? "没有运行中的任务或代理 — 派发子代理或开始 kanban 任务后，这里会实时显示。"
          : "暂无任务 — 创建 kanban 任务并用 `hermes kanban link` 建立依赖关系后这里会显示拓扑图。");
    }
    const visibleIds = new Set(visibleNodes.map(function (n) { return n.id; }));
    const visibleEdges = data.edges.filter(function (e) {
      return visibleIds.has(e.source) && visibleIds.has(e.target);
    });

    const { pos, width, height } = layoutGraph(visibleNodes, visibleEdges);
    const edges = visibleEdges.filter(function (e) { return pos[e.source] && pos[e.target]; });
    const nodeById = {};
    visibleNodes.forEach(function (n) { nodeById[n.id] = n; });

    // Legend
    const legend = [
      { s: "running", label: "运行中" }, { s: "ready", label: "就绪" }, { s: "todo", label: "待办" },
      { s: "done", label: "完成" }, { s: "blocked", label: "阻塞" },
    ];

    function nodeClass(n) {
      if (hovered && (hovered === n.id || edges.some(function (e) { return (e.source === hovered && e.target === n.id) || (e.target === hovered && e.source === n.id); }))) {
        return "agent-viz-topo-node dim-others";
      }
      return "agent-viz-topo-node";
    }

    function nodeColor(n) {
      if (n.kind === "agent") return n.status === "running" ? "#10b981" : "#60a5fa";
      if (n.kind === "deleg") return "#a78bfa";
      return STATUS_COLOR[n.status] || "#9ca3af";
    }

    function nodeAvatar(n) {
      if (n.kind === "agent") return "🤖";
      if (n.kind === "deleg") return "⚡";
      return (n.assignee || "?").slice(0, 2).toUpperCase();
    }

    function nodeSub(n) {
      if (n.kind === "agent") return n.label + (n.source ? " · " + n.source : "");
      if (n.kind === "deleg") return "delegate_task · " + (n.delegation_id || "").slice(0, 14);
      return (n.assignee || "未指派") + " · " + (STATUS_LABEL[n.status] || n.status);
    }

    const zoomIn = function () { setZoom(Math.min(2.5, +(zoom * 1.25).toFixed(2))); };
    const zoomOut = function () { setZoom(Math.max(0.3, +(zoom / 1.25).toFixed(2))); };
    const zoomFit = function () { setZoom(1); };

    const onWheel = function (e) {
      // Ctrl+wheel zooms; plain wheel scrolls the container
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        const delta = e.deltaY < 0 ? 1.15 : 1 / 1.15;
        setZoom(Math.min(2.5, Math.max(0.3, +(zoom * delta).toFixed(2))));
      }
    };

    return React.createElement("div", { className: "agent-viz-topo-wrap" },
      React.createElement("div", { className: "agent-viz-topo-legend" },
        legend.map(function (l) {
          return React.createElement("span", { key: l.s, className: "agent-viz-topo-legend-item" },
            React.createElement("i", { style: { background: STATUS_COLOR[l.s] } }),
            l.label);
        }),
        React.createElement("span", { className: "agent-viz-topo-hint" }, "滚轮缩放(Ctrl) · 拖拽滚动 · 点击节点查看详情"),
      ),
      React.createElement("div", { className: "agent-viz-topo-toolbar" },
        React.createElement("button", {
          onClick: function () { setOnlyActive(!onlyActive); },
          className: "agent-viz-topo-zoom-btn" + (onlyActive ? " active" : ""),
          title: "只显示运行中的代理",
        }, onlyActive ? "✓ 只看运行中" : "显示全部"),
        React.createElement("button", { onClick: zoomOut, title: "缩小", className: "agent-viz-topo-zoom-btn" }, "−"),
        React.createElement("span", { className: "agent-viz-topo-zoom-pct" }, Math.round(zoom * 100) + "%"),
        React.createElement("button", { onClick: zoomIn, title: "放大", className: "agent-viz-topo-zoom-btn" }, "+"),
        React.createElement("button", { onClick: zoomFit, title: "适应窗口", className: "agent-viz-topo-zoom-btn" }, "⤢ 适应"),
      ),
      React.createElement("div", { className: "agent-viz-topo", onWheel: onWheel },
        React.createElement("div", {
          className: "agent-viz-topo-zoom-stage",
          style: { transform: "scale(" + zoom + ")", transformOrigin: "0 0", width: width, height: height },
        },
        React.createElement("svg", {
          width: width, height: height,
          viewBox: "0 0 " + width + " " + height,
          style: { display: "block" },
        },
          React.createElement("defs", null,
            React.createElement("marker", {
              id: "agent-viz-arrow", markerWidth: 9, markerHeight: 9, refX: 8, refY: 4.5, orient: "auto",
            },
              React.createElement("path", { d: "M0,0 L9,4.5 L0,9 Z", fill: "#94a3b8" }),
            ),
            React.createElement("filter", { id: "agent-viz-node-glow", x: "-30%", y: "-30%", width: "160%", height: "160%" },
              React.createElement("feGaussianBlur", { stdDeviation: 6, result: "blur" }),
              React.createElement("feMerge", null,
                React.createElement("feMergeNode", { in: "blur" }),
                React.createElement("feMergeNode", { in: "SourceGraphic" }),
              ),
            ),
          ),
          // Dependency edges
          edges.map(function (e, i) {
            const s = pos[e.source], t = pos[e.target];
            const sx = s.x + NODE_W, sy = s.y + NODE_H / 2;
            const tx = t.x, ty = t.y + NODE_H / 2;
            const mx = (sx + tx) / 2;
            const isActive = !hovered || hovered === e.source || hovered === e.target;
            return React.createElement("path", {
              key: "e" + i,
              d: "M" + sx + "," + sy + " C" + mx + "," + sy + " " + mx + "," + ty + " " + tx + "," + ty,
              fill: "none",
              stroke: isActive ? "#94a3b8" : "#334155",
              strokeWidth: isActive ? 2 : 1.2,
              strokeDasharray: "6,4",
              markerEnd: "url(#agent-viz-arrow)",
              className: isActive ? "agent-viz-topo-edge" : "",
            });
          }),
          // Nodes
          data.nodes.map(function (n) {
            const p = pos[n.id];
            if (!p) return null;
            const color = nodeColor(n);
            const isSel = selected === n.id;
            const dim = hovered && hovered !== n.id && !edges.some(function (e) { return (e.source === hovered && e.target === n.id) || (e.target === hovered && e.source === n.id); });
            const running = n.status === "running";
            return React.createElement("g", {
              key: n.id,
              className: "agent-viz-topo-node" + (dim ? " dimmed" : "") + (running ? " running" : ""),
              transform: "translate(" + p.x + "," + p.y + ")",
              onMouseEnter: function () { setHovered(n.id); },
              onMouseLeave: function () { setHovered(null); },
              onClick: function () { setSelected(isSel ? null : n.id); },
              style: { cursor: "pointer" },
            },
              // glow for running nodes
              running ? React.createElement("rect", {
                x: -8, y: -8, width: NODE_W + 16, height: NODE_H + 16, rx: 14,
                fill: "none", stroke: color, strokeWidth: 2, filter: "url(#agent-viz-node-glow)",
                className: "agent-viz-topo-pulse",
              }) : null,
              // card background
              React.createElement("rect", {
                x: 0, y: 0, width: NODE_W, height: NODE_H, rx: 10,
                fill: isSel ? "color-mix(in srgb, " + color + " 22%, #0b1220)" : "color-mix(in srgb, " + color + " 10%, #0d1526)",
                stroke: isSel ? color : "color-mix(in srgb, " + color + " 55%, transparent)",
                strokeWidth: isSel ? 2.5 : 1.5,
              }),
              // left accent bar
              React.createElement("rect", { x: 0, y: 0, width: 6, height: NODE_H, rx: 3, fill: color }),
              // status dot + assignee avatar
              React.createElement("circle", { cx: 22, cy: 24, r: 9, fill: color }),
              React.createElement("text", { x: 22, y: 28, fontSize: 10, fontWeight: 700, textAnchor: "middle", fill: "#0b1220" },
                nodeAvatar(n)),
              // title
              React.createElement("text", { x: 40, y: 22, fontSize: 12.5, fontWeight: 600, fill: "#e2e8f0" },
                React.createElement("tspan", null, (n.label || n.id).slice(0, 22))),
              // assignee + status
              React.createElement("text", { x: 40, y: 40, fontSize: 10.5, fill: "#94a3b8" },
                nodeSub(n)),
              // priority stars if any
              (n.priority && n.priority > 0) ? React.createElement("text", { x: NODE_W - 12, y: 22, fontSize: 11, textAnchor: "end", fill: "#fbbf24" },
                "★".repeat(Math.min(n.priority, 3))) : null,
              // body preview (2 lines)
              (n.body && n.body.trim()) ? React.createElement("text", { x: 40, y: 58, fontSize: 9.5, fill: "#64748b" },
                React.createElement("tspan", null, n.body.slice(0, 30) + (n.body.length > 30 ? "…" : ""))) : null,
              // checkmark for done
              n.status === "done" ? React.createElement("text", { x: NODE_W - 12, y: NODE_H - 10, fontSize: 13, textAnchor: "end", fill: "#22c55e" }, "✓") : null,
            );
          }),
        ),
        ),
      ),
      // Detail panel
      selected && nodeById[selected] ? (function () {
        const n = nodeById[selected];
        return React.createElement("div", { className: "agent-viz-topo-detail" },
          React.createElement("div", { className: "agent-viz-topo-detail-head" },
            React.createElement("span", { className: "agent-viz-topo-detail-dot", style: { background: STATUS_COLOR[n.status] } }),
            React.createElement("b", null, n.label || n.id),
            React.createElement("span", { className: "agent-viz-topo-detail-status" }, STATUS_LABEL[n.status] || n.status),
            React.createElement("button", { onClick: function () { setSelected(null); }, className: "agent-viz-topo-detail-close" }, "×"),
          ),
          React.createElement("div", { className: "agent-viz-topo-detail-row" },
            React.createElement("span", null, "ID"), React.createElement("code", null, n.id)),
          n.kind === "agent" ?
            React.createElement("div", { className: "agent-viz-topo-detail-row" },
              React.createElement("span", null, "来源"), React.createElement("code", null, n.source || "agent")) :
            React.createElement("div", { className: "agent-viz-topo-detail-row" },
              React.createElement("span", null, "负责人"), React.createElement("code", null, n.assignee || "未指派")),
          n.session_key ? React.createElement("div", { className: "agent-viz-topo-detail-body" },
            React.createElement("span", null, "会话"), React.createElement("p", null, n.session_key)) : null,
          n.body ? React.createElement("div", { className: "agent-viz-topo-detail-body" },
            React.createElement("span", null, "任务说明"), React.createElement("p", null, n.body)) : null,
        );
      })() : null,
    );
  }

  /* ── Message-flow view (lane timeline, per-agent sections) ── */
  function FlowView({ data }) {
    const [collapsed, setCollapsed] = React.useState ? React.useState({}) : [{}, function () {}];

    if (!data || !data.lanes || data.lanes.length === 0) {
      return React.createElement("div", { className: "agent-viz-empty" },
        "暂无消息流 — 运行 delegate_task 多 agent 协作后这里会实时展示每个子代理的动作序列。");
    }
    return React.createElement("div", { className: "agent-viz-flow" },
      React.createElement("div", { className: "agent-viz-flow-summary" },
        "共 " + data.lanes.length + " 个子代理 · 点击标题栏折叠/展开"),
      data.lanes.map(function (lane, laneIdx) {
        const isCollapsed = !!collapsed[lane.id];
        const color = ["#10b981", "#60a5fa", "#a78bfa", "#f59e0b", "#f472b6"][laneIdx % 5];
        const evCount = lane.events.length;
        return React.createElement("div", { key: lane.id, className: "agent-viz-lane" },
          React.createElement("div", {
            className: "agent-viz-lane-header",
            style: { borderLeft: "4px solid " + color },
            onClick: function () {
              const c = Object.assign({}, collapsed);
              c[lane.id] = !isCollapsed;
              setCollapsed(c);
            },
          },
            React.createElement("span", { className: "agent-viz-lane-idx" }, "#" + (laneIdx + 1)),
            React.createElement(Badge, { className: "agent-viz-lane-badge" }, lane.agent),
            React.createElement("span", { className: "agent-viz-lane-status" }, lane.status),
            React.createElement("span", { className: "agent-viz-lane-count" }, evCount + " 条消息"),
            React.createElement("span", { className: "agent-viz-lane-goal" }, lane.goal),
            React.createElement("span", { className: "agent-viz-lane-toggle" }, isCollapsed ? "▸" : "▾"),
          ),
          isCollapsed ? null : React.createElement("div", { className: "agent-viz-lane-body" },
            lane.events.map(function (ev, i) {
              const c = KIND_COLOR[ev.kind] || "#9ca3af";
              const icon = ev.kind === "tool" ? "⚙" : ev.kind === "result" ? "✓" : ev.kind === "think" ? "💭" : ev.kind === "user" ? "👤" : ev.kind === "complete" ? "🏁" : "·";
              const dur = ev.duration_s != null ? " (" + ev.duration_s + "s)" : "";
              return React.createElement("div", { key: i, className: "agent-viz-ev", style: { borderLeftColor: c } },
                React.createElement("span", { className: "agent-viz-ev-time" }, ev.time),
                React.createElement("span", { className: "agent-viz-ev-icon", style: { color: c } }, icon),
                React.createElement("span", { className: "agent-viz-ev-kind", style: { color: c } },
                  ev.kind + (ev.tool ? ":" + ev.tool : "") + dur),
                React.createElement("span", { className: "agent-viz-ev-text" },
                  (ev.text || "").slice(0, 120)),
              );
            }),
          ),
        );
      }),
    );
  }

  /* ── Scraper progress view (raw-material project) ─────────── */
  const SCRAPER_STATE_COLOR = {
    "已完成": "#22c55e", "处理中": "#10b981", "已有数据": "#14b8a6",
    "已爬取·待提取": "#f59e0b", "待处理PDF": "#f59e0b",
    "有记录·待处理": "#94a3b8", "待处理": "#64748b", "D级": "#ef4444",
  };
  function progressStateColor(st) {
    for (var k in SCRAPER_STATE_COLOR) {
      if (st.indexOf(k) >= 0) return SCRAPER_STATE_COLOR[k];
    }
    return "#64748b";
  }

  function ProgressView({ data }) {
    const [filter, setFilter] = React.useState ? React.useState("全部") : ["全部", function () {}];
    const [importing, setImporting] = React.useState ? React.useState(false) : [false, function () {}];
    const [importText, setImportText] = React.useState ? React.useState("") : ["", function () {}];
    const [parsedRecords, setParsedRecords] = React.useState ? React.useState(null) : [null, function () {}];
    const [importMsg, setImportMsg] = React.useState ? React.useState("") : ["", function () {}];
    const [configOpen, setConfigOpen] = React.useState ? React.useState(false) : [false, function () {}];
    const [pathInput, setPathInput] = React.useState ? React.useState("") : ["", function () {}];
    const [configMsg, setConfigMsg] = React.useState ? React.useState("") : ["", function () {}];
    const [currentRoot, setCurrentRoot] = React.useState ? React.useState("") : ["", function () {}];
    const [cfgLoaded, setCfgLoaded] = React.useState ? React.useState(false) : [false, function () {}];

    // Load scraper root config once
    if (!cfgLoaded) {
      setCfgLoaded(true);
      SDK.fetchJSON(API + "/scraper-config").then(function (c) {
        if (c && c.scraper_root) {
          setCurrentRoot(c.scraper_root);
          setPathInput(c.scraper_root);
        }
      }).catch(function () {});
    }
    if (!data || !data.companies || data.companies.length === 0) {
      return React.createElement("div", { className: "agent-viz-empty" },
        "暂无爬取进度 — 未找到 D:\\AI\\codex_skill 数据。");
    }
    const stats = data.stats || {};
    const companies = data.companies;
    const filters = ["全部"].concat(Object.keys(stats));
    const visible = filter === "全部" ? companies : companies.filter(function (c) { return c.status === filter; });
    const doneCount = (stats["已完成"] || 0) + (stats["已完成（39产品）"] || 0) + (stats["已有数据(106产品)"] || 0);
    const pct = Math.round((doneCount / data.total) * 100);

    return React.createElement("div", { className: "agent-viz-progress" },
      React.createElement("div", { className: "agent-viz-progress-settings" },
        React.createElement("button", {
          className: "agent-viz-progress-filter import",
          onClick: function () { setConfigOpen(!configOpen); setConfigMsg(""); },
        }, configOpen ? "✕ 关闭设置" : "⚙ 设置路径 " + (currentRoot || "")),
      ),
      configOpen ? React.createElement("div", { className: "agent-viz-import" },
        React.createElement("div", { className: "agent-viz-import-msg" },
          "当前数据目录：" + (currentRoot || "加载中…")),
        React.createElement("input", {
          className: "agent-viz-import-text",
          type: "text",
          placeholder: "输入新的数据文件夹路径，如 D:\\AI\\codex_skill",
          value: pathInput,
          onChange: function (e) { setPathInput(e.target.value); },
        }),
        React.createElement("div", { className: "agent-viz-import-actions" },
          React.createElement("button", {
            className: "agent-viz-progress-export",
            onClick: function () {
              if (!pathInput.trim()) { setConfigMsg("路径不能为空"); return; }
              setConfigMsg("保存中…");
              SDK.fetchJSON(API + "/scraper-config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ scraper_root: pathInput.trim() }),
              }).then(function (r) {
                if (r.ok) {
                  setConfigMsg("已保存：" + r.scraper_root);
                  setCurrentRoot(r.scraper_root);
                  setConfigOpen(false);
                } else {
                  setConfigMsg("保存失败：" + (r.error || ""));
                }
              }).catch(function (e) { setConfigMsg("请求失败：" + String(e)); });
            },
          }, "💾 保存路径"),
        ),
        configMsg ? React.createElement("div", { className: "agent-viz-import-msg" }, configMsg) : null,
      ) : null,
      React.createElement("div", { className: "agent-viz-progress-summary" },
        React.createElement("div", { className: "agent-viz-progress-total" },
          React.createElement("b", null, data.total),
          React.createElement("span", null, "家公司")),
        React.createElement("div", { className: "agent-viz-progress-bar" },
          Object.keys(stats).map(function (k) {
            return React.createElement("i", {
              key: k,
              className: "seg",
              style: { width: Math.round((stats[k] / data.total) * 100) + "%", background: progressStateColor(k) },
              title: k + " " + stats[k],
            });
          })),
        React.createElement("div", { className: "agent-viz-progress-pct" },
          "已完成 " + doneCount + " · " + pct + "%"),
        React.createElement("a", {
          className: "agent-viz-progress-export",
          href: API + "/scraper-progress/export",
          download: "",
          title: "导出全部公司为 CSV（快速）",
        }, "⬇ 导出 CSV"),
        React.createElement("a", {
          className: "agent-viz-progress-export llm",
          href: API + "/scraper-progress/llm-export",
          download: "",
          title: "用 LLM 深度分析每家公司产出后导出（较慢，含产品数/质量/风险）",
        }, "⚡ LLM 深度导出"),
      ),
      React.createElement("div", { className: "agent-viz-progress-filters" },
        filters.map(function (f) {
          return React.createElement("button", {
            key: f,
            className: "agent-viz-progress-filter" + (filter === f ? " active" : ""),
            onClick: function () { setFilter(f); },
          }, f + (f === "全部" ? "" : " " + (stats[f] || 0)));
        }),
        React.createElement("button", {
          className: "agent-viz-progress-filter import",
          onClick: function () { setImporting(!importing); setParsedRecords(null); setImportMsg(""); },
        }, importing ? "✕ 关闭导入" : "＋ 导入公司"),
      ),
      importing ? React.createElement("div", { className: "agent-viz-import" },
        React.createElement("textarea", {
          className: "agent-viz-import-text",
          placeholder: "粘贴任意格式的公司清单：\n1. 广州白云化工 - www.baiyunchem.com - 已完成\n表格/网页/PDF文本都可以，AI 会自动识别公司名、网址、状态",
          value: importText,
          onChange: function (e) { setImportText(e.target.value); setParsedRecords(null); },
          rows: 5,
        }),
        React.createElement("div", { className: "agent-viz-import-actions" },
          React.createElement("button", {
            className: "agent-viz-progress-export",
            onClick: function () {
              if (!importText.trim()) { setImportMsg("请先粘贴内容"); return; }
              setImportMsg("AI 解析中…");
              SDK.fetchJSON(API + "/scraper-progress/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: importText, mode: "preview" }) })
                .then(function (r) {
                  if (r.ok) { setParsedRecords(r.records); setImportMsg("解析出 " + r.records.length + " 家公司，请确认后导入"); }
                  else { setParsedRecords(null); setImportMsg("解析失败：" + (r.error || "")); }
                })
                .catch(function (e) { setParsedRecords(null); setImportMsg("请求失败：" + String(e)); });
            },
          }, "🔍 AI 解析"),
          parsedRecords ? React.createElement("button", {
            className: "agent-viz-progress-export llm",
            onClick: function () {
              SDK.fetchJSON(API + "/scraper-progress/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: importText, mode: "commit" }) })
                .then(function (r) {
                  if (r.ok) { setImportMsg("已导入 " + r.written + " 家新公司（重复已跳过）"); setParsedRecords(null); setImportText(""); setImporting(false); if (data.reload) data.reload(); }
                  else { setImportMsg("导入失败：" + (r.error || "")); }
                })
                .catch(function (e) { setImportMsg("请求失败：" + String(e)); });
            },
          }, "✔ 确认导入") : null,
        ),
        parsedRecords ? React.createElement("div", { className: "agent-viz-import-preview" },
          parsedRecords.map(function (rec, i) {
            return React.createElement("div", { key: i, className: "agent-viz-import-row" },
              React.createElement("span", { className: "agent-viz-import-name" }, rec.name),
              rec.url ? React.createElement("code", null, rec.url) : null,
              React.createElement("span", { className: "agent-viz-progress-state", style: { color: progressStateColor(rec.status) } }, rec.status),
              rec.note ? React.createElement("span", { className: "agent-viz-import-note" }, rec.note) : null,
            );
          }),
        ) : null,
        importMsg ? React.createElement("div", { className: "agent-viz-import-msg" }, importMsg) : null,
      ) : null,
      React.createElement("div", { className: "agent-viz-progress-list" },
        visible.map(function (c) {
          const color = progressStateColor(c.status);
          return React.createElement("div", { key: c.name, className: "agent-viz-progress-row" },
            React.createElement("span", { className: "agent-viz-progress-dot", style: { background: color } }),
            React.createElement("div", { className: "agent-viz-progress-name" },
              c.name,
              c.note ? React.createElement("div", { className: "agent-viz-progress-note" }, c.note) : null),
            React.createElement("span", { className: "agent-viz-progress-state", style: { color: color } }, c.status),
          );
        }),
      ),
    );
  }

  /* ── Main page with tabs ───────────────────────────────────── */
  function AgentVizPage() {
    const board = usePoll(function () { return SDK.fetchJSON(API + "/board"); }, []);
    const agents = usePoll(function () { return SDK.fetchJSON(API + "/agents"); }, []);
    const activity = usePoll(function () { return SDK.fetchJSON(API + "/activity"); }, []);
    const topo = usePoll(function () { return SDK.fetchJSON(API + "/topology"); }, []);
    const flow = usePoll(function () { return SDK.fetchJSON(API + "/flow"); }, []);
    const summary = usePoll(function () { return SDK.fetchJSON(API + "/agents-summary"); }, []);
    const progress = usePoll(function () { return SDK.fetchJSON(API + "/scraper-progress"); }, []);

    const b = board.data, a = agents.data, act = activity.data;
    const running = (a && a.count) || 0;
    const total = (b && b.total) || 0;
    const done = (b && b.done) || 0;

    function Stat({ label, value }) {
      return React.createElement("div", { className: "agent-viz-stat" },
        React.createElement("b", null, value),
        React.createElement("span", null, label));
    }

    function fmtDuration(sec) {
      if (sec == null) return "";
      if (sec < 60) return sec + "s";
      if (sec < 3600) return Math.floor(sec / 60) + "m" + (sec % 60 ? " " + (sec % 60) + "s" : "");
      return Math.floor(sec / 3600) + "h" + (Math.floor((sec % 3600) / 60) ? " " + Math.floor((sec % 3600) / 60) + "m" : "");
    }

    function BoardColumn({ column }) {
      const tasks = (b && b.tasks || [])
        .filter(function (t) { return t.status === column.status; })
        .sort(function (a, c) {
          // priority desc, then failed first, then created asc (oldest waiting first)
          const pa = (a.priority || 0), pb = (c.priority || 0);
          if (pa !== pb) return pb - pa;
          if (!!a.failed !== !!c.failed) return a.failed ? -1 : 1;
          return (a.created_at || 0) - (c.created_at || 0);
        });
      return React.createElement("div", { className: "agent-viz-column" },
        React.createElement("h4", null,
          React.createElement("span", null, column.label),
          React.createElement("span", null, column.count)),
        tasks.map(function (t) {
          const prio = t.priority || 0;
          const failed = !!t.failed;
          const wait = t.wait_s != null ? "⏳ 等待 " + fmtDuration(t.wait_s) : (t.runtime_s != null ? "⏱ " + fmtDuration(t.runtime_s) : "");
          const depInfo = t.deps ? "⛓ 依赖 " + t.deps : "";
          const sub = [depInfo, wait].filter(Boolean).join(" · ");
          const cardCls = "agent-viz-task" + (failed ? " failed" : "") + (prio >= 2 ? " high-prio" : "");
          return React.createElement("div", { key: t.id, className: cardCls, title: failed ? t.failure_error : undefined },
            React.createElement("div", { className: "title" },
              prio > 0 ? React.createElement("span", { className: "stars" }, "★".repeat(Math.min(prio, 3))) : null,
              t.title || ("Task " + t.id)),
            React.createElement("div", { className: "meta" },
              (t.assignee || "未指派") + " · " + (STATUS_LABEL[t.status] || t.status)),
            failed ? React.createElement("div", { className: "err" }, "✗ " + (t.failure_error || ("失败 x" + (t.failures || 1)))) : null,
            sub ? React.createElement("div", { className: "sub" }, sub) : null);
        }));
    }

    function AgentFleet() {
      const s = summary.data;
      const agents = (s && s.agents) || [];
      const live = (s && s.live_subagents) || [];
      if (!agents.length && !live.length) {
        return React.createElement(Card, null,
          React.createElement(CardHeader, null, React.createElement(CardTitle, { className: "text-sm" }, "全部 Agent 工作状态")),
          React.createElement(CardContent, null,
            React.createElement("div", { className: "agent-viz-empty" }, "暂无 Agent — 创建 kanban 任务后这里会显示每个 agent 的工作状态。")));
      }
      return React.createElement(Card, null,
        React.createElement(CardHeader, null,
          React.createElement(CardTitle, { className: "text-sm" }, "全部 Agent 工作状态"),
          React.createElement("span", { className: "agent-viz-fleet-count" }, agents.length + live.length + " 个")),
        React.createElement(CardContent, null,
          React.createElement("div", { className: "agent-viz-fleet" },
            agents.map(function (ag) {
              const bs = ag.by_status || {};
              const st = ag.running ? "running" : ag.blocked ? "blocked" : ag.ready ? "ready" : "done";
              const total = Math.max(1, ag.total || 1);
              const pct = function (k) { return Math.round(((bs[k] || 0) / total) * 100); };
              return React.createElement("div", { key: ag.name, className: "agent-viz-fleet-card" },
                React.createElement("div", { className: "agent-viz-fleet-head" },
                  React.createElement("span", { className: "agent-viz-fleet-name" }, ag.name),
                  React.createElement("span", { className: "agent-viz-fleet-state " + st },
                    ag.running ? "● 运行中" : ag.blocked ? "● 阻塞" : ag.ready ? "○ 就绪" : "✓ 空闲")),
                React.createElement("div", { className: "agent-viz-fleet-bar" },
                  React.createElement("i", { className: "seg run", style: { width: pct("running") + "%" } }),
                  React.createElement("i", { className: "seg ready", style: { width: pct("ready") + "%" } }),
                  React.createElement("i", { className: "seg blocked", style: { width: pct("blocked") + "%" } }),
                  React.createElement("i", { className: "seg done", style: { width: pct("done") + "%" } })),
                React.createElement("div", { className: "agent-viz-fleet-meta" },
                  React.createElement("span", null, "运行 " + ag.running),
                  React.createElement("span", null, "就绪 " + ag.ready),
                  React.createElement("span", null, "阻塞 " + ag.blocked),
                  React.createElement("span", null, "完成 " + ag.done)),
                ag.current ? React.createElement("div", { className: "agent-viz-fleet-current" },
                  "▶ " + ag.current) : null,
                ag.failed ? React.createElement("div", { className: "agent-viz-fleet-failed" },
                  "✗ " + ag.failed + " 个失败") : null,
              );
            }),
            live.map(function (ag) {
              return React.createElement("div", { key: ag.name, className: "agent-viz-fleet-card live" },
                React.createElement("div", { className: "agent-viz-fleet-head" },
                  React.createElement("span", { className: "agent-viz-fleet-name" }, ag.name),
                  React.createElement("span", { className: "agent-viz-fleet-state running" }, "● 活跃")),
                React.createElement("div", { className: "agent-viz-fleet-current" }, "▶ " + (ag.detail || "子代理运行中")));
            }),
          ),
        ),
      );
    }

    function Overview() {
      return React.createElement("div", null,
        React.createElement(Card, null,
          React.createElement(CardHeader, null, React.createElement(CardTitle, null, "多 Agent 协作可视化")),
          React.createElement(CardContent, null,
            React.createElement("div", { className: "agent-viz-stats" },
              React.createElement(Stat, { label: "活动 Agent", value: running }),
              React.createElement(Stat, { label: "任务总数", value: total }),
              React.createElement(Stat, { label: "已完成", value: done })),
            React.createElement(RefreshRow, { error: board.error || agents.error || activity.error || summary.error, lastUpdated: agents.lastUpdated, reload: function () { board.reload(); agents.reload(); activity.reload(); summary.reload(); } }),
          ),
        ),
        React.createElement(AgentFleet, null),
        React.createElement("div", { className: "agent-viz-grid" },
          React.createElement(Card, null,
            React.createElement(CardHeader, null, React.createElement(CardTitle, { className: "text-sm" }, "实时 Agent 状态")),
            React.createElement(CardContent, null,
              (a && a.agents && a.agents.length) ? a.agents.map(function (ag) {
                return React.createElement("div", { key: ag.id, className: "agent-viz-agent" },
                  React.createElement("span", { className: "agent-viz-dot " + (ag.status || "finished") }),
                  React.createElement("div", { style: { minWidth: 0, flex: 1 } },
                    React.createElement("div", { className: "name" }, ag.name),
                    React.createElement("div", { className: "detail" }, ag.detail || "")),
                  React.createElement(Badge, { variant: "outline", className: "text-[10px]" }, ag.status_label));
              }) : React.createElement("div", { className: "agent-viz-empty" }, "当前没有运行中的 agent。"),
            ),
          ),
          React.createElement(Card, null,
            React.createElement(CardHeader, null, React.createElement(CardTitle, { className: "text-sm" }, "实时活动流")),
            React.createElement(CardContent, null,
              React.createElement("div", { className: "agent-viz-activity" },
                (act && act.events && act.events.length) ? act.events.map(function (e, i) {
                  return React.createElement("div", { key: i, className: "line" },
                    React.createElement("span", { className: "time" }, e.time || ""),
                    React.createElement("span", { className: "agent" }, e.agent || ""),
                    React.createElement("span", { className: "text" }, e.text || ""));
                }) : React.createElement("div", { className: "agent-viz-empty" }, "暂无活动记录。"),
              ),
            ),
          ),
        ),
        React.createElement(Card, null,
          React.createElement(CardHeader, null, React.createElement(CardTitle, { className: "text-sm" }, "任务看板（Kanban）")),
          React.createElement(CardContent, null,
            (b && b.columns && b.columns.length) ?
              React.createElement("div", { className: "agent-viz-board" },
                b.columns.map(function (col) { return React.createElement(BoardColumn, { key: col.status, column: col }); }))
              : React.createElement("div", { className: "agent-viz-empty" }, "暂无任务。"),
          ),
        ),
      );
    }

    return React.createElement("div", { className: "agent-viz-root" },
      React.createElement(Tabs, { defaultValue: "overview" }, function (value, setValue) {
        return React.createElement(React.Fragment, null,
          React.createElement(TabsList, null,
            React.createElement(TabsTrigger, { value: "overview", active: value === "overview", onClick: function () { setValue("overview"); } }, "总览"),
            React.createElement(TabsTrigger, { value: "topology", active: value === "topology", onClick: function () { setValue("topology"); } }, "拓扑图"),
            React.createElement(TabsTrigger, { value: "flow", active: value === "flow", onClick: function () { setValue("flow"); } }, "消息流"),
            React.createElement(TabsTrigger, { value: "progress", active: value === "progress", onClick: function () { setValue("progress"); } }, "爬取进度"),
          ),
          value === "overview" ? React.createElement(Overview, null) : null,
          value === "topology" ? React.createElement(Card, null,
            React.createElement(CardHeader, null,
              React.createElement(CardTitle, null, "任务依赖拓扑图"),
              React.createElement("p", { className: "text-xs opacity-60" }, "节点 = 任务 · 箭头 = 依赖 · 颜色 = 状态（采集→清洗→建模）"),
            ),
            React.createElement(CardContent, null,
              React.createElement(RefreshRow, { error: topo.error, lastUpdated: topo.lastUpdated, reload: topo.reload }),
              React.createElement(TopologyView, { data: topo.data }),
            ),
          ) : null,
          value === "flow" ? React.createElement(Card, null,
            React.createElement(CardHeader, null,
              React.createElement(CardTitle, null, "Agent 消息流"),
              React.createElement("p", { className: "text-xs opacity-60" }, "每个泳道 = 一个子代理 · 事件按时间排序（思考→工具→结果）"),
            ),
            React.createElement(CardContent, null,
              React.createElement(RefreshRow, { error: flow.error, lastUpdated: flow.lastUpdated, reload: flow.reload }),
              React.createElement(FlowView, { data: flow.data }),
            ),
          ) : null,
          value === "progress" ? React.createElement(Card, null,
            React.createElement(CardHeader, null,
              React.createElement(CardTitle, null, "原材料爬取进度"),
              React.createElement("p", { className: "text-xs opacity-60" }, "动态扫描数据目录 · 新增公司自动出现"),
            ),
            React.createElement(CardContent, null,
              React.createElement(RefreshRow, { error: progress.error, lastUpdated: progress.lastUpdated, reload: progress.reload }),
              React.createElement(ProgressView, { data: progress.data }),
            ),
          ) : null,
        );
      }),
    );
  }

  window.__HERMES_PLUGINS__.register("agent-viz", AgentVizPage);
})();
