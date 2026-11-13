# 钻孔分层网页补录服务

地质数据员把钻孔分层 CSV 补录到**只能逐层填表**的资料库，供剖面建模。
本服务用 **FastAPI + Playwright + SQLite** 提供补录 API：接收门户地址与 CSV，
**只通过网页“查询 + 填表”**完成补录，不直连任何门户后端接口。

## 一、核心业务规则（严格实现）

按 **孔号 + 层顶深度 + 层底深度** 唯一识别一层，逐行处理且**提交前必须先查**：

| 查询结果 | 处理 | 行状态 |
|---|---|---|
| 唯一记录且岩性相同 | 跳过，不填报 | `SKIPPED` |
| 不存在 | 才打开表单逐层填报，保存确认 | `CONFIRMED` |
| 多条记录 | 停在该行，后续行不动 | `CONFLICT` |
| 唯一但岩性不符 | 停在该行，后续行不动 | `CONFLICT` |

**保存可能延迟可见、响应可能中断**，因此：

- 已点击保存却**未拿到确认**（超时 / 断线 / 重启）→ 行落 `UNCONFIRMED`。
- 对 `UNCONFIRMED` 行，**恢复时只允许查询，查不到也绝不自动重提**，继续停行等待人工核实。
- 提交过程（点击保存之前）崩溃不落“已提交”，重启后仍是 `PENDING`，可安全重试。
- `CONFLICT` / `UNCONFIRMED` 行可通过 `POST .../resume` **续查**：查询变为唯一且岩性一致即解除并继续后续行，否则继续停行。
- 已确认（`CONFIRMED`/`SKIPPED`）的行在任何重复、并发、重启场景下都**不会重复填报**。

**重复 / 并发去重**：任务以 `(门户 URL, 文件 SHA-256)` 唯一约束。同一门户收到相同文件的
重复或并发请求，都接续**同一个任务**，返回同一任务 ID。

**持久化与重启**：任务与逐行进度全部写入 SQLite（挂载卷）。服务重启后：

- 残留的 `RUNNING` 任务在启动时自动重新排队（行级进度保留）；
- 可随时 `GET` 查询进度，对停行任务调 `resume` 续查。

## 二、目录结构

```
app/
  config.py              运行参数（超时、复核次数等，环境变量覆盖）
  main.py                FastAPI：补录/查询/续查 API + 后台工作循环
  worker/
    states.py            行/任务状态常量与中文说明
    db.py                SQLite 持久层（任务、逐行状态、崩溃恢复）
    csvparse.py          CSV 解析校验（中英文表头、UTF-8/GB18030）
    automation.py        Playwright：仅网页查询与逐层填表
    engine.py            补录状态机（先查后填、冲突停行、未确认只查不填）
  portal/
    server.py            无账号本地演示门户（延迟入库/断线/冲突/慢响应注入）
demo/
  layers.csv             中文表头演示数据
  layers_en_header.csv   英文表头演示数据
  demo.py                一键端到端演示脚本
tests/                   33 个单元/集成测试（假自动化，无需浏览器）
docker-compose.yml       门户 + 补录 API + 一键演示
```

## 三、API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/ingest` | 表单字段 `portal_url` + 文件 `file`（CSV），返回任务（202） |
| `GET` | `/api/tasks` | 任务列表与计数 |
| `GET` | `/api/tasks/{id}` | 任务详情（`rows=true` 含逐行状态） |
| `GET` | `/api/tasks/{id}/rows` | 逐行状态、说明、门户记录链接 |
| `POST` | `/api/tasks/{id}/resume` | 续查/恢复停行任务并继续后续行 |
| `GET` | `/health` | 健康检查 |

每行返回：`row_no, hole_no, top_depth, bottom_depth, lithology, status,
status_text, detail, record_url, attempts, updated_at`。
`record_url` 为门户记录的绝对链接。

### 调用示例

```bash
curl -X POST http://localhost:8080/api/ingest \
  -F "portal_url=http://portal:8000/" \
  -F "file=@demo/layers.csv"

curl http://localhost:8080/api/tasks/<任务ID>/rows
curl -X POST http://localhost:8080/api/tasks/<任务ID>/resume
```

交互式文档：`http://localhost:8080/docs`。

## 四、启动与一键演示（Docker Compose）

```bash
# 构建并启动门户(8000)与补录 API(8080)
docker compose up -d --build

# 一键演示全部场景（延迟入库 / 保存后断线 / 冲突 / 重复并发 / 持久化）
docker compose --profile demo run --rm demo
```

演示脚本依次验证：

1. **延迟入库**：门户保存后 8s 才可查询（超过补录端复核窗口）→ 首行停在 `UNCONFIRMED`
   → 延迟窗口后 `resume` 续查确认并继续后续行；
2. **重复 / 并发**：同文件并发 3 次只产生 1 个任务，已确认行零重填；
   门户已存在的数据用新文件补录时全部 `SKIPPED`；
3. **保存后断线**：门户已落库但响应中断 → `UNCONFIRMED` 停行、不自动重提
   → `resume` 续查发现记录已存在并确认；
4. **岩性冲突**：门户同层异岩性 → 停在首行、后续行保持 `PENDING`，
   续查仍冲突则继续停行，人工核实后续查放行；
5. **进度查询 / 持久化**：打印任务与逐行计数，并给出重启验证命令。

### 手动制造故障场景

演示门户提供管理接口（仅用于演示，不是补录通道）：

```bash
curl -X POST localhost:8000/admin/fail-mode -d "mode=disconnect"   # none|disconnect|conflict|slow
curl -X POST localhost:8000/admin/visibility-delay -d "delay_ms=8000"
curl -X POST localhost:8000/admin/reset
curl localhost:8000/admin/state
```

`slow` 模式保存响应约 15s，而补录端提交超时 8s（见 compose 环境变量），
可确定性制造“已提交未确认”。

### 重启恢复验证

```bash
docker compose restart api
curl localhost:8080/api/tasks          # 进度仍在
docker compose --profile demo run --rm demo   # 或直接 resume 停行任务
```

## 五、关键时序参数（环境变量）

| 变量 | 默认 | 含义 |
|---|---|---|
| `SUBMIT_TIMEOUT_MS` | 8000 | 点击保存后等待确认的超时，超时即 `UNCONFIRMED` |
| `VISIBILITY_WAIT_MS` / `VISIBILITY_MAX_ATTEMPTS` | 1500 / 3 | 保存确认后查询复核的间隔与次数 |
| `NAV_TIMEOUT_MS` | 15000 | 页面导航/元素超时 |
| `MAX_RUNTIME_ERRORS` | 5 | 浏览器运行时错误连续多少次后任务置 `FAILED`（仍可 resume） |
| `HEADLESS` | true | Playwright 是否无头 |

## 六、本地测试（无需浏览器）

```bash
pip install -r requirements.txt   # 测试用假自动化，可不装 playwright 浏览器
pytest -q
```

33 个测试覆盖：CSV 校验、SQLite 持久化与崩溃恢复、状态机全部分支
（跳过/填报/冲突/断线未确认/延迟可见/续查/重启不重提/并发去重）、API 与门户行为。
