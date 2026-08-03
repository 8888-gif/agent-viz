"""Agent Viz — backend API routes for multi-agent collaboration visualization.

Reads Hermes internals: kanban.db (task board), delegation live transcripts,
and state.db (session activity). Mounted at /api/plugins/agent-viz/.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

router = APIRouter()

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / "AppData/Local/hermes")
KANBAN_DB = HERMES_HOME / "kanban.db"
STATE_DB = HERMES_HOME / "state.db"
LIVE_DIR = HERMES_HOME / "cache" / "delegation" / "live"

KANBAN_STATUS_ORDER = ["triage", "todo", "scheduled", "ready", "running", "review", "blocked", "done", "archived"]
KANBAN_STATUS_LABELS = {
    "triage": "待分诊", "todo": "待办", "scheduled": "已排期", "ready": "就绪",
    "running": "运行中", "review": "待评审", "blocked": "阻塞", "done": "完成", "archived": "已归档",
}
KANBAN_STATUS_COLORS = {
    "triage": "gray", "todo": "blue", "scheduled": "indigo", "ready": "teal",
    "running": "emerald", "review": "amber", "blocked": "red", "done": "green", "archived": "zinc",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: Path) -> Optional[sqlite3.Connection]:
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


@router.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "hermes_home": str(HERMES_HOME),
        "kanban_db": KANBAN_DB.exists(),
        "state_db": STATE_DB.exists(),
        "live_dir": LIVE_DIR.exists(),
        "time": _now_iso(),
    }


@router.get("/board")
async def board() -> Dict[str, Any]:
    """Kanban task board grouped by status, with links and recent events."""
    conn = _connect(KANBAN_DB)
    if conn is None:
        return {"columns": [], "tasks": [], "links": [], "total": 0, "error": "kanban.db not found"}

    try:
        tasks = [dict(r) for r in conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 500"
        ).fetchall()]
        links = [dict(r) for r in conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()]
    except sqlite3.Error as exc:
        conn.close()
        return {"columns": [], "tasks": [], "links": [], "error": str(exc)}
    conn.close()

    # Count by status
    counts: Dict[str, int] = {}
    for t in tasks:
        counts[t.get("status", "todo")] = counts.get(t.get("status", "todo"), 0) + 1

    columns = [
        {"status": s, "label": KANBAN_STATUS_LABELS.get(s, s), "color": KANBAN_STATUS_COLORS.get(s, "zinc"), "count": counts.get(s, 0)}
        for s in KANBAN_STATUS_ORDER if s in counts
    ]

    return {
        "columns": columns,
        "tasks": tasks,
        "links": links,
        "total": len(tasks),
        "running": counts.get("running", 0),
        "done": counts.get("done", 0),
    }


@router.get("/agents")
async def agents() -> Dict[str, Any]:
    """Live agent status: kanban running tasks + active delegations."""
    result: List[Dict[str, Any]] = []

    # 1. Kanban running/blocked tasks = named agents at work
    conn = _connect(KANBAN_DB)
    if conn is not None:
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, title, assignee, status, worker_pid, last_heartbeat_at, "
                "started_at, created_at, session_id, current_step_key FROM tasks "
                "WHERE status IN ('running','blocked','ready','review') "
                "ORDER BY last_heartbeat_at DESC"
            ).fetchall()]
            for r in rows:
                result.append({
                    "id": str(r["id"]),
                    "name": r["assignee"] or f"task-{r['id']}",
                    "kind": "kanban",
                    "status": r["status"],
                    "status_label": KANBAN_STATUS_LABELS.get(r["status"], r["status"]),
                    "detail": r["title"] or "",
                    "step": r["current_step_key"] or "",
                    "pid": r["worker_pid"],
                    "heartbeat_at": r["last_heartbeat_at"],
                    "started_at": r["started_at"],
                    "session_id": r["session_id"],
                })
        except sqlite3.Error:
            pass
        conn.close()

    # 2. Live delegation transcripts = in-flight subagents
    if LIVE_DIR.exists():
        for deleg_dir in sorted(LIVE_DIR.iterdir(), reverse=True)[:10]:
            if not deleg_dir.is_dir():
                continue
            manifest_path = deleg_dir / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                manifest = {}
            # A delegation with a live log file is likely in flight
            logs = sorted(deleg_dir.glob("task-*.log"))
            if not logs:
                continue
            last_log = logs[-1]
            mtime = last_log.stat().st_mtime
            age = time.time() - mtime
            # Only show active-ish delegations (updated within 5 min) plus recently finished (30 min)
            if age > 1800:
                continue
            goal = str(manifest.get("goal") or manifest.get("tasks") or "")
            if isinstance(goal, list):
                goal = "; ".join(str(g.get("goal", "")) for g in goal)[:200]
            result.append({
                "id": deleg_dir.name,
                "name": deleg_dir.name.replace("deleg_", "agent-")[:16],
                "kind": "delegation",
                "status": "running" if age < 300 else "finished",
                "status_label": "运行中" if age < 300 else "已结束",
                "detail": str(goal)[:120],
                "pid": None,
                "heartbeat_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                "started_at": None,
                "session_id": None,
                "log_path": str(last_log),
                "last_activity_seconds_ago": int(age),
            })

    return {"agents": result, "count": len(result), "time": _now_iso()}


@router.get("/activity")
async def activity(limit: int = 30) -> Dict[str, Any]:
    """Recent activity stream: tail of delegation transcripts + kanban events."""
    events: List[Dict[str, Any]] = []

    # 1. Tail live transcripts (most recent lines across active delegations)
    if LIVE_DIR.exists():
        for deleg_dir in sorted(LIVE_DIR.iterdir(), reverse=True)[:5]:
            for log_path in sorted(deleg_dir.glob("task-*.log"), reverse=True)[:2]:
                try:
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                except Exception:
                    continue
                for line in lines[-limit:]:
                    if not line.strip() or line.startswith("==="):
                        continue
                    # Format: "HH:MM:SS type | content"
                    parts = line.split(" | ", 1)
                    if len(parts) == 2:
                        ts, rest = parts
                        typ = "info"
                        events.append({
                            "time": ts.strip(),
                            "agent": deleg_dir.name.replace("deleg_", "agent-")[:16],
                            "type": typ,
                            "text": rest.strip()[:200],
                        })

    # 2. Kanban task events
    conn = _connect(KANBAN_DB)
    if conn is not None:
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT task_id, kind, payload, created_at FROM task_events "
                "ORDER BY id DESC LIMIT 20"
            ).fetchall()]
            for r in rows:
                events.append({
                    "time": r["created_at"] or "",
                    "agent": f"task-{r['task_id']}",
                    "type": "kanban",
                    "text": f"{r['kind']}: {str(r['payload'])[:120]}",
                })
        except sqlite3.Error:
            pass
        conn.close()

    # Normalize time field to a sortable string (kanban events use unix epoch)
    for e in events:
        t = e.get("time")
        if isinstance(t, (int, float)):
            try:
                e["time"] = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M:%S")
            except (ValueError, OSError, OverflowError):
                e["time"] = ""
        e["time"] = str(e.get("time") or "")
    events.sort(key=lambda e: e["time"], reverse=True)
    return {"events": events[:limit], "count": min(len(events), limit)}


@router.get("/sessions")
async def sessions(limit: int = 10) -> Dict[str, Any]:
    """Recent session activity from state.db — what agents have been doing."""
    conn = _connect(STATE_DB)
    if conn is None:
        return {"sessions": [], "error": "state.db not found"}
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, source, user_id, session_key, display_name, model, "
            "expiry_finalized FROM sessions ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()]
    except sqlite3.Error as exc:
        conn.close()
        return {"sessions": [], "error": str(exc)}
    conn.close()
    return {"sessions": rows}


@router.get("/topology")
async def topology() -> Dict[str, Any]:
    """Collaboration graph: kanban task dependency links as nodes/edges."""
    conn = _connect(KANBAN_DB)
    if conn is None:
        return {"nodes": [], "edges": []}
    try:
        tasks = [dict(r) for r in conn.execute(
            "SELECT id, title, assignee, status, created_at FROM tasks LIMIT 200"
        ).fetchall()]
        links = [dict(r) for r in conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()]
    except sqlite3.Error as exc:
        conn.close()
        return {"nodes": [], "edges": [], "error": str(exc)}
    conn.close()

    nodes = [
        {
            "id": str(t["id"]),
            "label": t["title"] or f"Task {t['id']}",
            "assignee": t["assignee"] or "unassigned",
            "status": t["status"],
            "created_at": t["created_at"],
        }
        for t in tasks
    ]
    edges = [
        {"source": str(l["parent_id"]), "target": str(l["child_id"])}
        for l in links if any(str(l["parent_id"]) == n["id"] for n in nodes) and any(str(l["child_id"]) == n["id"] for n in nodes)
    ]
    return {"nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}


# ── Message-flow graph from delegation live transcripts ────────────────

# Map transcript line type -> kind used by the flow graph
_TRANSCRIPT_KINDS = {
    "kickoff": "kickoff",
    "user": "user",
    "start": "start",
    "think": "think",
    "tool": "tool",
    "result": "result",
    "complete": "complete",
}


def _parse_transcript_log(log_path: Path) -> List[Dict[str, Any]]:
    """Parse a delegation task-N.log into structured events.

    Line format: ``HH:MM:SS <type> | <content>``
    """
    events: List[Dict[str, Any]] = []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return events
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("==="):
            continue
        # Match "HH:MM:SS type | content"
        parts = stripped.split(" | ", 1)
        if len(parts) != 2:
            continue
        ts_raw, content = parts
        ts_parts = ts_raw.strip().split()
        if not ts_parts:
            continue
        ts = ts_parts[0]
        typ = ts_parts[1] if len(ts_parts) > 1 else "info"
        kind = _TRANSCRIPT_KINDS.get(typ, "info")

        event: Dict[str, Any] = {
            "time": ts,
            "type": typ,
            "kind": kind,
            "text": content.strip()[:500],
        }

        # Extract tool name from "tool     | -> tool_name(args...)" lines
        if kind == "tool":
            m = re.search(r"->\s*([a-zA-Z_][a-zA-Z0-9_]*)", content)
            if m:
                event["tool"] = m.group(1)
        # Extract duration from "result   | tool_name ok 1.2s: {...}"
        elif kind == "result":
            m = re.search(r"ok\s+([0-9.]+)s", content)
            if m:
                event["duration_s"] = float(m.group(1))
            tool_m = re.search(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s", content)
            if tool_m:
                event["tool"] = tool_m.group(1)

        events.append(event)
    return events


@router.get("/flow")
async def flow(limit: int = 400) -> Dict[str, Any]:
    """Structured message-flow graph from delegation live transcripts.

    Returns lanes (one per delegation task) with ordered events, plus
    per-lane metadata (goal, status, start/end time).
    """
    lanes: List[Dict[str, Any]] = []

    if LIVE_DIR.exists():
        for deleg_dir in sorted(LIVE_DIR.iterdir(), reverse=True)[:8]:
            if not deleg_dir.is_dir():
                continue
            manifest_path = deleg_dir / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                manifest = {}
            tasks_meta = manifest.get("tasks") or []
            logs = sorted(deleg_dir.glob("task-*.log"))
            for log_path in logs:
                idx = 0
                try:
                    idx = int(log_path.stem.split("-")[-1])
                except ValueError:
                    idx = 0
                meta = tasks_meta[idx] if idx < len(tasks_meta) else {}
                goal = str(meta.get("goal") or manifest.get("goal") or "")[:160]
                events = _parse_transcript_log(log_path)
                if not events:
                    continue
                lanes.append({
                    "id": f"{deleg_dir.name}-{idx}",
                    "agent": f"{deleg_dir.name.replace('deleg_', 'agent-')}-{idx}"[:20],
                    "goal": goal,
                    "status": str(meta.get("status") or manifest.get("status") or "unknown"),
                    "started": str(manifest.get("started") or ""),
                    "completed": str(manifest.get("completed") or ""),
                    "event_count": len(events),
                    "events": events[-limit:],
                })

    return {"lanes": lanes, "lane_count": len(lanes), "time": _now_iso()}
