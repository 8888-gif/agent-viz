"""Agent Viz — backend API routes for multi-agent collaboration visualization.

Reads Hermes internals: kanban.db (task board), delegation live transcripts,
and state.db (session activity). Mounted at /api/plugins/agent-viz/.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / "AppData/Local/hermes")
KANBAN_DB = HERMES_HOME / "kanban.db"
STATE_DB = HERMES_HOME / "state.db"
LIVE_DIR = HERMES_HOME / "cache" / "delegation" / "live"

# Plugin config: user-editable scraper data root (set from the frontend)
PLUGIN_CONFIG = Path(__file__).resolve().parent / "config.json"
DEFAULT_SCRAPER_ROOT = r"D:\AI\codex_skill"


def _load_config() -> Dict[str, Any]:
    try:
        if PLUGIN_CONFIG.exists():
            return json.loads(PLUGIN_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_config(cfg: Dict[str, Any]) -> bool:
    try:
        PLUGIN_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _scraper_root() -> Path:
    return Path(_load_config().get("scraper_root", DEFAULT_SCRAPER_ROOT))


def _load_main_model_config() -> Dict[str, Any]:
    """Read the host's main model config (config.yaml -> model:) for LLM calls."""
    import yaml  # local import: hermes venv has pyyaml
    try:
        cfg_path = HERMES_HOME / "config.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            return cfg.get("model", {}) or {}
    except Exception:
        pass
    return {}

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

    # ── Derive display fields per task ─────────────────────────
    now = time.time()
    child_counts: Dict[str, int] = {}
    parent_counts: Dict[str, int] = {}
    for l in links:
        child_counts[l["child_id"]] = child_counts.get(l["child_id"], 0) + 1
        parent_counts[l["parent_id"]] = parent_counts.get(l["parent_id"], 0) + 1

    for t in tasks:
        tid = t.get("id", "")
        created = t.get("created_at") or t.get("started_at")
        completed = t.get("completed_at")
        t["deps"] = child_counts.get(tid, 0)          # 前置依赖数
        t["dep_of"] = parent_counts.get(tid, 0)        # 被多少任务依赖
        t["failed"] = bool(t.get("last_failure_error")) or (t.get("consecutive_failures") or 0) > 0
        t["failures"] = t.get("consecutive_failures") or 0
        t["failure_error"] = (t.get("last_failure_error") or "")[:160]
        t["runtime_s"] = None
        if created and completed:
            try:
                t["runtime_s"] = max(0, int(float(completed) - float(created)))
            except (TypeError, ValueError):
                t["runtime_s"] = None
        t["wait_s"] = None
        if created and not completed:
            try:
                t["wait_s"] = max(0, int(now - float(created)))
            except (TypeError, ValueError):
                t["wait_s"] = None

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
                "WHERE status IN ('running','blocked') "
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


@router.get("/scraper-config")
async def scraper_config() -> Dict[str, Any]:
    """Get the current scraper data root (configurable from frontend)."""
    root = _scraper_root()
    return {
        "scraper_root": str(root),
        "exists": root.exists(),
        "default": DEFAULT_SCRAPER_ROOT,
    }


@router.post("/scraper-config")
async def scraper_config_save(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Save a new scraper data root. Must be an existing directory."""
    new_root = str(payload.get("scraper_root") or "").strip()
    if not new_root:
        return {"ok": False, "error": "路径不能为空"}
    p = Path(new_root)
    if not p.exists() or not p.is_dir():
        return {"ok": False, "error": f"路径不存在或不是文件夹: {new_root}"}
    cfg = _load_config()
    cfg["scraper_root"] = str(p)
    if _save_config(cfg):
        return {"ok": True, "scraper_root": str(p)}
    return {"ok": False, "error": "保存失败"}


@router.get("/scraper-progress")
async def scraper_progress() -> Dict[str, Any]:
    """Raw-material scraper project progress — DYNAMIC scan of the configured
    scraper root (default D:\\AI\\codex_skill, changeable from the frontend).

    Discovers companies from the actual project structure (not a fixed CSV):
      - output/<company>/ with *_产品数据.json  -> 已完成
      - output/<company>/ with *_urls.csv       -> 已爬取(待提取)
      - memory/companies/<company>.md           -> 有记忆记录
      - 进度追踪.csv                              -> 手动跟踪状态(补充标注)
    New companies added later appear automatically.
    """
    root = _scraper_root()
    companies: Dict[str, Dict[str, Any]] = {}
    def upsert(name: str) -> Dict[str, Any]:
        c = companies.setdefault(name, {
            "name": name, "status": "待处理", "output": "", "note": "",
            "has_json": False, "has_urls": False, "has_memory": False, "sources": [],
        })
        return c

    # 1. Scan output/<company>/ directories (the ground truth of what's done)
    out_root = root / "output"
    if out_root.exists():
        for d in out_root.iterdir():
            if not d.is_dir():
                continue
            name = d.name
            # Skip non-company dirs: batch containers, meta dirs, artifacts
            if name.startswith(("batch_", "2026", "_", "archived", "archive")):
                continue
            c = upsert(name)
            c["sources"].append("output")
            for f in d.rglob("*产品数据*.json"):
                c["has_json"] = True
                c["output"] = f.name
                c["status"] = "已完成"
            for f in d.rglob("*_urls.csv"):
                c["has_urls"] = True
                if not c["has_json"]:
                    c["status"] = "已爬取·待提取"

    # 2. Scan memory/companies/*.md
    mem_dir = root / "memory" / "companies"
    if mem_dir.exists():
        for f in mem_dir.glob("*.md"):
            name = f.stem
            if name in ("README", "readme") or name.startswith("_"):
                continue
            c = upsert(name)
            c["has_memory"] = True
            c["sources"].append("memory")
            if not c["has_json"] and not c["has_urls"]:
                c["status"] = "有记录·待处理"

    # 3. Merge CSV tracking status/notes
    csv_path = root / "data" / "进度追踪.csv"
    if csv_path.exists():
        try:
            with open(csv_path, encoding="utf-8-sig", errors="replace", newline="") as f:
                rows = list(csv.reader(f))
            for row in rows[1:]:
                if len(row) < 6:
                    row = (row + [""] * 6)[:6]
                name = row[1].strip()
                if name:
                    c = upsert(name)
                    c["sources"].append("csv")
                    if row[3].strip():
                        # CSV status is authoritative when present
                        if c["status"] == "待处理" or c["status"].startswith("有记录"):
                            c["status"] = row[3].strip()
                    c["note"] = row[5].strip()
                    if row[4].strip():
                        c["output"] = row[4].strip()
        except Exception:
            pass

    result = sorted(companies.values(), key=lambda c: c["name"])
    stats: Dict[str, int] = {}
    for c in result:
        st = c["status"] or "待处理"
        stats[st] = stats.get(st, 0) + 1

    return {"companies": result, "stats": stats, "total": len(result)}


@router.post("/scraper-progress/import")
async def scraper_progress_import(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Import companies from ARBITRARY text using the LLM.

    The user pastes any format (excel dump, web table, markdown list, plain
    notes). The LLM figures out the structure and extracts structured company
    records {name, url, status, note}. Returns parsed records for preview;
    the frontend asks the user to confirm before actually writing the CSV.
    """
    raw = str(payload.get("text") or "").strip()
    mode = str(payload.get("mode") or "preview")  # preview | commit
    if not raw:
        return {"ok": False, "error": "没有可导入的内容"}

    # LLM parse: structure-agnostic extraction
    provider = os.environ.get("HERMES_LLM_PROVIDER") or "deepseek"
    model = os.environ.get("HERMES_LLM_MODEL") or "deepseek-chat"
    base_url = os.environ.get("HERMES_LLM_BASE_URL") or None
    api_key = os.environ.get("DEEPSEEK_API_KEY") or None
    try:
        cfg = _load_main_model_config()
        provider = cfg.get("provider", provider)
        model = cfg.get("default", model)
        base_url = cfg.get("base_url") or base_url
        api_key = os.environ.get("DEEPSEEK_API_KEY") or None
    except Exception:
        pass

    prompt = (
        "你是数据整理专家。用户会粘贴一段内容，格式可能是表格、清单、网页文字、PDF提取文本等任意格式。\n"
        "请识别出其中的企业/公司记录，输出严格 JSON 数组：\n"
        '[{"name": "企业名称", "url": "官网URL(如无则空)", "status": "状态(如无则 待处理)", "note": "备注(如无则空)"}]\n'
        "规则：\n"
        "1. 只提取看起来是化工/材料企业的条目，忽略无关文字。\n"
        "2. 如果一行有公司名和网址，拆成 name + url。\n"
        "3. 状态可能是 已完成/处理中/待处理/D级 等，原样保留，没有就写 待处理。\n"
        "4. 不要发明数据，不确定的字段留空。\n"
        f"用户粘贴的内容：\n{raw[:6000]}"
    )
    try:
        from agent.auxiliary_client import call_llm
        resp = call_llm(
            task=None, provider=provider, model=model, base_url=base_url, api_key=api_key,
            messages=[
                {"role": "system", "content": "你输出严格 JSON，不要多余文字。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1, max_tokens=1500,
        )
        text = ""
        if isinstance(resp, dict):
            text = str(resp.get("content") or resp.get("message") or resp.get("text") or "")
        elif hasattr(resp, "choices") and resp.choices:
            text = str(resp.choices[0].message.content or "")
        elif hasattr(resp, "content"):
            text = str(resp.content)
        else:
            text = str(resp)
        # Robust JSON array extraction
        parsed = None
        import re as _re
        m = _re.search(r"\[.*\]", text, _re.S)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception:
                parsed = None
        if parsed is None:
            decoder = json.JSONDecoder()
            idx = 0
            while idx < len(text):
                try:
                    obj, end = decoder.raw_decode(text, idx)
                    if isinstance(obj, list):
                        parsed = obj
                        break
                except Exception:
                    pass
                idx = text.find("[", idx + 1)
                if idx < 0:
                    break
        if parsed is None:
            return {"ok": False, "error": "LLM 未能解析出公司列表"}
        records = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            records.append({
                "name": name,
                "url": str(item.get("url") or "").strip(),
                "status": str(item.get("status") or "待处理").strip(),
                "note": str(item.get("note") or "").strip(),
            })
        if not records:
            return {"ok": False, "error": "未识别出任何公司"}

        if mode == "commit":
            written = _append_companies_to_csv(records)
            return {"ok": True, "records": records, "written": written}

        return {"ok": True, "records": records, "written": 0}
    except Exception as exc:
        return {"ok": False, "error": f"LLM 调用失败: {str(exc)[:100]}"}


def _append_companies_to_csv(records: List[Dict[str, str]]) -> int:
    """Append parsed company records to 进度追踪.csv (create if missing)."""
    csv_path = _scraper_root() / "data" / "进度追踪.csv"
    try:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        import io as _io
        existing = []
        if csv_path.exists():
            with open(csv_path, encoding="utf-8-sig", errors="replace", newline="") as f:
                existing = list(csv.reader(f))
        header = existing[0] if existing else ["序号", "企业名称", "官网URL", "状态", "产出文件", "备注"]
        rows = existing[1:] if existing else []
        existing_names = {r[1].strip() for r in rows if len(r) > 1 and r[1].strip()}
        added = 0
        for rec in records:
            if rec["name"] in existing_names:
                continue  # dedupe
            rows.append([str(len(rows) + 1), rec["name"], rec["url"], rec["status"], "", rec["note"]])
            existing_names.add(rec["name"])
            added += 1
        # renumber
        for i, r in enumerate(rows, start=1):
            if r:
                r[0] = str(i)
        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        csv_path.write_text("\ufeff" + buf.getvalue(), encoding="utf-8")
        return added
    except Exception:
        return 0


@router.get("/scraper-progress/llm-export")
async def scraper_progress_llm_export(limit: int = 100) -> Response:
    """LLM-enhanced export: read each company's product JSON + memory, and
    use the host LLM to produce a deep summary (product count, quality grade,
    field completeness, risks) merged into the CSV.

    Runs in a worker thread so the event loop stays responsive. Only
    companies that have a product JSON (or a memory file) are analyzed.
    """
    result = await scraper_progress()
    companies = result.get("companies", [])[:limit]
    if not companies:
        return Response("没有可导出的公司数据", media_type="text/plain; charset=utf-8")

    root = _scraper_root()

    def _read_company_files(c: Dict[str, Any]) -> Dict[str, str]:
        """Collect the raw material LLM should analyze for one company."""
        name = c.get("name", "")
        files: Dict[str, str] = {}
        # product JSONs
        out_dir = root / "output" / name
        if out_dir.exists():
            for f in sorted(out_dir.rglob("*产品数据*.json")):
                try:
                    files["产品数据"] = f.read_text(encoding="utf-8", errors="replace")[:4000]
                except Exception:
                    pass
                break
        # memory file
        mem = root / "memory" / "companies" / (name + ".md")
        if mem.exists():
            try:
                files["记忆"] = mem.read_text(encoding="utf-8", errors="replace")[:1500]
            except Exception:
                pass
        return files

    def _llm_analyze(c: Dict[str, Any], files: Dict[str, str]) -> Dict[str, str]:
        """Call host LLM to summarize one company's data."""
        if not files:
            return {"产品数": "0", "数据质量": "N/A", "字段完整度": "N/A", "风险提示": "无产出文件"}
        import asyncio
        try:
            from agent.auxiliary_client import call_llm
        except Exception:
            return {"产品数": "?", "数据质量": "?", "字段完整度": "?", "风险提示": "LLM 不可用"}

        # Resolve the host's main model config so we reuse the same provider/auth
        provider = os.environ.get("HERMES_LLM_PROVIDER") or "deepseek"
        model = os.environ.get("HERMES_LLM_MODEL") or "deepseek-chat"
        base_url = os.environ.get("HERMES_LLM_BASE_URL") or None
        api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("HERMES_LLM_API_KEY") or None
        try:
            cfg = _load_main_model_config()
            provider = cfg.get("provider", provider)
            model = cfg.get("default", model)
            base_url = cfg.get("base_url") or base_url
            api_key = os.environ.get("DEEPSEEK_API_KEY") or None
        except Exception:
            pass

        material = "\n\n".join(f"【{k}】\n{v}" for k, v in files.items())
        prompt = (
            "你是原材料产品数据质检专家。分析以下某厂家的产品数据，输出严格 JSON：\n"
            '{"产品数": 数字, "数据质量": "A/B/C", "字段完整度": "高/中/低", "风险提示": "一句话"}。\n'
            "数据质量：A=完整无异常，B=有字段缺失但可用，C=数据严重不足。\n"
            "字段完整度：检查 产品名称/型号/简介/特性/用途/技术规格 是否齐全。\n"
            "风险提示：指出数据问题（如英文键名、乱码、规格缺失、URL 无效）。\n"
            f"公司：{c.get('name')}，状态：{c.get('status')}。\n\n{material}"
        )
        try:
            resp = call_llm(
                task=None,
                provider=provider,
                model=model,
                base_url=base_url,
                api_key=api_key,
                messages=[
                    {"role": "system", "content": "你输出严格 JSON，不要多余文字。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=300,
            )
            text = ""
            if isinstance(resp, dict):
                text = str(resp.get("content") or resp.get("message") or resp.get("text") or "")
            elif hasattr(resp, "choices") and resp.choices:
                text = str(resp.choices[0].message.content or "")
            elif hasattr(resp, "content"):
                text = str(resp.content)
            else:
                text = str(resp)
            import re as _re
            m = _re.search(r"\{.*\}", text, _re.S)
            parsed = None
            if m:
                try:
                    parsed = json.loads(m.group(0))
                except Exception:
                    parsed = None
            if parsed is None:
                # Robust fallback: scan for the first balanced {...} object
                decoder = json.JSONDecoder()
                idx = 0
                while idx < len(text):
                    try:
                        obj, end = decoder.raw_decode(text, idx)
                        if isinstance(obj, dict):
                            parsed = obj
                            break
                    except Exception:
                        pass
                    idx = text.find("{", idx + 1)
                    if idx < 0:
                        break
            if parsed:
                return {
                    "产品数": str(parsed.get("产品数", "?")),
                    "数据质量": str(parsed.get("数据质量", "?")),
                    "字段完整度": str(parsed.get("字段完整度", "?")),
                    "风险提示": str(parsed.get("风险提示", ""))[:80],
                }
            return {"产品数": "?", "数据质量": "?", "字段完整度": "?", "风险提示": "解析失败"}
        except Exception as exc:
            return {"产品数": "?", "数据质量": "?", "字段完整度": "?", "风险提示": f"LLM错误:{str(exc)[:40]}"}

    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["序号", "企业名称", "状态", "产品数", "数据质量", "字段完整度", "风险提示", "产出文件", "备注"])
    for i, c in enumerate(companies, start=1):
        files = _read_company_files(c)
        deep = _llm_analyze(c, files)
        writer.writerow([
            i,
            c.get("name", ""),
            c.get("status", ""),
            deep.get("产品数", "0"),
            deep.get("数据质量", "N/A"),
            deep.get("字段完整度", "N/A"),
            deep.get("风险提示", ""),
            c.get("output", ""),
            c.get("note", ""),
        ])
    csv_data = "\ufeff" + buf.getvalue()

    filename = "scraper_llm_export_%s.csv" % time.strftime("%Y%m%d_%H%M%S")
    from urllib.parse import quote
    filename_ascii = quote(filename)
    return Response(
        content=csv_data.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={filename_ascii}; filename*=UTF-8''{filename_ascii}",
        },
    )


@router.get("/agents-summary")
async def agents_summary() -> Dict[str, Any]:
    """One-screen overview of ALL agents' work state.

    Groups kanban tasks by assignee and reports each agent's status
    distribution + current running task, plus any live subagents from
    delegation transcripts.
    """
    conn = _connect(KANBAN_DB)
    agents_map: Dict[str, Dict[str, Any]] = {}
    if conn is not None:
        try:
            tasks = [dict(r) for r in conn.execute(
                "SELECT id, title, assignee, status, worker_pid, last_heartbeat_at, "
                "started_at, created_at, completed_at, last_failure_error FROM tasks "
                "ORDER BY created_at DESC LIMIT 500"
            ).fetchall()]
        except sqlite3.Error:
            tasks = []
        conn.close()

        for t in tasks:
            name = t.get("assignee") or "unassigned"
            a = agents_map.setdefault(name, {
                "name": name,
                "by_status": {},
                "running": [], "ready": [], "blocked": [], "done": [], "failed": 0,
                "total": 0, "heartbeat_at": None,
            })
            st = t.get("status") or "todo"
            a["by_status"][st] = a["by_status"].get(st, 0) + 1
            a["total"] += 1
            if t.get("last_failure_error"):
                a["failed"] += 1
            if st == "running":
                a["running"].append({"id": t["id"], "title": t["title"], "pid": t.get("worker_pid")})
            elif st == "ready":
                a["ready"].append({"id": t["id"], "title": t["title"]})
            elif st == "blocked":
                a["blocked"].append({"id": t["id"], "title": t["title"], "error": (t.get("last_failure_error") or "")[:100]})
            elif st == "done":
                a["done"].append({"id": t["id"], "title": t["title"]})
            hb = t.get("last_heartbeat_at")
            if hb and (a["heartbeat_at"] is None or hb > a["heartbeat_at"]):
                a["heartbeat_at"] = hb
            # latest running task = "current work"
            if st == "running" and not a.get("current"):
                a["current"] = t["title"]
            # trim lists for payload size
            a["running"] = a["running"][:5]
            a["ready"] = a["ready"][:5]
            a["blocked"] = a["blocked"][:5]

    # Active subagents from live transcripts (recently updated)
    live: List[Dict[str, Any]] = []
    if LIVE_DIR.exists():
        now = time.time()
        for deleg_dir in sorted(LIVE_DIR.iterdir(), reverse=True)[:8]:
            for log_path in sorted(deleg_dir.glob("task-*.log"))[:2]:
                try:
                    age = now - log_path.stat().st_mtime
                except OSError:
                    continue
                if age <= 180:
                    live.append({
                        "name": deleg_dir.name.replace("deleg_", "agent-")[:16],
                        "status": "running",
                        "detail": "子代理活跃 · " + str(log_path.name),
                    })

    result = [
        {
            "name": a["name"],
            "by_status": a["by_status"],
            "total": a["total"],
            "running": len(a["running"]),
            "ready": len(a["ready"]),
            "blocked": len(a["blocked"]),
            "done": len(a["done"]),
            "failed": a["failed"],
            "current": a.get("current"),
            "heartbeat_at": a["heartbeat_at"],
            "task_preview": (a["running"] or a["ready"] or a["blocked"] or a["done"])[:3],
        }
        for a in sorted(agents_map.values(), key=lambda x: -(x["total"]))
    ]
    return {"agents": result, "live_subagents": live, "count": len(result) + len(live)}


@router.get("/activity")
async def activity(limit: int = 30) -> Dict[str, Any]:
    """Recent activity stream: tail of delegation transcripts + kanban events."""
    events: List[Dict[str, Any]] = []

    # 1. Tail live transcripts (most recent lines across ACTIVE delegations —
    #    only logs modified within the last 3 minutes are "live")
    if LIVE_DIR.exists():
        now = time.time()
        for deleg_dir in sorted(LIVE_DIR.iterdir(), reverse=True)[:5]:
            for log_path in sorted(deleg_dir.glob("task-*.log"), reverse=True)[:2]:
                try:
                    if now - log_path.stat().st_mtime > 180:
                        continue  # stale — skip historical transcripts
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

    # 2. Kanban task events (recent only — last 3 minutes)
    conn = _connect(KANBAN_DB)
    if conn is not None:
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT task_id, kind, payload, created_at FROM task_events "
                "WHERE created_at IS NOT NULL AND created_at > ? "
                "ORDER BY id DESC LIMIT 20",
                (now - 180,),
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
    """Collaboration graph: kanban task deps + delegate_task parent/child agent chains.

    Node types:
      - task   : kanban task (kanban.db)
      - agent  : a session (main / subagent / cron) that spawned work
      - deleg  : a delegate_task dispatch (the delegation itself)
    Edges:
      - task parent->child dependencies (task_links)
      - session -> delegation -> subagent-session chains (state.db async_delegations)
    """
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    seen: set = set()

    # ── 1. Kanban tasks + dependencies ────────────────────────
    conn = _connect(KANBAN_DB)
    if conn is not None:
        try:
            tasks = [dict(r) for r in conn.execute(
                "SELECT id, title, body, assignee, status, created_at, completed_at, priority "
                "FROM tasks LIMIT 200"
            ).fetchall()]
            links = [dict(r) for r in conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()]
        except sqlite3.Error as exc:
            conn.close()
            return {"nodes": [], "edges": [], "error": str(exc)}
        conn.close()

        for t in tasks:
            nid = "task:" + str(t["id"])
            nodes.append({
                "id": nid,
                "label": t["title"] or f"Task {t['id']}",
                "body": (t["body"] or "")[:200],
                "assignee": t["assignee"] or "unassigned",
                "status": t["status"],
                "created_at": t["created_at"],
                "completed_at": t["completed_at"],
                "priority": t["priority"],
                "kind": "task",
            })
            seen.add(nid)
        for l in links:
            edges.append({"source": "task:" + str(l["parent_id"]), "target": "task:" + str(l["child_id"])})

    # ── 2. Agent session graph from state.db ──────────────────
    conn = _connect(STATE_DB)
    if conn is not None:
        try:
            sessions = [dict(r) for r in conn.execute(
                "SELECT id, source, session_key, display_name, chat_type FROM sessions "
                "WHERE source IN ('main','subagent','weixin','telegram','cron','agent','cli','local','api') "
                "ORDER BY rowid DESC LIMIT 100"
            ).fetchall()]
            delegs = [dict(r) for r in conn.execute(
                "SELECT delegation_id, origin_session, parent_session_id, state, dispatched_at, completed_at "
                "FROM async_delegations ORDER BY dispatched_at DESC LIMIT 50"
            ).fetchall()]
        except sqlite3.Error:
            sessions, delegs = [], []
        conn.close()

        # session id -> node id map (use session.id as stable key)
        def sess_node(s: dict) -> Optional[str]:
            sid = str(s["id"])
            key = "sess:" + sid
            if sid in seen:
                return key
            seen.add(sid)
            src = s.get("source") or "agent"
            is_sub = src == "subagent"
            # label: derive from session_key tail or source
            sk = str(s.get("session_key") or "")
            if sk.startswith("agent:main:"):
                label = "主 Agent"
            elif is_sub:
                label = "子 Agent"
            else:
                label = src
            # A subagent session is only "running" if a matching live transcript
            # exists and was updated recently (<= 5 min). Historical subagent
            # sessions are shown as "done" so the graph reflects reality.
            is_alive = False
            if is_sub and LIVE_DIR.exists():
                try:
                    for deleg_dir in LIVE_DIR.iterdir():
                        if not deleg_dir.is_dir():
                            continue
                        for log_path in deleg_dir.glob("task-*.log"):
                            if sid in log_path.read_text(encoding="utf-8", errors="replace")[:2000]:
                                age = time.time() - log_path.stat().st_mtime
                                if age <= 300:
                                    is_alive = True
                                break
                        if is_alive:
                            break
                except Exception:
                    is_alive = False
            nodes.append({
                "id": key,
                "label": label,
                "body": "",
                "assignee": "",
                "status": "running" if is_sub and is_alive else "done",
                "created_at": None,
                "completed_at": None,
                "priority": 0,
                "kind": "agent",
                "source": src,
                "session_key": sk[:120],
                "alive": is_alive,
            })
            return key

        # parent session -> delegation -> child subagent session
        sub_by_parent = {}
        for d in delegs:
            # map origin_session key -> session id if we have it
            parent_sid = str(d.get("parent_session_id") or "")
            deleg_id = "deleg:" + str(d.get("delegation_id") or "")
            state = str(d.get("state") or "unknown")
            if deleg_id not in seen:
                seen.add(deleg_id)
                nodes.append({
                    "id": deleg_id,
                    "label": "delegate_task",
                    "body": "",
                    "assignee": "",
                    "status": state if state in KANBAN_STATUS_COLORS else "done",
                    "created_at": d.get("dispatched_at"),
                    "completed_at": d.get("completed_at"),
                    "priority": 0,
                    "kind": "deleg",
                    "delegation_id": str(d.get("delegation_id") or ""),
                })
            # parent session node
            if parent_sid:
                pkey = "sess:" + parent_sid
                pnode = next((x for x in nodes if x["id"] == pkey), None)
                if pnode is None:
                    # synthetic parent node from delegation metadata
                    if parent_sid not in seen:
                        seen.add(parent_sid)
                        nodes.append({
                            "id": pkey, "label": "主 Agent", "body": "", "assignee": "",
                            "status": "done", "created_at": None, "completed_at": None,
                            "priority": 0, "kind": "agent", "source": "main",
                            "session_key": str(d.get("origin_session") or "")[:120],
                        })
                edges.append({"source": pkey, "target": deleg_id})
            sub_by_parent[deleg_id] = d

        # link subagent sessions to their delegation via origin/parent matching
        for s in sessions:
            sid = str(s["id"])
            if s.get("source") == "subagent":
                skey = "sess:" + sid
                # find a delegation whose parent chain leads here: match by delegation_id appearing in session origin
                linked = False
                for d in delegs:
                    deleg_id = "deleg:" + str(d.get("delegation_id") or "")
                    origin = str(d.get("origin_session") or "")
                    if sid in origin or origin in sid:
                        edges.append({"source": deleg_id, "target": skey})
                        linked = True
                        break
                if not linked:
                    # orphan subagent — attach to most recent delegation
                    if delegs:
                        last = "deleg:" + str(delegs[0].get("delegation_id") or "")
                        edges.append({"source": last, "target": skey})
                sess_node(s)

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
