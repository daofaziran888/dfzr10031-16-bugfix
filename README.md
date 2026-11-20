# 数据分析实验与数据应用服务平台

面向指标口径管理、分析实验设计、OLAP查询、数据看板构建与数据API发布的数据应用平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标定义 | `metric_def` | 指标 | 指标编号、指标名称、计算公式 |
| 维度管理 | `dimension` | 分析维度 | 维度编号、维度名称、维度类型 |
| 数据集管理 | `dataset` | 分析数据集 | 数据集编号、数据集名称、来源类型 |
| 分析实验 | `experiment` | 实验方案 | 方案编号、实验名称、分析数据集 |
| OLAP查询 | `query_builder` | 查询模板 | 模板编号、模板名称、查询语句 |
| 数据看板 | `dashboard` | 看板配置 | 看板编号、看板名称、包含图表 |
| 图表配置 | `chart` | 图表 | 图表编号、图表名称、图表类型 |
| 数据API发布 | `data_service` | 数据服务 | 服务编号、服务名称、输出数据集 |
| 智能告警 | `alert` | 告警规则 | 规则编号、告警名称、监控指标 |
| 分析用户组 | `user_group` | 用户组 | 组编号、组名称、组描述 |
| 数据快照 | `snapshot` | 快照记录 | 快照编号、数据集名称、快照时间 |
| 任务调度 | `scheduler` | 调度任务 | 任务编号、任务名称、调度类型 |
| 缓存管理 | `cache` | 缓存配置 | 缓存编号、缓存目标、缓存策略 |
| 分析管道 | `pipeline` | 分析节点 | 节点编号、输入数据集、分析算子 |
| 分析模型 | `model` | 模型配置 | 模型编号、模型名称、模型类型 |
| 数据集市 | `data_mart` | 集市表 | 表编号、表名称、主题域 |
| 数据导出 | `export` | 导出任务 | 任务编号、导出对象、导出格式 |
| 数据审计 | `audit` | 审计记录 | 记录编号、操作类型、操作人 |
| 关账账本 | `ledger` | 关账批次 | 批次号、业务日、版本、期末结余 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 关账账本

数据快照只登记校验哈希，证明「取过数」，但证明不了「那天账上应该是多少」。
关账账本（`backend/app/services/ledger.py`、`frontend/src/views/ledger/index.vue`）
按业务日冻结每个仓、每个商品的出入库与盘点，关账后发布只读批次，供数据集市
按仓查询可用结余。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/ledger/catalog` | 仓库、商品、期初结余、已关账业务日 |
| POST | `/api/ledger/movements` | 登记出入库变动（正数入库、负数出库） |
| POST | `/api/ledger/transfers` | 登记跨仓调拨（两侧成对，单边会被拒绝） |
| POST | `/api/ledger/counts` | 登记盘点合计（按 `count_version` 换版） |
| POST | `/api/ledger/close` | 关账某个业务日 |
| GET | `/api/ledger/days/{date}` | 某日截面，`?edition=1` 或 `?batch_id=` 回看历史版 |
| GET | `/api/ledger/days/{date}/editions` | 该日所有版本及逐行差额 |
| GET | `/api/ledger/days/{date}/verify` | 调拨守恒、结余公式、日间衔接自检 |
| GET | `/api/ledger/delta?earlier=&later=` | 两日关系：后一日期初 vs 前一日期末 |
| GET | `/api/ledger/batches[/{id}]` | 关账批次列表 / 按批号取冻结截面 |
| GET | `/api/ledger/trace?transfer_id=` | 实际结余、当天调整、来源批号 |
| GET | `/api/data_mart/balances?biz_date=` | 集市口径的按仓可用结余（只认已关账批次） |
| POST | `/api/ledger/seed-demo` | 预置两日演示账本（第 1 日漏记 10 件调拨） |

账本落盘在 `backend/data/ledger.json`（可用 `LEDGER_DATA_DIR` 改目录，
该文件已被 `.gitignore` 忽略），原子替换写入，服务重启后先读盘再对外服务。

### 规则

- **按业务日挂账。** 晚到的补录记进它实际发生的 `biz_date`，从那天往后逐日
  重算；已关账的日期追加**修订批次**，首发批次原样保留、仍可按批号回看。
- **调拨守恒。** 出库仓 `-qty`、入库仓 `+qty` 一次落两行，`transfer_id` 相同；
  只提交一侧直接拒绝，批次自检里还会核对每个调拨号的合计为 0。
- **幂等。** 显式 `idempotency_key` 优先级最高，否则按业务内容派生；同一笔
  回补重复提交只回放首次结果，不参与求和。两仓并发提交同一笔调拨时由写锁
  排队，后到的命中幂等键，账本里只出现一组调拨行。
- **盘点换版是替换。** 结余取最新盘点版本，`count_adjustment = 实盘 - 账面`；
  同版本重提直接回放，换版重新算差额，不会把盘盈亏叠加多次。
- **截面不可变。** 批次发布后只读，所以页面刷新、再次取数、重启服务看到的
  是同一份关账截面；两日账本关系用 `/api/ledger/delta` 独立核对。

### 修订策略：次日补记，还是同时发布两日修订？

场景：第 1 日少记甲仓转给乙仓 10 件，第 2 日两仓已按旧账开账。

本实现选择**同时发布两日修订账本**：

1. 第 1 日的关账结余本身就错，只在第 2 日补一笔「调整」会让第 1 日的历史报表
   永远错着，也会把第 2 日的经营变动和补记差额混在同一行；
2. 重述后第 1 日结余改成正确值，第 2 日的**期初**跟着重述，而第 2 日当天的
   `net_movement`（示例里甲仓 -4、乙仓 +6）保持原样，差额落在哪一天一目了然；
3. 首发批次（`edition=1`）仍在账本里，可用 `?edition=1` 或批号回看；
4. 修订行带 `opening_delta` / `closing_delta` / `previous_batch_id`，数据集市按
   批号取数，两版截面能独立核对。

复现（服务起来后）：

```bash
curl -X POST 127.0.0.1:8000/api/ledger/seed-demo
curl -X POST 127.0.0.1:8000/api/ledger/transfers -H 'Content-Type: application/json' -d '{
  "values": {"from_warehouse":"WH-A","to_warehouse":"WH-B","sku":"SKU-1001",
             "biz_date":"2026-09-01","qty":10,"lot":"LOT-0901",
             "transfer_id":"TRF-0901-01","idempotency_key":"BK-D1-10"}}'
curl '127.0.0.1:8000/api/ledger/days/2026-09-01'            # 第 2 版：甲 90 / 乙 90
curl '127.0.0.1:8000/api/ledger/days/2026-09-01?edition=1'  # 首发：甲 100 / 乙 80
curl '127.0.0.1:8000/api/ledger/days/2026-09-02'            # 期初已重述，当天变动不变
curl '127.0.0.1:8000/api/ledger/delta?earlier=2026-09-01&later=2026-09-02'
curl '127.0.0.1:8000/api/data_mart/balances?biz_date=2026-09-01'
```

重启服务后再打开这两天的关账记录，结余、当天调整和来源批号都不变。

### 测试

```bash
cd backend
.venv/bin/pip install pytest   # 只在本地跑测试时需要
.venv/bin/python -m pytest tests/ -q
```

覆盖调拨守恒、单边拒绝、重复回补幂等、两仓并发只记一次、盘点换版不重复、
晚到更正重述两日、首发可回看、集市只认关账批次、重启后截面一致。
