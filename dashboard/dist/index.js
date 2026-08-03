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

  /* ── Topology view (SVG dependency graph) ──────────────────── */
  const NODE_W = 210, NODE_H = 62, H_GAP = 60, V_GAP = 28;

  function layoutGraph(nodes, edges) {
    // Assign layers by longest-path from roots (no incoming edges)
    const indegree = {};
    const adj = {};
    nodes.forEach(function (n) { indegree[n.id] = 0; adj[n.id] = []; });
    edges.forEach(function (e) {
      if (adj[e.source]) adj[e.source].push(e.target);
      indegree[e.target] = (indegree[e.target] || 0) + 1;
    });
    const layers = {};
    const queue = [];
    nodes.forEach(function (n) { if ((indegree[n.id] || 0) === 0) { layers[n.id] = 0; queue.push(n.id); } });
    while (queue.length) {
      const id = queue.shift();
      (adj[id] || []).forEach(function (t) {
        layers[t] = Math.max(layers[t] || 0, (layers[id] || 0) + 1);
        queue.push(t);
      });
    }
    // Group by layer
    const byLayer = {};
    nodes.forEach(function (n) {
      const l = layers[n.id] == null ? 0 : layers[n.id];
      (byLayer[l] = byLayer[l] || []).push(n);
    });
    // Position: layer -> x, index within layer -> y (centered)
    const pos = {};
    const maxLayer = Math.max.apply(null, Object.keys(byLayer).map(Number).concat([0]));
    const totalW = (maxLayer + 1) * (NODE_W + H_GAP) - H_GAP + 40;
    Object.keys(byLayer).forEach(function (lk) {
      const l = Number(lk);
      const xs = 20 + l * (NODE_W + H_GAP);
      const list = byLayer[lk];
      const totalH = list.length * (NODE_H + V_GAP) - V_GAP + 20;
      list.forEach(function (n, i) {
        const yTop = (totalH - (list.length * (NODE_H + V_GAP) - V_GAP)) / 2 + i * (NODE_H + V_GAP) + 10;
        pos[n.id] = { x: xs, y: yTop };
      });
      byLayer[lk]._h = totalH;
    });
    const totalH = Math.max.apply(null, Object.keys(byLayer).map(function (k) { return byLayer[k]._h || 0; }).concat([200]));
    return { pos: pos, width: totalW, height: totalH };
  }

  function TopologyView({ data }) {
    if (!data || !data.nodes || data.nodes.length === 0) {
      return React.createElement("div", { className: "agent-viz-empty" },
        "暂无任务 — 创建 kanban 任务并用 `hermes kanban link` 建立依赖关系后这里会显示拓扑图。");
    }
    const { pos, width, height } = layoutGraph(data.nodes, data.edges);
    const edges = data.edges.filter(function (e) { return pos[e.source] && pos[e.target]; });

    return React.createElement("div", { className: "agent-viz-topo" },
      React.createElement("svg", {
        width: "100%", viewBox: "0 0 " + width + " " + height,
        style: { minHeight: Math.max(height, 260), background: "transparent" },
      },
        // Dependency edges (bezier curves with arrowheads)
        React.createElement("defs", null,
          React.createElement("marker", {
            id: "agent-viz-arrow", markerWidth: 8, markerHeight: 8,
            refX: 7, refY: 4, orient: "auto",
          },
            React.createElement("path", { d: "M0,0 L8,4 L0,8 Z", fill: "#64748b" }),
          ),
        ),
        edges.map(function (e, i) {
          const s = pos[e.source], t = pos[e.target];
          const sx = s.x + NODE_W, sy = s.y + NODE_H / 2;
          const tx = t.x, ty = t.y + NODE_H / 2;
          const mx = (sx + tx) / 2;
          return React.createElement("path", {
            key: "e" + i,
            d: "M" + sx + "," + sy + " C" + mx + "," + sy + " " + mx + "," + ty + " " + tx + "," + ty,
            fill: "none", stroke: "#64748b", strokeWidth: 1.5,
            strokeDasharray: "5,3", markerEnd: "url(#agent-viz-arrow)",
          });
        }),
        // Nodes
        data.nodes.map(function (n) {
          const p = pos[n.id];
          if (!p) return null;
          const color = STATUS_COLOR[n.status] || "#9ca3af";
          return React.createElement("g", { key: n.id, className: "agent-viz-topo-node" },
            React.createElement("rect", {
              x: p.x, y: p.y, width: NODE_W, height: NODE_H, rx: 8,
              fill: "color-mix(in srgb, " + color + " 12%, transparent)",
              stroke: color, strokeWidth: 1.5,
            }),
            React.createElement("rect", { x: p.x, y: p.y, width: 5, height: NODE_H, rx: 2.5, fill: color }),
            React.createElement("text", { x: p.x + 14, y: p.y + 22, fill: "var(--foreground, #eee)", fontSize: 12, fontWeight: 600 },
              React.createElement("tspan", null, (n.label || n.id).slice(0, 26)),
            ),
            React.createElement("text", { x: p.x + 14, y: p.y + 42, fill: "#94a3b8", fontSize: 10 },
              "[" + (STATUS_LABEL[n.status] || n.status) + "] " + (n.assignee || "未指派"),
            ),
          );
        }),
      ),
    );
  }

  /* ── Message-flow view (lane timeline) ─────────────────────── */
  function FlowView({ data }) {
    if (!data || !data.lanes || data.lanes.length === 0) {
      return React.createElement("div", { className: "agent-viz-empty" },
        "暂无消息流 — 运行 delegate_task 多 agent 协作后这里会实时展示每个子代理的动作序列。");
    }
    return React.createElement("div", { className: "agent-viz-flow" },
      data.lanes.map(function (lane) {
        return React.createElement("div", { key: lane.id, className: "agent-viz-lane" },
          React.createElement("div", { className: "agent-viz-lane-header" },
            React.createElement(Badge, { className: "agent-viz-lane-badge" }, lane.agent),
            React.createElement("span", { className: "agent-viz-lane-status" }, lane.status),
            React.createElement("span", { className: "agent-viz-lane-goal" }, lane.goal),
          ),
          React.createElement("div", { className: "agent-viz-lane-body" },
            lane.events.map(function (ev, i) {
              const color = KIND_COLOR[ev.kind] || "#9ca3af";
              const icon = ev.kind === "tool" ? "⚙" : ev.kind === "result" ? "✓" : ev.kind === "think" ? "💭" : ev.kind === "user" ? "👤" : ev.kind === "complete" ? "🏁" : "·";
              const dur = ev.duration_s != null ? " (" + ev.duration_s + "s)" : "";
              return React.createElement("div", { key: i, className: "agent-viz-ev", style: { borderLeftColor: color } },
                React.createElement("span", { className: "agent-viz-ev-time" }, ev.time),
                React.createElement("span", { className: "agent-viz-ev-icon", style: { color: color } }, icon),
                React.createElement("span", { className: "agent-viz-ev-kind", style: { color: color } },
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

  /* ── Main page with tabs ───────────────────────────────────── */
  function AgentVizPage() {
    const board = usePoll(function () { return SDK.fetchJSON(API + "/board"); }, []);
    const agents = usePoll(function () { return SDK.fetchJSON(API + "/agents"); }, []);
    const activity = usePoll(function () { return SDK.fetchJSON(API + "/activity"); }, []);
    const topo = usePoll(function () { return SDK.fetchJSON(API + "/topology"); }, []);
    const flow = usePoll(function () { return SDK.fetchJSON(API + "/flow"); }, []);

    const b = board.data, a = agents.data, act = activity.data;
    const running = (a && a.count) || 0;
    const total = (b && b.total) || 0;
    const done = (b && b.done) || 0;

    function Stat({ label, value }) {
      return React.createElement("div", { className: "agent-viz-stat" },
        React.createElement("b", null, value),
        React.createElement("span", null, label));
    }

    function BoardColumn({ column }) {
      const tasks = (b && b.tasks || []).filter(function (t) { return t.status === column.status; });
      return React.createElement("div", { className: "agent-viz-column" },
        React.createElement("h4", null,
          React.createElement("span", null, column.label),
          React.createElement("span", null, column.count)),
        tasks.map(function (t) {
          return React.createElement("div", { key: t.id, className: "agent-viz-task" },
            React.createElement("div", { className: "title" }, t.title || ("Task " + t.id)),
            React.createElement("div", { className: "meta" }, (t.assignee || "未指派") + " · " + (STATUS_LABEL[t.status] || t.status)));
        }));
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
            React.createElement(RefreshRow, { error: board.error || agents.error || activity.error, lastUpdated: agents.lastUpdated, reload: function () { board.reload(); agents.reload(); activity.reload(); } }),
          ),
        ),
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
        );
      }),
    );
  }

  window.__HERMES_PLUGINS__.register("agent-viz", AgentVizPage);
})();
