# 拾影工坊 · 音视频采集与转写结构化平台

面向音视频媒体源接入、分片切分、转码作业、转写任务、语音分段、说话人标注、字幕对齐、时间轴标注、术语抽取、转写版本、置信复核、纠错回路、字幕包交付、分片水位与失败重投的采集结构化平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
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

健康检查：`curl http://127.0.0.1:8005/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5178/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8005`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 媒体源 | `media_source` | 媒体源 | 媒体源编号、来源渠道、媒体标题 |
| 采集通道 | `capture_channel` | 通道配置 | 通道编号、关联媒体源、并发上限 |
| 媒体分片 | `media_shard` | 分片记录 | 分片编号、关联通道、起始秒 |
| 转码作业 | `transcode_job` | 转码作业 | 作业编号、关联分片、目标编码 |
| 转写任务 | `transcribe_task` | 转写任务 | 任务编号、关联分片、转写引擎 |
| 语音分段 | `speech_segment` | 语音段 | 分段编号、关联任务、起始毫秒 |
| 说话人标注 | `speaker_label` | 标注条目 | 标注编号、关联分段、说话人标识 |
| 字幕对齐 | `subtitle_align` | 对齐结果 | 对齐编号、关联分段、字幕行号 |
| 时间轴标注 | `timeline_mark` | 标注点 | 标注点编号、关联媒体源、标注毫秒 |
| 术语抽取 | `term_extract` | 术语条目 | 术语编号、关联任务、术语文本 |
| 转写版本 | `transcript_version` | 版本记录 | 版本编号、关联任务、版本号 |
| 置信复核 | `confidence_review` | 复核条目 | 复核编号、关联分段、段置信度 |
| 纠错回路 | `correction_loop` | 纠错单 | 纠错编号、关联版本、纠错范围毫秒 |
| 字幕包 | `subtitle_pack` | 字幕包 | 字幕包编号、关联媒体源、绑定版本 |
| 媒体索引 | `media_index` | 索引条目 | 索引编号、关联媒体源、索引词 |
| 分片水位 | `shard_watermark` | 水位读数 | 水位编号、关联媒体源、已处理分片序号 |
| 失败重投 | `fail_redeliver` | 重投单 / 阶段工单 | 重投编号、关联分片、失败阶段 |
| 转写配额 | `transcribe_quota` | 配额条目 | 配额编号、来源渠道、转写引擎 |
| 抽样质检 | `sample_qc` | 质检样本 | 样本编号、关联任务、抽样比例 |
| 交付回执 | `delivery_receipt` | 回执记录 | 回执编号、关联字幕包、接收方标识 |
| 归档架位 | `archive_shelf` | 归档条目 | 归档编号、关联媒体源、架位标识 |
| 采集审计 | `media_audit` | 审计条目 | 审计编号、关联对象、操作类型 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 失败分片按阶段领办

失败重投的重投单同时是一张可领办的**阶段工单**：`失败阶段` 记录它为什么失败
（转码失败 / 转写失败），`当前阶段` 记录现在该干转码还是转写。页面入口
`/fail_redeliver/claim`，领域逻辑在 `backend/app/pipeline/stage_claim_engine.py`。

- 待办：`GET /api/fail_redeliver/todos?stage=转码|转写&page=&size=`，
  按分片序号升序（同号按批次、工单号）排序后分页；`/todos/summary` 给阶段计数。
- 领办：`POST /api/fail_redeliver/orders/{order}/claim`，开工回执统一返回
  `{ order, receipt, audio_binding }`；`/start` 标记开工，`/transfer` 原单转办，
  `/close-reclaim` 结单改领，`/confirm` 确认产出。
- 音轨：`POST /api/fail_redeliver/artifacts` 补登转码实物；
  `GET /api/fail_redeliver/receipts/{receipt}` 凭回执定位实际投入音轨。

现场必须守住的口径：

| 约束 | 实现位置 |
| --- | --- |
| 一张工单同一时刻最多一个执行中阶段 | `StageClaimEngine.claim/transfer` |
| 音轨按「批次 + 分片序号」匹配，异批次同号不能代跑 | `latest_artifact/usable_artifact` |
| 已有可验证转码实物（成功标记 + 录音字节 + 音轨引用）不再转码 | `claim` 的转码分支 |
| 转码回执只有成功标记、没有录音时，仍领取转码并保留原失败原因 | `claim` 的转写分支回落 |
| 开工回执绑定实际投入音轨，确认产出时核对一致 | `confirm_output` |
| 转写任务凭回执定位音轨后才记录产出 | `TranscribeTaskService.produce_transcription` |

可验证音轨 = 有成功标记 **且** 有录音字节 **且** 有音轨引用；三者缺一都只能算
「仅成功标记无录音」，不能据此领取转写。

种子数据预置了 4 号片现场：`FRDL-0004` 无音轨（先领转码，迟到音轨后可选转办或
结单改领）、`FRDL-0005` 只有成功标记（领取时会落回转码并回显原失败原因）、
`FRDL-0006` 已有可验证音轨（直接领转写），另有异批次 `BATCH-20260902` 的 4 号音轨
用于验证不能跨批次代跑。

领域：数据工程
子方向：数据采集、爬虫与RPA
