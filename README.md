# 实时指标计算与监控大盘平台

面向实时指标计算引擎、多维度下钻分析、告警阈值管理、指标异常检测、数据服务开放与可视化看板配置的数据分析与应用工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
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
| 实时指标 | `realtimemetric` | 指标定义 | 指标编号、指标名称、计算窗口秒 |
| 监控大盘 | `dashboard` | 大盘配置 | 大盘编号、大盘名称、包含指标组 |
| 多维下钻 | `drilldown` | 下钻路径 | 路径编号、起始维度、下钻层级序列 |
| 阈值管理 | `threshold` | 阈值规则 | 规则编号、关联指标、阈值下限 |
| 异常检测 | `anomalydetect` | 检测模型 | 模型编号、监控指标、检测算法 |
| 数据服务 | `dataservice` | 服务接口 | 服务编号、服务名称、接口路径 |
| 告警历史 | `alarmhistory` | 告警记录 | 记录编号、告警规则、触发时间 |
| 同比环比 | `comparison` | 对比配置 | 配置编号、目标指标、对比类型 |
| 聚合配置 | `aggregate` | 聚合规则 | 规则编号、源数据粒度、目标粒度 |
| 筛选预设 | `filterpreset` | 预设模板 | 预设编号、预设名称、维度条件组 |
| 用户洞察 | `userinsight` | 洞察报告 | 报告编号、分析主题、目标人群 |
| 事件分析 | `eventanalysis` | 分析模型 | 模型编号、事件名称、分组维度 |
| 分群画像 | `segmentprofile` | 画像定义 | 画像编号、分群名称、筛选规则 |
| 趋势预测 | `trendforecast` | 预测方案 | 方案编号、预测指标、算法模型 |
| 可视化模板 | `viztemplate` | 图表模板 | 模板编号、图表类型、数据绑定 |
| 订阅推送 | `subscription` | 订阅配置 | 订阅编号、订阅内容、接收人 |
| 维度映射 | `dimensionmap` | 映射规则 | 规则编号、原始维度名、标准维度名 |
| 数据质量分 | `qualityscore` | 评分规则 | 规则编号、数据集名称、完整性权重 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 筛选预设的保存、套用与快照

筛选预设把「保存 → 评审 → 发布 → 套用」串成一条数据流，所有规则都在
`backend/app/services/filterpreset.py`：

- **保存不丢字段**：新建/修改会一并保存时间范围、排序规则（`显示顺序` 别名）、
  显示列、共享状态和发布状态，`使用次数` 由系统维护、不可外部写入。
- **发布状态只能流转**：`草稿 → 预设评审中 → 已发布 → 已废弃`；直接改状态会被拒绝，
  重复执行同一动作是幂等的。已发布预设修改条件会回落到评审中，重新发布才递增版本。
- **条件完整解析**：`维度条件组` 支持 JSON 数组、`{"conditions": [...]}` 和
  `区域=华东;渠道 in [线上,APP]` 文本写法，解析时保留全部条件（不会只取第一个）。
- **套用与快照**：只有已发布预设可套用；套用时生成不可变的筛选条件数据快照
  (`SNAP-xxxx`)，包含有效条件、失效维度、显示列、时间范围、预设版本、使用次数和幂等键。
  失效维度只记录不参与查询，显示列同理。
- **使用次数不虚增**：只有成功套用才计数；同一 `request_id` 重复刷新返回原快照，
  不重复累加；回看旧快照（`snapshot_id`）也不计数。已废弃预设不能再套用，
  但历史快照仍可只读回看。
- **上游依赖**：生成快照前需要预设模板、维度条件组、时间范围记录就绪；依赖按提交顺序
  校验，每类只采纳首个通过的记录，被跳过的记录原因写在快照的 `依赖跳过` 里。
- **下游消费**：多维下钻通过 `POST /api/drilldown/{id}/snapshot` 消费快照，只记录
  `筛选快照编号`，不复制条件文本，也不回写预设使用次数。

主要接口：`GET/POST /api/filterpreset`、`PUT /api/filterpreset/{id}`、
`GET /api/filterpreset/{id}`（详情）、`POST /api/filterpreset/{id}/apply`、
`GET /api/filterpreset/snapshots[/{snapshot_id}]`、`GET /api/filterpreset/usage`。

## 测试

```bash
cd backend
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests -q
```


领域：数据工程
子方向：数据分析与数据应用
