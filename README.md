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

### 2.2 动作知识库（RAG）
- 数据源：free-exercise-db，入库时由 fast 模型生成中文名、别名与动作要点。
- 检索链路：过滤 → 向量 + BM25 → RRF 融合 → 重排。
- 提供检索、动作详情、知识库统计接口。

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
- [ ] 渐进超负荷规则
- [ ] 停滞检测与动作替换
- [ ] `suggest_next_week` 工具

### 阶段 3：评测
- [ ] 检索评测集与脚本
- [ ] Agent 评测集与脚本
- [ ] 评测结果写入 README / 简历

### 阶段 4：可观测性与 Dashboard
- [ ] LLM 调用记录落库
- [ ] 统一 request_id 贯穿请求
- [ ] 检索与工具调用记录
- [ ] 前端 Dashboard

### 阶段 5：Docker 部署
- [ ] 后端 Dockerfile
- [ ] 前端 Dockerfile（构建后由 Nginx 提供静态文件并反向代理 API）
- [ ] docker-compose：backend + frontend + PostgreSQL（+ Milvus 可选）
- [ ] 引入 Alembic 管理表结构
- [ ] 部署到云服务器

### 阶段 6：联网搜索工具
- [ ] `web_search` 工具
- [ ] 结果安全处理
- [ ] 注入测试用例

### 阶段 7：多 Agent（视评测结果决定）
- [ ] 根据阶段 3 的评测找出单 Agent 的瓶颈
- [ ] 按需拆分子 Agent

### 贯穿各阶段：安全与测试
- [x] 越权：每个新增资源（计划、记录、统计）都写双用户互访测试（阶段 1 已覆盖）
- [ ] 提示注入：用户输入、检索文档、搜索结果三类来源各有用例
- [ ] 伤病场景：有伤病的用户不推荐对应高风险动作，回答包含就医提示
- [x] 写操作幂等：重复调用 `save_plan` / `log_workout` 不产生重复数据（阶段 1 已覆盖）
- [x] 每个阶段结束时更新 README 和测试，保持 `pytest` 全部通过（已更新至阶段 1）
