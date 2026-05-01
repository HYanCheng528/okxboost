# 链上刷量统计机器人

> **仅统计，不下单。** 抓取 EVM 链上指定钱包的交易记录，自动配对买卖 Cycle，汇总交易量、磨损、Gas 等关键指标，支持多时间段、多钱包、飞书同步和 CSV 导出。

支持链：**BSC · Base · Arbitrum · Ethereum**

---

## 功能一览

| 功能 | 说明 |
|------|------|
| Cycle 配对 | 将买入/卖出交易自动配对成完整交易周期（余额归零或超时强制闭合） |
| 多时间段统计 | 一个任务内可添加多个时间段，分段汇总 |
| 多钱包支持 | 逗号分隔，同时统计多个钱包 |
| Boost 对比 | 计算预期 Boost 交易量与实际值的差异 |
| Gas 汇总 | 统计 Native Gas 和 USD 换算 |
| 文件夹分类 | 任务可归入文件夹，方便管理多个项目 |
| 追加时间段 | 对已有任务追加新时间段并重扫，无需重建任务 |
| 飞书同步 | 将统计结果追加写入飞书多维表格 |
| CSV 导出 | 一键导出 Cycle 配对明细 |
| 暗色 Dashboard UI | 浏览器访问，无需安装客户端 |

---

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/HYanCheng528/okxboost.git
cd okxboost
```

### 2. 创建虚拟环境并安装依赖

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

用文本编辑器打开 `.env`，填入你自己的 API Key：

```env
TX_SOURCE=explorer          # 数据来源，推荐 explorer

BSC_RPC_URL=https://rpc.ankr.com/bsc/YOUR_ANKR_API_KEY,...
BSC_EXPLORER_API_KEY=YOUR_BSCSCAN_API_KEY

BASE_RPC_URL=https://rpc.ankr.com/base/YOUR_ANKR_API_KEY
ARBITRUM_RPC_URL=https://rpc.ankr.com/arbitrum/YOUR_ANKR_API_KEY
ETHEREUM_RPC_URL=https://rpc.ankr.com/eth/YOUR_ANKR_API_KEY

# 飞书同步（可选）
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_APP_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**API Key 获取方式：**

- **Ankr RPC**：注册 [Ankr](https://www.ankr.com/)，在 Dashboard 创建 Endpoint 获取 API Key（免费套餐够用）
- **BSCScan API Key**：注册 [BscScan](https://bscscan.com/)，进入 API Keys 页面创建（免费）
- **飞书**：在[飞书开放平台](https://open.feishu.cn/)创建企业自建应用，获取 App ID / App Secret / App Token

### 4. 启动服务

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)

---

## 使用教程

### 新建统计任务

1. 点击左侧导航 **新建任务**
2. 填写参数：

   | 字段 | 说明 |
   |------|------|
   | 统计链 | 选择 BSC / Base / Arbitrum / Ethereum |
   | 任务名称 | 可选，方便识别，如"2月BSC统计" |
   | 文件夹 | 可选，用于分类管理多个任务 |
   | 钱包地址 | 多个钱包用英文逗号分隔 |
   | Token CA | 目标代币的合约地址 |
   | 基准币种 | USDT 或 USDC |
   | 时间段 | 选择统计的开始/结束时间（UTC+8），可点击"+ 增加时间段"添加多段 |
   | Boost 倍数 | 计算预期 Boost 交易量的系数，默认 0.85 |
   | 尾差容忍值 | Cycle 配对时允许的余额误差，默认 0.0001 |
   | 配对超时 | 超过此分钟数未闭环则强制结束 Cycle，默认 30 分钟 |
   | 实际 Boost | 可选，填入平台显示的实际 Boost 值用于对比 |

3. 点击 **开始统计**，任务创建后自动跳转到详情页

### 查看任务详情

1. 点击左侧 **任务详情**，或在任务列表点击任意一行
2. 点击 **查看详情** 加载汇总数据（每 5 秒自动刷新）
3. 汇总卡片展示：总交易量、计算 Boost、实际 Boost、Boost 差值、总磨损、平均费率、Gas 消耗等
4. 展开时间段明细可查看每段的分项数据

### 查看配对记录

在任务详情页点击 **查看配对记录**，展示每个 Cycle 的：
- 钱包地址
- 开始/结束时间
- 交易前/后余额
- 交易量、磨损、费率
- Gas（Native + USD）
- 是否完整闭环
- 关联交易哈希

### 追加时间段并重扫

对已有任务追加新的统计时间段，无需重建任务：

1. 在任务详情页下方找到 **追加时间段并重扫**
2. 填写新的时间段（默认从上次结束时间续接）
3. 点击 **追加并重扫**

### 导出 CSV

在任务详情页点击 **导出 CSV**，下载当前任务所有 Cycle 配对明细。

### 同步到飞书多维表格

1. 确保 `.env` 中已填写飞书 App 凭证
2. 在任务详情页下方 **同步到飞书** 区域：
   - 选择目标飞书子表
   - 确认字段名称与飞书表格列名一致（日期、交易前、交易后、gas费）
3. 点击 **同步到飞书**，数据以追加方式写入，不覆盖已有记录

### 文件夹管理

- 在 **任务列表** 页点击 **创建文件夹**，输入名称后确认
- 在任务详情页的 **归属文件夹** 下拉框中选择文件夹，点击 **保存分类**
- 任务列表支持按文件夹筛选

---

## 项目结构

```
okxboost/
├── app/
│   ├── main.py                  # FastAPI 入口
│   ├── models.py                # 数据库模型（Task, Cycle, TxCache, Price）
│   ├── schemas.py               # Pydantic 请求/响应模型
│   ├── database.py              # SQLite 初始化
│   ├── config.py                # 配置加载（从 .env 读取）
│   ├── time_utils.py            # UTC+8 时间工具
│   ├── routers/
│   │   └── tasks.py             # RESTful API 路由
│   ├── services/
│   │   ├── cycle_matcher.py     # Cycle 配对算法
│   │   ├── calculator.py        # 汇总指标计算
│   │   ├── task_runner.py       # 任务编排执行
│   │   ├── task_progress.py     # 进度追踪
│   │   ├── price_service.py     # 历史价格（CryptoCompare）
│   │   ├── feishu_bitable.py    # 飞书多维表格 API
│   │   └── chain/
│   │       ├── evm_provider.py  # EVM 链交易解析
│   │       ├── mock_provider.py # Mock 数据回放（测试用）
│   │       └── types.py         # 链相关类型定义
│   └── static/
│       └── index.html           # 前端单页应用
├── tests/                       # 单元测试
├── data/
│   └── sample_transactions.json # 测试用 Mock 数据
├── .env.example                 # 环境变量模板（复制为 .env 后填写）
├── .gitignore
└── requirements.txt
```

---

## API 文档

启动后访问 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) 查看完整 Swagger 文档。

主要接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/tasks` | 创建任务 |
| GET | `/api/tasks` | 任务列表 |
| GET | `/api/tasks/{taskId}` | 任务详情（含 summary） |
| GET | `/api/tasks/{taskId}/cycles` | Cycle 配对明细（分页） |
| POST | `/api/tasks/{taskId}/append-ranges` | 追加时间段并重扫 |
| GET | `/api/tasks/{taskId}/export.csv` | 导出 CSV |
| POST | `/api/tasks/{taskId}/sync-feishu` | 同步到飞书 |
| DELETE | `/api/tasks/{taskId}` | 删除任务 |

---

## 运行测试

```bash
pytest tests/ -v
```

---

## 注意事项

- `.env` 文件包含私钥信息，**永远不要提交到 Git**（已在 `.gitignore` 中排除）
- 数据库文件 `okx_volume_stats.db` 同样被排除，不会上传
- 统计结果仅供参考，不构成任何投资建议
- RPC 节点有速率限制，任务长时间卡在同一阶段通常是限流导致，稍等即可自动重试

---

## License

MIT
