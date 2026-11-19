<div align="center">

# FitnessPro

**AI 私人教练与训练管理助手**

基于 LLM、RAG 与工具调用，将训练目标转化为可执行、可记录、可调整的训练计划。

[快速开始](#快速开始) · [配置说明](#配置说明) · [Docker 部署文档](deploy/README.md)

</div>

---

## 项目简介

FitnessPro 是面向健身人群的 AI 训练管理平台。用户可以通过对话描述目标、训练经验、时间安排和器械条件，系统结合用户档案、动作知识库与训练文献，辅助制定周计划、记录训练、查看统计，并根据实际表现提出下一周的调整建议。

- **智能问答**：查询动作步骤、目标肌群和训练原理，查看回答引用的知识库来源。
- **个性化计划**：结合目标、训练水平、伤病及可用器械生成周计划，并通过代码规则校验。
- **训练记录**：保存实际完成的组数、次数和负重，查看完成率、训练容量及个人表现。
- **渐进调整**：根据历史记录提出加重、减重或动作替换建议，确认后生成新版本计划。
- **器械识别**：上传器械照片，识别候选并确认，再查询适用的训练动作。
- **长期记忆**：提取训练偏好与限制，经用户确认后用于后续对话。

适用于希望获得训练规划辅助、管理日常训练记录和持续追踪进展的用户。当前图片功能面向健身器械识别，不提供体态诊断或疾病判断。

## 技术亮点

- **双知识库混合检索**：动作库与文献库独立索引，使用 Qdrant 向量检索、BM25、RRF 融合与 Reranker 重排序。
- **可追溯的回答引用**：为本轮可见证据分配编号，保存字段与片段快照，支持当前回答及历史消息中的来源查看。
- **Agent 工具编排**：通过工具调用完成检索、档案管理、计划草拟、训练记录与统计，前端可选择本轮允许使用的工具。
- **训练 Skill 渐进式披露**：常驻技能元信息，规划相关任务按需加载完整规则，覆盖训练分化、重量日、容量日与渐进超负荷。
- **规则校验与计划版本化**：用代码检查计划约束；调整建议经确认后保存为新版本，保留历史计划与训练记录。
- **独立视觉模型**：图片识别与文本对话分别配置模型，当前视觉模型使用 DeepSeek Flash 并复用快速模型密钥。
- **执行追踪与质量评估**：记录 LLM、检索与工具调用，提供运行监控、离线评测与可选的 RAGAS 在线评估。
- **容器化部署**：Docker Compose 编排前后端、PostgreSQL、Qdrant、数据库迁移与首次入库任务。

| 层级 | 技术 |
| --- | --- |
| 后端 | Python 3.12+、FastAPI、Pydantic 2 |
| 数据持久化 | SQLAlchemy 2 异步 ORM、Alembic、PostgreSQL / SQLite |
| 模型接入 | OpenAI 兼容接口，独立配置 fast / normal / vision |
| 知识检索 | Qdrant、BAAI/bge-m3、BM25、jieba、RRF、bge-reranker |
| 前端 | React 19、TypeScript、Vite 7、Recharts、react-markdown |
| 部署与测试 | Docker Compose、Nginx、pytest、Node.js Test Runner |

## 核心功能

### 1. Agent 智能训练助手

- 支持多轮对话、会话列表、历史消息回看与会话删除。
- 通过 SSE 流式输出回答，展示工具调用过程与结果。
- 输入区可选择 Fast / Normal 模型档位及本轮工具集。
- 执行面板可显示或隐藏模型阶段与耗时，用于区分等待响应、模型推理、工具参数生成和工具执行等阶段；不展示原始思考文本。
- 点击取消会向后端提交终止请求，取消正在执行的轮次；已提交的数据不会自动回滚。
- 内置动作检索、文献检索、档案与记忆管理、计划与训练管理、器械识别、时间查询和联网搜索工具。

### 2. 周计划与训练记录

- 档案包含目标、训练水平、每周训练天数、单次时长、伤病、训练场景及可用或不可用器械。
- 周计划以训练日和动作为单位管理，支持草拟、校验、保存、激活及动作修改。
- 代码校验器检查档案完整性、训练天数、动作有效性、器械限制、热身与放松、肌群容量及部分伤病风险。
- 支持记录实际训练，并以周视图、训练记录页和统计页展示进展。
- 计划保存和训练记录写入支持幂等处理，避免重复调用产生重复数据。

### 3. 训练分析与渐进调整

- 查看训练容量趋势、完成率与个人表现。
- 连续两次在不同训练日期完成同一处方全部组数并达到次数上限时，按规则提出上肢加 2.5 kg、下肢加 5 kg 的建议；徒手或未设置负重时调整次数区间。
- 连续两次未达到最低次数目标，且有明确目标重量时，建议减重 7.5%；缺组或未知重量不直接判定为失败。
- 对满足条件的连续三周停滞情况，检索同主要肌群、同器械的替换动作并重新校验。
- 用户可预览理由、选择候选并确认保存下周草稿；激活新版后归档旧版，保留历史记录。

### 4. 动作与文献知识库

- 动作资料来自 [free-exercise-db](https://github.com/yuhonas/free-exercise-db)，入库时可生成中文名、别名和动作要点。
- 文献库收录训练相关综述、荟萃分析等资料的核心观点摘录，与动作库分开管理。
- 支持自然语言查询，并按肌群、器械等元数据筛选动作。
- 回答引用可定位动作字段、具体步骤或文献片段，显示回答时保存的原文快照。
- 联网搜索用于补充知识库缺失信息；文献来源和筛选规则见 [文献库规范](app/knowledge/README.md)。

### 5. 混合检索与重排序

检索流程为：元数据过滤 → 向量检索与 BM25 召回 → RRF 融合 → Reranker 重排。

- **语义召回**：使用 bge-m3 向量与 Qdrant 匹配语义相关内容。
- **关键词召回**：使用 jieba 分词与 BM25 补充动作名称、器械等精确匹配。
- **重排序**：使用 bge-reranker 对融合后的候选进一步排序。
- **降级处理**：向量检索或重排不可用时提供降级结果与提示。
- **查询缓存**：动作检索器使用进程内 LRU 缓存查询向量；当前未引入 Redis。
- **索引发布**：动作与文献分别使用 Qdrant 集合和别名，完整写入并核对后切换版本。

### 6. 器械图片识别

- 每轮支持一张 JPEG、PNG 或 WebP 图片，默认最大 8 MiB、2000 万像素。
- 上传后纠正图片方向、压缩并移除 EXIF，长边最多保留 1600 像素。
- 视觉模型返回器械候选、可见特征和补拍建议；用户确认后，再从动作库查询适用动作。
- 输入区使用固定 88px 缩略图与悬浮删除按钮，历史消息支持图片回看和删除。
- 图片读写按用户鉴权，保存于本地文件目录，Docker 部署复用持久化卷。
- 图片识别轮次禁止写入档案、计划等业务数据，并跳过自动个人记忆提取；图片会发送到配置的模型服务商。

### 7. 个性化上下文与训练 Skill

- 按轮组织对话历史，结合 Token 预算进行滚动摘要，保留近期对话。
- 自动提取候选记忆，用户确认后生效；支持手动添加、编辑、拒绝、删除和临时记忆过期。
- 训练规则位于 [training-plan-design/SKILL.md](app/agent/skills/training-plan-design/SKILL.md)。
- 平时仅注入技能名称与描述；Agent 识别到计划制定、评估或调整需求后，通过只读工具加载完整正文。
- 技能正文仅用于当前轮上下文，后续规划按需重新加载；技能规则与文献检索、代码校验共同约束计划生成。

### 8. 评估与运行监控

- 离线检索评测支持 BM25、仅向量、混合检索和混合加重排对比，统计 Recall、MRR、nDCG 与延迟。
- Agent 评测在隔离的临时数据库中运行用例，检查工具调用与任务行为。
- 可选 RAGAS 在线评估在后台执行，当前支持忠实度与答案相关性，默认关闭。
- 监控页展示模型调用、Token 用量、耗时、检索与工具执行记录，并通过 request_id 关联请求。
- 管理员由服务端配置；费用估算需填写实际模型单价，未配置的模型标记为未定价。
- 评测方法见 [评测说明](eval/PROTOCOL.md)。当前不在 README 中声称未经复核的质量提升指标。

### 9. 账户与数据隔离

- 注册与登录使用密码哈希和 JWT 鉴权。
- 会话、档案、记忆、计划、训练记录及图片按用户隔离。
- API Key、JWT 密钥和数据库密码通过环境变量管理。
- Docker 默认仅发布前端 HTTP 端口，PostgreSQL 与 Qdrant 位于内部网络。
- 数据库结构由 Alembic 管理；已有无版本数据库通过显式检查与接管流程迁移。

## 快速开始

### 前置要求

- 推荐使用 Docker Engine / Docker Desktop 和 Docker Compose v2.20+。
- 本地开发需要 Python 3.12+、Node.js 22 与 npm。
- 准备可用的 DeepSeek 模型 API Key，以及 Embedding / Reranker API Key；默认后两者使用 SiliconFlow。

### 方法一：Docker 部署（推荐）

**1. 获取项目**

```bash
git clone https://github.com/Nismozzz/FitnessPro.git
cd FitnessPro
```

**2. 配置环境变量**

首次部署时复制配置；已有 `.env` 时保留原文件。

```bash
cp .env.example .env
python -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_hex(32)); print('JWT_SECRET=' + secrets.token_urlsafe(48))"
```

把生成的两个值填入 `.env`，填写模型密钥，并核对 `config.yml` 中的模型、接口地址与功能开关。

**3. 构建并启动**

```bash
docker compose config --quiet
docker compose up -d --build
docker compose logs -f migrate ingest backend
```

Compose 会启动 PostgreSQL、Qdrant、数据库迁移任务、首次入库任务、FastAPI 后端及 Nginx 前端。首次下载动作、翻译和生成向量可能耗时较长并产生 API 费用，已有完整数据时跳过相应入库步骤。`migrate` 与 `ingest` 完成后显示 `Exited (0)` 为正常状态。

**4. 访问应用**

```bash
docker compose ps -a
curl --fail http://localhost/health
```

访问 `http://localhost`，注册账号、填写训练档案后开始对话或创建计划。端口被占用时，在 `.env` 设置 `HTTP_PORT=8080`，改为访问 `http://localhost:8080`。

部署细节、卷持久化、备份、旧库接管与 Qdrant 迁移见 [Docker 部署说明](deploy/README.md)。

### 方法二：本地开发

**1. 安装后端依赖并配置密钥**

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

填写 `.env` 中的模型密钥与 `JWT_SECRET`。默认业务数据库为 SQLite。当前 `config.yml` 的 Qdrant 主机名用于 Docker 网络；本地开发可将 `vector_store.type` 改为 `local`，使用本地向量索引，或将 `vector_store.url` 改为本机可访问的 Qdrant 地址。

**2. 初始化知识库并启动后端**

```bash
python -m scripts.ingest_exercises
python -m scripts.ingest_knowledge
uvicorn app.main:app --reload --workers 1
```

首次导入需要联网。可先使用 `python -m scripts.ingest_exercises --limit 20` 试跑。更换 Embedding 模型后需重新生成索引。

**3. 在另一个终端启动前端**

```bash
cd frontend
npm ci
npm run dev
```

| 入口 | 本地开发地址 |
| --- | --- |
| 前端 | http://localhost:5173 |
| 后端 | http://127.0.0.1:8000 |
| API 文档 | http://127.0.0.1:8000/docs |
| 健康检查 | http://127.0.0.1:8000/health |

前端开发服务器会将 `/api` 请求代理到后端。业务 API 统一使用 `/api/v1` 前缀；Docker 默认不直接发布后端端口。

### 测试与调试

```bash
# 项目根目录：后端测试
pytest

# 前端测试与生产构建
cd frontend
npm test
npm run build
```

也可在项目根目录执行 `python -m scripts.chat_cli`，通过命令行调试对话，无需启动 HTTP 服务。

## 配置说明

### 1. 环境变量（.env）

配置模板见 [.env.example](.env.example)。

| 变量 | 用途 |
| --- | --- |
| `LLM_API_KEY` | Normal 模型密钥 |
| `FAST_LLM_API_KEY` | Fast 模型密钥，当前视觉模型复用此值 |
| `EMBEDDING_API_KEY` | Embedding 服务密钥 |
| `RERANKER_API_KEY` | 重排序服务密钥 |
| `JWT_SECRET` | 登录令牌签名密钥 |
| `POSTGRES_PASSWORD` | Docker 首次初始化 PostgreSQL 的密码 |
| `HTTP_PORT` | 可选，Docker 前端端口，默认 80 |
| `DATABASE_URL` | 可选，覆盖本地配置的业务数据库连接；Compose 已指定内部 PostgreSQL 连接 |
| `DEEPSEEK_SEARCH_API_KEY` | 当前配置的联网搜索密钥，可填写同一 DeepSeek Key；或将 `web_search.api_key_env` 改为 `LLM_API_KEY` 直接复用 |

`.env` 不应提交到版本库。修改环境变量后需重建容器实例以加载新值；修改现有 PostgreSQL 卷对应的 `.env` 密码不会自动修改数据库角色密码。

### 2. 主配置文件（config.yml）

[config.yml](config.yml) 保存模型、检索、上下文、图片、评估与超时等非敏感参数。

| 配置项 | 当前用途与默认设置 |
| --- | --- |
| `llm.fast` | `deepseek-flash`，关闭思考，用于快速回答及摘要、记忆提取、翻译等辅助任务 |
| `llm.normal` | `deepseek-v4-pro`，开启思考，`reasoning_effort: low` |
| `llm.vision` | `deepseek-flash`，关闭思考，复用 `FAST_LLM_API_KEY` |
| `vision` | 器械图片开关、存储路径、大小、像素与缩图限制，当前启用 |
| `vector_store` | 默认 Qdrant，Docker 地址为 `http://qdrant:6333`，支持切换为 `local` |
| `embedding` / `reranker` | 默认 SiliconFlow 的 bge-m3 / bge-reranker-v2-m3 |
| `retrieval` | 向量与关键词召回数量、RRF 参数及返回数量 |
| `context` | 历史 Token 预算、压缩阈值、近期轮数及工具结果预算 |
| `memory` | 自动候选提取、临时记忆 TTL 与上下文注入数量 |
| `agent` | 最大迭代次数、工具超时、整轮超时与最终回答预留时间 |
| `knowledge` | 文献原始资料、索引路径及分块大小 |
| `web_search` | DeepSeek 联网搜索开关、密钥环境变量名与超时 |
| `evaluation` | 可选 RAGAS 在线评估，默认关闭 |
| `observability` | 管理员用户 ID、货币、模型单价与离线报告路径 |

模型服务需支持对应能力：文本 Agent 使用工具调用，视觉接口需要支持图片输入及 JSON 输出。各模型的 `extra_body` 控制思考等供应商扩展参数，切换服务商时需一并核对。

Docker 将 `config.yml` 只读挂载到后端。修改该文件后执行 `docker compose restart backend`；修改业务代码或前端代码后，需重新构建镜像并更新容器，Git 提交与推送不会自动部署。

### 3. 项目结构与数据存储

```text
app/
  agent/           Agent 循环、工具、引用与训练 Skill
  api/             HTTP 路由
  auth/            密码哈希与 JWT 鉴权
  context/         历史分组、上下文预算与摘要压缩
  conversation/    会话与消息存储
  database/        ORM 模型与数据库会话
  evaluation/      在线 RAGAS 评估
  knowledge/       文献分块、索引与检索
  llm/             模型接口与 Token 估算
  memory/          候选记忆提取与管理
  observability/   请求追踪、调用记录与统计
  rag/             动作混合检索与 Qdrant 存储
  training/        计划校验、训练记录、统计与渐进调整
  users/           用户档案
  vision/          图片处理与器械识别
frontend/          React 页面与组件测试
migrations/        Alembic 数据库迁移
scripts/           入库、评测、命令行调试与部署初始化
tests/             后端测试
eval/              评测用例与评分说明
deploy/            Docker 部署说明
compose.yaml       服务编排
config.yml         非敏感配置
```

Docker 使用 `postgres_data` 保存业务数据、`qdrant_data` 保存向量数据、`app_data` 保存原始资料、索引文件及器械图片。停止或重建容器会保留命名卷；`docker compose down -v` 会删除这些卷。

## 未来规划

- [x] 智能对话、工具调用与会话管理
- [x] 动作及文献双知识库、混合检索与来源引用
- [x] 用户档案、候选记忆确认与上下文压缩
- [x] 周计划、规则校验、训练记录与统计
- [x] 基于记录的下周调整建议与计划版本管理
- [x] 训练 Skill 的按需加载
- [x] 器械图片识别与确认后的动作推荐
- [x] 任务取消、模型阶段与耗时展示
- [x] 运行监控、离线评测与可选在线评估
- [x] Docker Compose 部署、Alembic 迁移与 Qdrant 持久化
- [ ] 复核评测标签，补充可复现的质量与延迟对比结果
- [ ] 扩充真实健身房图片测试集，评估相似器械、遮挡和模糊场景
- [ ] 增加未引用图片的过期清理与保留策略
- [ ] 完善提示注入、伤病约束等场景的测试覆盖
- [ ] 按部署需求引入持久化后台任务与跨进程会话协调
- [ ] 根据评测确认单 Agent 瓶颈，再决定是否拆分子 Agent

当前会话锁、取消任务注册表及后台任务均位于进程内，部署保持单 worker；进程重启后未完成的摘要、记忆提取等后台任务不会自动恢复。
