# FitnessPro

面向健身人群的 AI 教练 Agent：推荐训练动作、规划每周训练。动作知识库来自 [free-exercise-db](https://github.com/yuhonas/free-exercise-db)。

当前阶段完成了：RAG 检索、上下文管理、记忆系统、Agent 循环，以及对应的 HTTP API 和 React 前端。尚未做 Docker 封装。

## 快速开始

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # 填写 LLM_API_KEY / FAST_LLM_API_KEY / EMBEDDING_API_KEY / JWT_SECRET

# 1. 入库（先试跑 20 条，确认翻译和 Embedding 正常后再跑全量）
python -m scripts.ingest_exercises --limit 20
python -m scripts.ingest_exercises
#    没有 Embedding key 时可只用 BM25：--skip-embed；不翻译：--skip-translate

# 2. 命令行对话（不需要启动服务）
python -m scripts.chat_cli

# 3. 启动 API（文档：http://localhost:8000/docs）
uvicorn app.main:app --reload

# 测试（不需要任何 API key）
pytest
```

## 前端

前端位于 `frontend/`，使用 React + TypeScript + Vite，布局参考本地 CookHero：侧栏会话、欢迎卡片、底部输入区，支持浅色 / 深色主题和移动端。

```bash
# 终端一：启动后端
uvicorn app.main:app --reload

# 终端二：启动前端（Node.js 20.19+ 或 22.12+）
cd frontend
npm ci
npm run dev
```

打开 Vite 输出的本地地址（默认 http://localhost:5173）。开发服务器将 `/api` 代理到 `http://127.0.0.1:8000`，无需额外修改 CORS。

- 注册 / 登录后可以使用 SSE 流式教练对话、历史会话查看与删除。
- 动作库支持关键词、肌群、器械筛选，以及动作步骤和示意图详情；需要先完成知识库入库。
- 训练档案支持目标、水平、身体数据、器械、伤病和备注；侧栏“我的记忆”按“已确认 / 待确认”分栏，支持添加、确认、忽略、编辑与删除，并展示待确认数量。
- 模型返回 `reasoning_content` 时，通过 `thinking` SSE 事件实时展示在默认折叠的“思考过程”区域；可手动展开，正文开始输出时自动折叠。思考内容只保留在当前页面，不落库、不进入跨轮历史，刷新或重新打开会话后不会恢复；未返回思考内容的模型不会显示此区域。
- 收到对话 `done` 后延迟 3 秒刷新一次记忆。后台提取耗时更长时，可在“我的记忆”中手动刷新；登出或切换账号会清除待执行刷新并丢弃旧账号的延迟响应。
- 首页和主题切换无需登录。对话需要有效的模型配置；前端不会生成模拟回答。
- “停止接收”仅断开前端连接，后端仍可能完成任务；稍后重新打开会话可查看保存结果。

阶段 0 验证：`pytest`（后端）、`cd frontend && npm test`（SSE 与 DOM 交互测试）、`npm run build`（类型检查与构建）。自动化验收使用伪造模型和真实临时 SQLite，覆盖思考不落库、候选记忆提取 → 编辑 → 确认 → 下一轮提示词生效，以及双用户越权保护；不会调用收费模型。真实模型手动验收：开启支持思考输出的模型，发送“我左膝有旧伤”，展开思考区域观察增量，等待回答结束后进入“我的记忆 → 待确认”，确认候选后继续询问训练建议。后台提取失败或没有提取到事实时不会创建候选。

生产构建：`cd frontend && npm run build`，产物为 `frontend/dist/`。部署时用 Web 服务器托管该目录，并将 `/api/` 反向代理到后端。`npm run preview` 仅用于静态产物预览，需要另行配置 API 代理。

## 目录结构

```text
app/
├── config.py              # config.yml + .env
├── main.py                # FastAPI 入口
├── database/              # SQLAlchemy 模型（默认 SQLite，可切换 PostgreSQL）
├── llm/                   # OpenAI 兼容客户端（fast / normal 两档）、token 估算
├── rag/                   # 动作知识库
│   ├── schema.py          #   枚举（中文同义词）、ExerciseDoc、ExerciseFilter
│   ├── bm25.py            #   jieba + BM25
│   ├── vector_store.py    #   本地 numpy 向量库（npy + json，无 pickle）
│   ├── embeddings.py      #   API / 本地 Embedding
│   ├── reranker.py        #   可选 /rerank 接口
│   └── retriever.py       #   过滤 → 向量 + BM25 → RRF → 重排
├── context/               # 上下文：按轮分组、预算选择、滚动摘要
├── memory/                # 长期记忆：候选 → 确认，自动提取
├── users/profile.py       # 结构化用户画像
├── conversation/          # 会话与消息存储
├── agent/
│   ├── loop.py            #   ReAct 循环（流式、统一终止协议）
│   ├── service.py         #   会话锁、后台执行、持久化、后台任务调度
│   ├── prompts.py         #   系统提示词
│   └── tools/             #   工具协议 + 8 个工具
└── api/                   # auth / users / memories / agent(SSE) / knowledge
scripts/
├── ingest_exercises.py    # 下载 → 翻译（缓存可续跑）→ 入库 → 建索引
└── chat_cli.py            # 命令行调试
```

## 设计要点

### RAG
- 一条动作 = 一个文档（中位数约 100 词），无需切块。
- **先结构化过滤，再语义排序**：肌群、器械、难度等由 Agent 通过工具参数（枚举值，支持中文同义词归一化）给出，
  检索只在候选集合内进行，不需要再调用一次 LLM 抽取过滤条件。
- 稠密向量（bge-m3，中英双语）+ BM25（jieba）→ RRF 融合 → 可选重排。
- 入库时用 LLM 生成中文名、别名和中文步骤，检索文本中英双语并带肌群同义词。
- 降级：向量检索或重排失败时退回 BM25，并在结果的 warnings 中说明；索引会校验 Embedding 模型名。

### 上下文
- 每次请求从数据库重建：系统提示词（规则 + 时间 + 画像 + 记忆 + 摘要）+ 近期原文 + 当前消息。
- 以**轮**为单位处理历史，工具调用与结果不会被拆开；摘要覆盖 `seq < summary_upto_seq`，其余全部以原文进入，没有覆盖缺口。
- 超过 `history_token_budget` 时先同步压缩；压缩失败则丢弃最早的轮次并**显式告知模型**。
- 回答后超过 `compress_trigger_tokens` 时后台压缩；同会话压缩串行执行，写回使用版本号乐观锁。
- 历史工具结果和当前轮工具结果分别按预算截断；中断留下的不完整工具调用会被自动修复。

### 记忆
| 层次 | 存储 | 更新方式 |
|---|---|---|
| 工作记忆 | 本次执行的 messages | 工具循环追加 |
| 会话原文 | `messages` | 每轮保存 |
| 会话摘要 | `agent_sessions.summary` | 滚动压缩 |
| 用户画像 | `user_profiles` | API / `update_user_profile` 工具 |
| 长期事实 | `memories` | 见下 |

长期事实带类型、来源、状态、有效期：
- 自动提取只生成 `candidate`，以“待确认”区块进入提示词，用户确认后才变为 `active`；
- 新事实否定旧事实时生成 `supersedes` 候选，确认后旧记忆归档；
- 区分 `long_term` 与 `temporary`（默认 7 天过期）；
- 用户明确要求“记住”时由 `save_memory` 直接写入 `active`；可通过 API 查看、编辑、删除。

### Agent
- 工具参数用 Pydantic 定义，同时生成 JSON Schema 和服务端校验；`user_id` 只来自认证上下文，不出现在模型参数中。
- JSON 解析失败或校验失败时把错误反馈给模型，不带空参数执行；每个工具有超时，内部异常不泄露细节。
- 统一终止协议：每次执行以且仅以一个 `done` 结束，`status` 为 completed / max_iterations / timeout / error。
- 最后一轮强制 `tool_choice="none"` 让模型给出回答；除轮数外还有总时长预算。
- Agent 在后台任务中运行并通过队列推送 SSE，客户端断开后仍会完成并保存；同会话请求串行。
- 所有会话、记忆接口都按 user_id 校验归属。

## 工具列表

| 工具 | 说明 | 副作用 |
|---|---|---|
| `search_exercises` | 混合检索 + 结构化筛选 | 否 |
| `get_exercise_detail` | 动作步骤、示意图 | 否 |
| `get_user_profile` / `update_user_profile` | 读写画像 | 写 |
| `save_memory` / `confirm_memory` / `forget_memory` | 长期记忆 | 写 |
| `get_current_time` | 当前日期与本周日期 | 否 |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/register`、`/login` | 获取 JWT |
| GET/PUT | `/api/v1/users/me/profile` | 画像 |
| GET/POST/PATCH/DELETE | `/api/v1/memories`、`/{id}/confirm`、`/{id}/reject` | 记忆管理 |
| POST | `/api/v1/agent/chat` | SSE 对话：session / context / thinking / content / tool_call / tool_result / error / done |
| GET/DELETE | `/api/v1/agent/sessions`、`/{id}/messages` | 会话 |
| POST | `/api/v1/knowledge/search` | 检索调试（返回两路名次） |

## 已知限制 / 后续计划

- 后台任务为进程内任务，进程重启后未完成的摘要和记忆提取会丢失；需要时改为持久化任务队列。
- 会话锁为进程内锁，多进程部署需要换成分布式锁。
- token 为估算值；表结构由 `create_all` 创建，正式环境应引入 Alembic。
- 下一步：训练计划工具（生成 + 规则校验）、训练记录与统计、检索评测集、Docker 化。
