# FitnessPro

面向健身人群的 AI 私人教练。

## 1. 项目简介

FitnessPro 是一个基于大模型 Agent 的健身助手：用户通过对话描述目标、水平、伤病和训练条件，系统结合动作知识库（RAG）、长期记忆和用户档案，生成可校验的周训练计划，记录训练并给出统计反馈。


**技术栈**

| 层 | 技术 |
| --- | --- |
| 后端 | Python 3.12+、FastAPI、SQLAlchemy 2（异步）、Pydantic 2 |
| 数据库 | SQLite（默认）/ PostgreSQL（asyncpg） |
| 模型 | OpenAI 兼容接口（默认 DeepSeek；fast / normal 两档模型） |
| 检索 | bge-m3 向量 + BM25（jieba 分词）→ RRF 融合 → bge-reranker 重排 |
| 前端 | React 19、TypeScript、Vite 7、react-markdown |
| 测试 | pytest + pytest-asyncio；前端 `node --test` |

**目录结构**

```
app/
  agent/          Agent 主循环、提示词、工具（动作、记忆、档案、计划、时间）
  api/            HTTP 路由（auth / users / agent / memories / knowledge / training）
  auth/           密码哈希与 JWT
  context/        上下文构建、历史分组、滚动摘要压缩
  conversation/   会话与消息存储
  database/       ORM 模型与会话管理
  llm/            模型调用与 token 估算
  memory/         记忆提取与服务
  rag/            BM25、向量库、Embedding、重排、混合检索
  training/       计划 schema、规则校验器、器械预设、训练服务与统计
  users/          用户档案
frontend/         React 前端
scripts/          动作知识库入库、命令行对话
tests/            后端测试
config.yml        非敏感配置（模型、检索、上下文、记忆等参数）
.env              敏感信息（API Key、JWT 密钥），不入库
```

## 2. 项目功能

### 2.1 智能对话 Agent
- 工具调用循环（动作检索、动作详情、档案读写、记忆、计划、训练记录与统计、当前时间）。
- SSE 流式输出，前端打字机效果，并展示模型思考过程。
- 会话列表、历史消息查看与删除。
- 聊天输入框下方可选择 Fast / Normal 回答模型，默认 Normal，并在当前浏览器记住选择。每轮请求通过 `model_tier` 指定档位（仅允许 `fast` / `normal`，省略时为 `normal`），只影响该轮 Agent 回答及其工具调用循环。

### 2.2 动作知识库（RAG）
- 数据源：free-exercise-db，入库时由 fast 模型生成中文名、别名与动作要点。
- 检索链路：过滤 → 向量 + BM25 → RRF 融合 → 重排。
- 提供检索、动作详情、知识库统计接口。
- 回答中的知识库事实支持可点击引用：定位到动作字段或具体步骤，展示回答时的字段值/步骤原文快照；中文步骤标明是知识库中文步骤。
- 后端为本轮可见的完整字段分配证据编号，校验引用并剔除不存在或跨轮的编号。预算不足时按完整字段/步骤省略，不截断证据 JSON。
- SSE `citations` 事件返回本条回答使用的证据；历史消息接口的 `citations` 从同轮已保存工具快照恢复，无需数据库迁移。上线前已有的回答没有证据快照，不补造引用。
- 第一版校验来源编号及可见性；引用是否在语义上充分支持结论仍由提示词与忠实度评估约束，尚无逐句语义拦截。


### 2.3 上下文管理
- 按轮分组历史、token 预算、滚动摘要（乐观锁防并发覆盖）。
- 超出预算时先同步压缩再回答；回答后按阈值后台压缩。

### 2.4 记忆系统
- 自动从对话中提取候选记忆，**需用户确认后生效**；也可手动添加、编辑、拒绝、删除。
- 临时记忆带 TTL；前端提供"待确认记忆"入口。

### 2.5 用户与档案
- 注册 / 登录（JWT）。
- 档案：训练目标、水平、每周训练天数、单次时长、伤病、训练场景与可用/不可用器械。

### 2.6 周计划与训练记录
- 计划、计划日、计划动作、训练记录的数据模型；幂等保存（重复调用不产生重复数据）。
- 计划规则校验器（纯代码）：器械可用性、档案完整性、训练天数、动作是否存在、热身/放松、肌群容量、伤病高风险动作等。
- 训练场景选择、常见器械预设、不可用器械设置。
- Agent 工具：`draft_weekly_plan`、`save_plan`、`get_active_plan`、`update_plan_exercise`、`log_workout`、`get_training_stats`。
- 前端：周视图、训练记录页、训练统计（容量趋势、完成率、个人表现）。
- 所有资源按用户隔离，已有双用户互访测试。

### 2.7 API 概览
所有接口前缀为 `/api/v1`，交互式文档见 `http://127.0.0.1:8000/docs`。

| 模块 | 路径 |
| --- | --- |
| 认证 | `POST /auth/register`、`POST /auth/login` |
| 用户 | `GET /users/me`、`GET/PUT /users/me/profile` |
| Agent | `POST /agent/chat`（SSE）、`GET /agent/sessions`、`GET /agent/sessions/{id}/messages`、`DELETE /agent/sessions/{id}` |
| 记忆 | `GET/POST /memories`、`POST /memories/{id}/confirm`、`POST /memories/{id}/reject`、`PATCH/DELETE /memories/{id}` |
| 知识库 | `POST /knowledge/search`、`GET /knowledge/exercises/{id}`、`GET /knowledge/stats` |
| 计划 | `POST /plans/validate`、`GET/POST /plans`、`GET /plans/active`、`GET /plans/{id}`、`POST /plans/{id}/activate`、`PATCH /plans/{id}/exercises` |
| 训练记录 | `GET/POST /logs`、`GET /logs/{id}`、`GET /stats/weekly` |
| 健康检查 | `GET /health` |

## 3. 启动说明

### 3.1 环境要求
- Python >= 3.12
- Node.js（建议 20+）与 npm
- 可用的 LLM / Embedding / Reranker API Key（默认 DeepSeek 与 SiliconFlow）

### 3.2 后端

```bash
# 1) 创建虚拟环境并安装依赖
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2) 配置密钥
cp .env.example .env
# 编辑 .env：LLM_API_KEY、FAST_LLM_API_KEY、EMBEDDING_API_KEY、RERANKER_API_KEY、JWT_SECRET
# JWT_SECRET 生成：python -c "import secrets; print(secrets.token_urlsafe(48))"

# 3) 导入动作知识库（首次必须执行，需要联网）
python -m scripts.ingest_exercises
# 可选：先试跑 python -m scripts.ingest_exercises --limit 20
# 可选：仅英文 + BM25 python -m scripts.ingest_exercises --skip-translate --skip-embed

# 4) 启动服务（默认 http://127.0.0.1:8000）
uvicorn app.main:app --reload
```

> 更换 Embedding 模型后必须重新运行入库脚本（索引会校验模型名）。

### 3.3 前端

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173，/api 自动代理到 127.0.0.1:8000
npm run build      # 生产构建
```

### 3.4 命令行调试（可选）
无需启动 HTTP 服务：

```bash
python -m scripts.chat_cli     # 命令：/new 新会话  /profile 查看画像  /mem 查看记忆  /quit 退出
```

### 3.5 配置说明
- [config.yml](config.yml)：时区、CORS、数据库、模型、检索、上下文、记忆、Agent 超时等非敏感参数。
- 切换 PostgreSQL：设置环境变量 `DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/fitnesspro`。
- 本地 Embedding：将 `embedding.provider` 设为 `local`，并安装 `sentence-transformers`。

### 3.6 测试

```bash
pytest                      # 后端（项目根目录）
cd frontend && npm test     # 前端
```

## 4. TodoList
后台任务为进程内任务，进程重启后未完成的摘要和记忆提取会丢失；需要时改为持久化任务队列。
会话锁为进程内锁，多进程部署需要换成分布式锁。
token 为估算值；表结构由 create_all 创建，正式环境应引入 Alembic。

### 基础能力
- [x] RAG 搭建，第一版知识库入库成功（过滤 → 向量 + BM25 → RRF → 重排）
- [x] 上下文机制搭建（按轮分组、token 预算、滚动摘要 + 乐观锁）
- [x] 前端可视化（SSE 流式输出，打字机效果已具备）
- [x] 记忆系统（自动提取为候选，需用户确认后生效；可手动添加）
- [x] API（auth / profile / memories / agent / knowledge）

### 阶段 0：快速改进
- [x] 展示模型思考过程（"边想边生成"）
- [x] 前端增加"待确认记忆"入口

### 阶段 1：周计划与训练记录
- [x] 计划、计划日、计划动作、训练记录的数据表
- [x] 计划规则校验器（纯代码）
- [x] 训练场景选择、常见器械预设与不可用器械设置
- [x] 计划相关 Agent 工具
- [x] 训练记录工具与统计
- [x] 前端：周视图 + 记录页
- [x] 前端：训练统计与容量趋势、完成率、个人表现
- [x] 测试

### 阶段 2：根据记录自动调整计划
- [x] 渐进超负荷规则
- [x] 停滞检测与动作替换
- [x] `suggest_next_week` 工具

### 阶段 3：评测
- [x] 检索评测集与脚本
- [x] Agent 评测集与脚本
- [ ] 评测结果写入 README

### 阶段 4：可观测性与 Dashboard
- [x] LLM 调用记录落库（Agent / 摘要 / 记忆 / 翻译 / RAGAS）
- [x] 统一 request_id 贯穿请求与后台任务
- [x] 检索与工具调用记录（含失败、中断与降级）
- [x] 前端 Dashborad：运行监控 / RAGAS 评估双页签

### 阶段 5：添加高质量资料，增强理论性支撑
> 方案结论：用普通 RAG，不引入 GraphRAG（单点事实查找为主，GraphRAG 建库成本高、收益小；文献到数百篇且需要跨文献综合推理时再评估）。
- [ ] 独立文献知识库：与动作库逻辑分离（独立索引目录、分块规则、工具入口），复用现有 BM25 / 向量 / RRF / reranker 代码
- [ ] 资料筛选标准：优先系统综述、荟萃分析与权威指南（如 ACSM 立场声明），避免小样本研究与营销类科普；结论冲突的资料需人工甄别
- [ ] 文献入库脚本：按章节与小标题做语义分块（约 300～600 字，相邻块少量重叠），保留研究对象等限定条件
- [ ] 块级元数据：标题、作者、年份、章节、来源链接
- [ ] `search_knowledge_articles` 工具：制定计划或回答原理类问题时调用，返回带出处的片段
- [ ] 评估对接：文献检索工具和检索动作一样，复用 ragas
- [ ] 评测与测试：补充文献检索评测集与工具、入库的单元测试

### 阶段 6：联网搜索工具
- [x] `web_search` 工具（deepseek_research / DeepSeek Search，复用现有 Agent 循环）
- [x] 结果安全处理与前端来源展示
- [x] 注入测试用例（强制模型尝试调用 forget_memory，服务端拦截）

启用方式：在 `.env` 配置 DeepSeek 官方 `LLM_API_KEY`（默认复用模型 Key），在 `config.yml` 设置 `web_search.enabled: true`，重启后端。
默认禁用；缺少 key、超时、上游错误、空结果都会明确反馈，不伪装搜索成功。搜索 API 按请求计费，未计入 Dashboard 的 token 估算费用。若模型 Key 来自第三方代理，请为搜索配置独立的 DeepSeek 官方 Key，并通过 `web_search.api_key_env` 指定其环境变量名。

动作推荐/计划仍优先使用知识库；最新研究、赛事和器材价格等库外信息才使用 `web_search`。
最终回答附来源链接；执行轨迹中展示标题、URL 和摘要。外部信息不生成知识库字段级证据编号，
也不将网页摘要当作已阅读原文或医学结论。

搜索固定请求 `POST https://api.deepseek.com/v1/tools/search`，请求体为 `query`、`count`、`need_content: false`。
搜索词限 128 个 Unicode 字符；将响应 `summary`（缺失时用 `snippet`）映射为工具摘要，不使用正文 `content`。
保留 Agent 工具名 `web_search`，返回的 provider 为 `deepseek_research`。
禁止跳转和完整 URL 搜索，不调用 Fetch，不抓取结果页或用户提供的 URL。
接口文档：https://api-docs.deepseek.com/zh-cn/guides/search
最多返回 5 项，标题/摘要/URL 分别限 120/500/2048 字符，过滤私网 IP、本地地址、非 HTTP(S) 和带凭据的链接。
结果使用 `untrusted_content.sources` 包装；前端以文本渲染并对外链协议再次校验。
搜索关键词不得包含画像、记忆、伤病详情、身份或密钥；不自动附加会话上下文。

为阻止网页提示注入触发写操作，任何包含 `web_search` 的工具批次在执行前将本轮切换为只读，
包括同批次排在搜索前的写操作；后续轮次的记忆、画像、计划和训练记录写工具也拒绝执行。
若用户同时需要修改，请下一轮单独提出；服务端不把搜索结果中的“用户已授权”当作授权。
该防护确保写操作不执行，但不保证模型永远不会尝试调用；提示词同时要求忽略外部指令。
搜索工具调用沿用阶段 4 的 request_id 和工具日志；当前 RAGAS 仍按知识库证据评分，不评判网页事实。

### 阶段 7：多 Agent（视评测结果决定）
- [ ] 根据阶段 3 的评测找出单 Agent 的瓶颈
- [ ] 按需拆分子 Agent

### 阶段 8：Docker 部署
- [ ] 后端 Dockerfile
- [ ] 前端 Dockerfile（构建后由 Nginx 提供静态文件并反向代理 API）
- [ ] docker-compose：backend + frontend + PostgreSQL（+ Milvus 可选）
- [ ] 引入 Alembic 管理表结构
- [ ] 部署到云服务器

### 贯穿各阶段：安全与测试
- [x] 越权：每个新增资源（计划、记录、统计）都写双用户互访测试（阶段 1 已覆盖）
- [ ] 提示注入：用户输入、检索文档、搜索结果三类来源各有用例
- [ ] 伤病场景：有伤病的用户不推荐对应高风险动作，回答包含就医提示
- [x] 写操作幂等：重复调用 `save_plan` / `log_workout` 不产生重复数据（阶段 1 已覆盖）
- [x] 每个阶段结束时更新 README 和测试，保持 `pytest` 全部通过（已更新至阶段 1）

### 会话工具选择

聊天输入框底部的“工具”面板按动作知识库、训练计划与记录、档案与记忆、实时信息分组，支持单项勾选、全选可用、全部取消和恢复默认。选择按账号与会话保存在当前浏览器，新会话默认使用默认工具；登录后可操作，生成回答时锁定。MCP 与 Agents 分区目前展示未接入状态。

`GET /api/v1/agent/tools` 返回需登录访问的工具目录和可用状态。`POST /api/v1/agent/chat` 支持 `selected_tools`：省略或 `null` 使用默认工具，`[]` 不允许调用任何工具，名称数组仅允许指定工具；未知名称返回 422。每轮创建独立执行器，模型 schema 和实际执行权限使用同一份筛选结果，未选中的工具无法执行。该设置限制模型工具调用，不清除已有上下文，也不更改后台自动记忆提取配置。
