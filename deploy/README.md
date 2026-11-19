# Docker 部署

## 首次启动

需要 Docker Engine / Desktop 和 Docker Compose v2.20+。在项目根目录执行：

```bash
cp .env.example .env  # 仅首次；已有 .env 不要覆盖
python -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_hex(32)); print('JWT_SECRET=' + secrets.token_urlsafe(48))"
```

把生成的两个值填入 `.env`，再填写 `LLM_API_KEY`、`FAST_LLM_API_KEY`、`EMBEDDING_API_KEY`、`RERANKER_API_KEY`。模型名称与 API 地址在 `config.yml` 中调整。数据库密码使用十六进制，避免连接 URL 转义问题；没有内置默认密码。`.env` 不进入构建上下文，运行时只注入后端和一次性任务。

```bash
docker compose config --quiet
docker compose up -d --build
docker compose logs -f migrate ingest backend
docker compose ps -a
curl --fail http://localhost/health
```

访问 `http://localhost` 注册、填写画像，再生成计划。默认只发布前端 80 端口；本机端口被占用时在 `.env` 设置 `HTTP_PORT=8080`，访问 `http://localhost:8080`。

启动依赖顺序：PostgreSQL 健康 → `migrate` 执行 `alembic upgrade head` → `ingest` 一次性入库 → 后端健康 → Nginx。`migrate`、`ingest` 成功后显示 `Exited (0)` 是正常现象。第一次下载动作数据、翻译与生成向量可能耗时较长，并产生模型 API 费用；不会把它放进镜像构建或每个 Web worker 中执行。后续启动已有数据时跳过入库；上次向量生成失败时补建索引。

`config.yml` 以只读方式挂载。修改后执行 `docker compose restart backend`；修改 `.env` 后执行 `docker compose up -d --force-recreate`。请保留 `--workers 1`：会话锁和取消任务注册表仍在进程内。

## 数据与索引

- `postgres_data` 命名卷保存账号、对话、档案、记忆、计划、训练记录和监控记录。
- `app_data` 命名卷保存原始动作、翻译缓存、动作向量与文献索引；首次从镜像内的文献 Markdown 初始化原始资料。
- 本地开发的 SQLite 和 `data/index` 不会自动迁入 PostgreSQL，也不会被打进镜像；本次为新部署建库，不迁移已有个人数据。
- 向量检索使用 Qdrant（`qdrant/qdrant:v1.13.6`），独立的 `qdrant_data` 卷保存向量。服务仅在 Compose 网络内开放，不映射宿主机 6333/6334 端口。
- 动作/文献分别使用 `fitnesspro_exercises` / `fitnesspro_articles` 别名，发布到按内容哈希命名的集合；完整写入并核对数量后原子切换别名。运行中的检索器绑定不可变集合版本，与自己的动作/文献快照一致。旧集合不自动删除。
- `app_data` 中的 NumPy 向量文件作为离线导入源保留；检索通过 Qdrant REST 查询，不再在应用里对 NumPy 矩阵执行相似度排序。BM25、业务原文、翻译缓存仍保留在原处。
- Qdrant 不可用时检索降级关键词；初始化任务会报错并阻止启动，避免迁移失败后被当作成功部署。

停止及重建容器保留命名卷：

```bash
docker compose down
docker compose up -d
```

不要在需要保留数据时运行 `docker compose down -v`，该参数会删除卷。PostgreSQL、`app_data` 和 `qdrant_data` 应成套备份；升级前备份数据库：

```bash
docker compose exec -T postgres pg_dump -U fitnesspro -d fitnesspro -Fc > fitnesspro.dump
```

恢复时先停止写入，再向空数据库导入备份；数据库与向量库版本应匹配。数据库密码只对首次初始化卷生效，修改 `.env` 不会修改现有 PostgreSQL 角色密码。

重新入库需安排维护窗口，避免在运行中切换数据库/索引；执行完成后重启后端加载：

```bash
docker compose stop backend frontend
docker compose run --rm --no-deps ingest python -m scripts.ingest_exercises
docker compose run --rm --no-deps ingest python -m scripts.ingest_knowledge
docker compose up -d backend frontend
```

替换文献原始文件可用 `docker compose cp path/to/article.md backend:/app/data/knowledge/raw/`（后端运行时执行），再运行文献入库。入库失败先看任务日志，修正密钥/网络后 `docker compose up -d` 重试。旧向量模型与配置不一致时必须显式重新入库，不会在启动时静默重建。

## Alembic 迁移

新数据库（本地默认 SQLite 或 PostgreSQL）使用相同迁移；服务和动作入库的 `init_db()` 已改为调用 Alembic，不再执行 `create_all`。Compose 另有前置迁移任务，确保入库前表结构就绪。新增字段时：

```bash
alembic revision --autogenerate -m "describe schema change"
# 人工检查 migrations/versions 下的新文件，尤其默认值、数据回填和删除操作
alembic upgrade head
alembic check
```

`0001` 是冻结的初始表结构，之后的迁移不能直接引用随业务变化的 `Base.metadata.create_all()`。

已有 `create_all` 创建的无版本本地数据库，先停服务、备份数据库（SQLite 有 WAL 时应使用 SQLite backup API），再显式接管：

```bash
python -m scripts.adopt_legacy_db
alembic upgrade head
alembic check
```

接管脚本仅兼容当前基线及缺少 `training_location` / `excluded_equipment` 的旧档案版本，补列后对比表结构，符合才标记为 `0001`；其他结构差异直接报错，需人工迁移。不要对未知旧库盲目 `alembic stamp head`。该操作仅管理当前数据库版本，不负责 SQLite → PostgreSQL 数据搬迁。

## Nginx、SSE 与云服务器

前端多阶段构建为静态文件，Nginx 负责 SPA 回退与 `/api/` 反向代理。API 关闭响应缓冲和缓存，读写超时设为 900 秒；保留后端的 `X-Accel-Buffering: no`。Nginx 使用 Docker DNS 动态解析后端，容器更换 IP 后可恢复代理。

云服务器复制项目及自己配置的 `.env` 后按首次启动流程执行，防火墙只向外开放 80/443（SSH 按运维需要限制来源）；PostgreSQL 和后端不发布宿主机端口。公网部署需在入口配置 HTTPS 证书，可由云负载均衡或宿主机反向代理终止 TLS；此 Compose 默认仅提供 HTTP，没有自动申请证书。单独使用外部 HTTPS 入口时，将 `HTTP_PORT` 绑定改为宿主机回环地址，例如 Compose ports 使用 `127.0.0.1:8080:80`，避免绕过入口。

本仓库的本地容器验证不代表已经部署到云服务器；真实模型的注册后对话与计划生成仍需要有效的供应商密钥和网络。

## 验证记录（2026-11-18）

- 后端测试 171 项、前端测试 30 项通过，前后端镜像构建成功。
- 隔离 Compose 项目运行真实 PostgreSQL，使用模型与 Embedding 桩服务；预置单个动作原始数据与翻译缓存，执行默认 `bootstrap_docker` 任务完成动作向量、文献原始资料及向量初始化，没有调用真实模型密钥。
- 验证注册、画像、计划校验/保存/激活、SPA 回退与 SSE；首段约 0.03 秒，完成约 3.06 秒，流式响应未被 Nginx 缓冲。
- PostgreSQL 中 `alembic check` 通过；重启数据库、重建后端后账号、计划、对话与文件卷保留。测试容器与测试卷已清理。
- 检查后端镜像不含 `/app/.env`，镜像中文献资料可解析出 17 个片段。
- 真实供应商响应、全量动作下载/翻译和云服务器 HTTPS 尚未验收。

## 从已有 Docker 本地向量库迁移到 Qdrant

保持 Compose 项目名称 `fitnesspro` 与原有卷不变。配置已设为 `vector_store.type: qdrant`、`url: http://qdrant:6333`。

```bash
docker compose stop backend frontend
docker compose up -d qdrant postgres
docker compose build backend
docker compose run --rm ingest
docker compose up -d backend frontend
```

默认入库任务自动将 `app_data` 内已有动作及文献 `.npy` 向量导入 Qdrant，保留原始 ID（Qdrant 点 ID 使用稳定 UUID 映射）。有完整本地向量时不会调用翻译或 Embedding。迁移中断可以重跑；上传失败时不切换别名，也不覆盖原有 `qdrant.json` 清单。旧向量文件、PostgreSQL 用户与训练数据都不删除。

后续新入库仍生成离线向量，再发布至 Qdrant；文献先在暂存目录准备原文、向量、清单再切换目录。重新入库保持维护窗口，完成后重启后端。

本地开发若继续使用 NumPy，设置 `vector_store.type: local`；默认的 `qdrant` 主机名仅在 Compose 网络内可解析，本地 Python 直连需另外提供可访问的 Qdrant URL。不要将未鉴权的 Qdrant 端口直接暴露到公网。

回退时停止后端，将类型改为 `local` 并重启；本地向量仍在。不要删除 `qdrant_data` 或旧集合，除非已确认不再需要。

### Qdrant 迁移验证（2026-11-18）

已在当前 `fitnesspro` Compose 项目完成真实迁移：876 个动作向量、17 个文献片段向量，均为 1024 维。复用已有向量，没有调用模型 API。验证两套生产检索器加载 Qdrant、按原始 ID 过滤检索、与原 NumPy 余弦分数一致。重建 Qdrant 容器后，两套集合仍可检索，前端代理 `/health` 正常；后端回归测试 172 项通过。原 PostgreSQL 与本地向量文件保留。

## 器械图片识别（LLM_VISION）

聊天框支持上传一张 JPEG / PNG / WebP 器械照片（默认最大 8 MiB、2000 万像素）。照片上传后点击发送，Agent 调用 `identify_equipment`，视觉模型返回候选、可见特征与补拍建议。用户下一轮确认器械后，再通过 `search_exercises` / `get_exercise_detail` 推荐和讲解动作；无匹配时明确告知，不将所有 `machine` 动作当成适用。此期不支持体态评估。

### 配置与启动

`llm.vision` 是独立的 OpenAI Chat Completions 兼容视觉接口，必须支持 `image_url`（Base64 data URL）及 JSON 输出。不能直接复用不支持图片的文本模型。当前启用 DeepSeek `deepseek-flash`；权限及价格以供应商为准，可替换为其他兼容视觉模型。

1. 默认复用 `.env` 的 `FAST_LLM_API_KEY`；如需独立 Key，将 `llm.vision.api_key_env` 改为 `VISION_LLM_API_KEY` 并设置对应环境变量（不要提交到 Git）。
2. 核对 `config.yml` 中 `llm.vision.model` / `base_url` 和 `vision.enabled: true`。
3. 构建并更新前后端（不需要重新入库、不改数据库结构）：

```bash
docker compose build backend frontend
docker compose up -d --no-deps backend frontend
```

仅修改挂载的 `config.yml` 时可 `docker compose restart backend`；修改 `.env` 后必须 `docker compose up -d --no-deps --force-recreate backend`，单纯 restart 不会刷新容器环境。

未启用或缺少 Key 时，前端显示配置提示，文字聊天保持可用。工具选择中需要启用“器械图片识别”；确认后推荐动作还需启用动作库工具。识别耗时沿用执行面板工具计时，取消按钮会取消正在等待的视觉请求（供应商是否停止计费取决于其实现）。识别阶段仅允许读取信息，不能自动改画像、记忆或训练计划。

### 存储和隐私

- 上传原始图片经实际解码校验、方向纠正、长边缩至 1600 像素，重新编码 JPEG 并移除 EXIF 等元信息；不保存原图，不向模型发送用户画像或历史全文。
- 照片保存在 `data/uploads/equipment/<用户哈希>/<随机ID>.jpg`，复用现有 Docker `app_data` 卷。备份该卷会包含照片。
- 图片读取和删除均需用户鉴权，不能凭 URL 跨用户读取，无公开静态目录。前端用鉴权请求读取 Blob，URL 不携带 JWT。
- 每次发送最多一张；对话记录只保存图片 ID 和文字识别结果，不保存 Base64。刷新后可回看照片；删除照片后无法再次查看/识别，但既有文字分析仍在，删除会话会删除会话内图片及消息。
- 未发送照片可在预览中删除，切换会话/新建对话/主动退出时会尝试清理草稿照片。浏览器异常退出、上传后刷新或网络中断可能留下未引用文件；当前不自动过期清理，需管理员按实际保留政策管理该目录。
- 上传入口为输入行左侧图标；输入区显示 88px 缩略图及右上角删除按钮，不显示独立隐私提示。照片会发送至配置的模型供应商，此功能只收器械照片，不用于体态、疾病或身份判断。
- Nginx 上传上限已调整为 9 MiB；提高后端 `max_upload_bytes` 时需同步检查该限制。

### 验收

自动化测试使用模拟视觉和聊天响应，覆盖上传格式/大小/像素限制、EXIF 去除、跨用户隔离、删除、结构化结果校验、不确定/非器械结果、图片聊天→确认→动作检索、历史回看和取消识别。实际识别准确率仍需配置真实视觉服务后，用全景、局部、模糊、相似器械和非器械图片验收。不会在默认测试中调用付费服务。

### 图片功能验证（2026-11-19）

- 本地后端 183 项、前端 33 项测试通过；前后端生产构建通过。新后端镜像在无网络、无生产数据卷的临时容器内回归 183 项通过。
- 复用现有 SiliconFlow Key，模型列表确认 `Qwen/Qwen3-VL-32B-Instruct` 可用；公开哑铃卧推示意图真实调用返回哑铃相关候选和确认要求，本地约 8.3 秒、Docker 内约 7.5 秒。这只是接口烟雾测试，不代表真实健身房照片准确率。
- 已更新当前 Docker 前后端；通过 Nginx 上传约 3 MiB PNG，验证重新编码 JPEG、私有鉴权读取、无缓存、卷内落盘和删除成功，临时账号与图片已清理。
- 图片识别轮次在执行器层禁止写操作，并跳过后台个人记忆提取，避免将视觉推测写入档案或记忆。

### 视觉模型与输入区更新（2026-11-19）

视觉请求已切换为 DeepSeek `deepseek-flash`，复用 `FAST_LLM_API_KEY`，关闭思考模式。公开哑铃卧推示意图真实接口测试约 2.5 秒返回候选；单次结果不代表稳定延迟。输入区照片改为固定 88px 缩略图、cover 裁切、10px 圆角及右上角圆形 ×，上传入口为输入行左侧图标，移除独立隐私文字；文字输入行保持固定高度。
