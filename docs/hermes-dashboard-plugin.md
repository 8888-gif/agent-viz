---
name: hermes-dashboard-plugin
description: 开发 Hermes web dashboard 插件（自定义 tab/可视化）的完整流程与踩坑记录。
platforms: [windows, linux, macos]
---

# Hermes Dashboard 插件开发

开发 `hermes dashboard`（web 管理面板）的自定义插件：新增 tab、可视化面板、读 Hermes 内部数据。

## 触发场景

- 用户要做 dashboard 自定义 tab / 可视化 UI / 数据面板
- 需要把 kanban / 子代理 / 会话数据可视化
- 想扩展 Hermes web dashboard 而不 fork 代码

## 插件结构

```
~/.hermes/plugins/<name>/
└── dashboard/
    ├── manifest.json        # 必需：tab 配置、入口、api 声明
    ├── dist/
    │   ├── index.js         # 必需：IIFE JS bundle（无构建步骤）
    │   └── style.css        # 可选：自定义样式
    └── plugin_api.py        # 可选：FastAPI 后端路由（挂 /api/plugins/<name>/）
```

## 关键步骤

1. **manifest.json**：
   ```json
   {
     "name": "my-plugin", "label": "显示名", "icon": "Activity",
     "tab": { "path": "/my-plugin", "position": "after:sessions" },
     "entry": "dist/index.js", "css": "dist/style.css", "api": "plugin_api.py"
   }
   ```
   icon 用 Lucide 名称（Activity/BarChart3/Database/Globe/Zap 等）。

2. **前端 index.js**（IIFE，用 `window.__HERMES_PLUGIN_SDK__`，不 import React）：
   ```js
   const SDK = window.__HERMES_PLUGIN_SDK__;
   const { React } = SDK;
   const { useState, useEffect, useCallback } = SDK.hooks;
   const { Card, CardHeader, CardTitle, CardContent, Badge, Button, Tabs, TabsList, TabsTrigger } = SDK.components;
   function MyPage() { return React.createElement(Card, null, ...); }
   window.__HERMES_PLUGINS__.register("my-plugin", MyPage);
   ```

3. **后端 plugin_api.py**：导出 `router = APIRouter()`，路由自动挂 `/api/plugins/<name>/`。可 `from hermes_cli.config import load_config`、`from hermes_state import SessionDB` 直接读 Hermes 内部。

4. **启用插件**：必须在 `config.yaml` 的 `plugins.enabled` 列表中加入插件名，否则 dashboard 不加载（#46435 门禁）。**不要用 `hermes config set plugins.enabled '["x"]'`——会把列表存成字符串**；用 Python yaml 直接改：
   ```python
   import yaml; p=r'C:\Users\Administrator\AppData\Local\hermes\config.yaml'
   cfg=yaml.safe_load(open(p,encoding='utf-8')); cfg.setdefault('plugins',{})['enabled']=['agent-viz']
   yaml.safe_dump(cfg,open(p,'w',encoding='utf-8'),allow_unicode=True,sort_keys=False)
   ```

5. **启动 dashboard**：如果从桌面 App 环境启动，必须先 unset 桌面变量否则加载桌面 UI 而非管理面板：
   ```bash
   unset HERMES_DESKTOP HERMES_WEB_DIST HERMES_SERVE_HEADLESS && hermes dashboard
   ```

## 数据源（读 Hermes 内部）

| 数据 | 路径/API | 用途 |
|---|---|---|
| Kanban 任务 | `~/.hermes/kanban.db` 表 `tasks`/`task_links`/`task_events` | 任务看板、拓扑图 |
| 子代理实时日志 | `~/.hermes/cache/delegation/live/<deleg>/task-N.log` + `manifest.json` | 消息流/活动流 |
| 会话记录 | `~/.hermes/state.db` 表 `sessions`/`messages` | 会话活动 |
| 环境 | `HERMES_HOME`（Windows 默认 `%LOCALAPPDATA%\hermes`） | 路径解析 |

live_transcripts 日志格式：`HH:MM:SS <type> | <content>`，type ∈ kickoff/user/start/think/tool/result/complete。

## 坑（务必记住）

1. **SDK 的 Tabs 是 render-prop 模式**：`Tabs` 的 children 是函数 `(value, setValue) => JSX`，用 `defaultValue` 初始化；`TabsTrigger` 用 `active={value===x}` + `onClick` 切换。**不要**用常规受控组件写法（`value`/`onValueChange`）——会导致整个 dashboard SPA 崩溃（白屏）。
2. **用户插件必须进 `plugins.enabled`**，否则 404 "Plugin not found"。
3. **`hermes config set` 数组会存成字符串**（`'["a"]'` 而非 YAML 列表），用 Python yaml 直接改。
4. **桌面 App 环境变量劫持**：`HERMES_DESKTOP=1` + `HERMES_WEB_DIST`（app.asar 路径）会让 `hermes dashboard` 加载桌面前端（报 "Desktop IPC bridge is unavailable"，issue #52945）。unset 三个变量即可。
5. **后端 500 常见原因**：混合类型排序（Unix 时间戳 int vs 字符串），统一 strftime 转字符串再排。
6. 插件 API 有 auth 门禁：curl 会 401，浏览器会话里 `SDK.fetchJSON` 正常。测试用浏览器 console 的 `window.__HERMES_PLUGIN_SDK__.fetchJSON(...)`。
7. **插件后端调 LLM**：`PluginLlm` 需要 PluginContext（路由拿不到），直接用底层同步函数：
   ```python
   from agent.auxiliary_client import call_llm
   resp = call_llm(task=None, provider=cfg.get("provider","deepseek"), model=cfg.get("default"),
                   base_url=cfg.get("base_url"), api_key=os.environ.get("DEEPSEEK_API_KEY"),
                   messages=[...], temperature=0.1, max_tokens=300)
   ```
   - **返回类型是 OpenAI ChatCompletion**：取文本用 `resp.choices[0].message.content`（不是 `resp.content`！那是 None）。
   - **不要用 `task="vision"` 等辅助任务名**——那些走 openrouter/nous 辅助 provider（未配置会报 "No LLM provider configured"）；显式传主配置的 provider/model/api_key。
   - 从 `config.yaml` 的 `model:` 段读主配置（pyyaml），`DEEPSEEK_API_KEY` 从 .env 读。
   - **LLM 输出 JSON 解析要鲁棒**：用 `json.JSONDecoder().raw_decode` 逐位置扫描，不要用贪婪正则 `\{.*\}`（LLM 常在 JSON 后加解释文字导致 `Extra data` 错误）。
   - 同步调用会阻塞事件循环 → 用 `asyncio.to_thread` 或在 FastAPI 里直接跑（简单场景可接受）。
8. **CSV 导出注意**：
   - **HTTP 头不能含中文文件名**（latin-1 编码限制）→ 用 ASCII 文件名 + `filename*=UTF-8''` 或干脆全 ASCII：`scraper_export_20260803_111326.csv`。
   - **Excel 中文乱码** → 加 BOM：`"\ufeff" + csv_content`。
   - 返回用 `fastapi.responses.Response(content=..., media_type="text/csv; charset=utf-8", headers={"Content-Disposition": ...})`。
   - 前端下载按钮用 `<a href="/api/plugins/<name>/export" download>`。
9. **用户可配置路径**：不要硬编码数据目录。存插件自己的 `config.json`（`Path(__file__).resolve().parent / "config.json"`），提供 GET/POST `/xxx-config` 端点（POST 校验 `Path(p).exists() and is_dir()`），前端渲染设置面板。文件用 `Path(__file__)` 定位——**注意插件物理路径可能是 junction 指向 D:\hermes-data**。
10. **SDK.fetchJSON POST**：**必须显式传 `headers: {"Content-Type": "application/json"}`**，否则 body 被当字符串 → 后端 422 "Input should be a valid dictionary"。
11. **React.createElement 三元表达式**：`cond ? createElement(...) : null` —— `:` 不能省！漏了会报 `Unexpected token ','`，且错误位置可能指向**下一行**（解析器恢复点），排错时检查整个 JSX 块而不是只看报错行。

## 验证

1. `node --check dist/index.js` + Python `ast.parse` 语法检查
2. 重启 dashboard，浏览器开 `http://127.0.0.1:9119/<tab-path>`
3. 浏览器 console 调 `SDK.fetchJSON('/api/plugins/<name>/health')` 验证后端
4. 检查 `document.querySelector('#root')` 非空（SPA 没崩）
