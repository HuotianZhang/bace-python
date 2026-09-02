# BACE 服务层规划 — v1

2026-09-02。输入：Round 3 UI（`docs/BACE Console - Round 3.dc.html`）、
项目文档 `bace-ui-flow-model.md`、`bace-ui-round2-brief.md`（三层判决、来源标记）、
`bace-open-defects.md`（第四批 = 写 service/ 之前定）。

## 一句话

服务层是**引擎外面的一层薄壳**：一个进程跑在 sternwarte 上，独占 rig，把
`run_transient_scan` / `run_jv` 这些**事件生成器**包成 HTTP + WebSocket，
浏览器里的 console 是它唯一的客户端。它不重写任何测量逻辑——逻辑仍在
`bace/experiment/`，服务层只做：排队、广播事件、记 journal、执行 pipeline 树。

## 进程与并发模型

- **一个 service 进程**（lab PC，`py -3`，绑定 127.0.0.1），FastAPI + uvicorn，
  顺带 serve 前端静态文件。VISA 仪器（2400 / 33220A / 81150A / 示波器 / relay /
  shutter）只有这个进程碰。
- **既有的独立 console 保持独立**：1918-C 在 :8918（单进程约束），331 在 :8331
  （未接线）。服务层像现在的代码一样走 HTTP 找它们。
- **同一时刻至多一个 RunWorker**（bench 锁）。生成器在 worker 线程里跑（阻塞
  VISA I/O）；RunRecorder / JVRecorder 套在生成器链里面，跟 tools/scan.py 一样
  （`storage.recorder.record`）；事件进 asyncio 队列 → 扇出给 WebSocket 订阅者 +
  journal。（2026-09-02 订正：recorder 不在 asyncio 扇出里，见 contract §2。）
- **手动跑一张卡 = 单节点 pipeline**，同一条代码路径——这就是 flow-model 里
  "manual run 和 pipeline step 共享 monitor 和 history"的实现方式。
- **并发只允许不碰 VISA 总线的观察者**：power monitor 走 :8918 的 HTTP，可以
  在 bace 扫描期间旁路运行（R3·2 画的就是这个）；GPIB 上的任何操作都必须经过
  唯一的 worker。这条规则同时回答了「power 是模块还是监视器」——两者都是：
  `power read` 是模块（占 worker），`power monitor` 是观察者（不占）。

## API 草图（资源随 UI 的四个 tab）

```
GET  /bench                     仪器状态 + 触发链读回 + 生效的 rig.toml 数值
POST /bench/read                重读链条/仪器状态（strip 的 re-read）
POST /bench/actions/{name}      显式单击动作：set-33220a-pol-inv · park · relay …
                                → 每个动作进 journal（"by hand"条目，R3·2 已画）
GET  /modules                   模块目录 + 每个参数 {value, source, editable}
PUT  /modules/{m}/params        编辑（source 变 edited；reset 回 default）
POST /runs                      起一个模块 run {module, params} → run_id
POST /runs/{id}/stop            {mode: after_shot | abort}   两个诚实的动词
GET  /runs · /runs/{id}         journal（session log 和 this session 卡）
GET  /runs/{id}/data            光暗轨 / Q(loop) / J-V 曲线，喂卡片内嵌图
POST /pipelines/validate        树进 → 解析后的执行序列 + 16 checks + 成本估计
POST /pipelines                 Start（内部先 validate；crit 挡回）
WS   /events                    全部事件流
```

Dry run = `validate` + 返回完整 schedule，不碰任何输出——UI 的 Dry run 按钮
就是它。

## 事件与 journal

- **线上格式 = 现有 dataclass 事件的 JSON**（RunStarted / DCMeasured /
  AxisResolved / InstrumentState / StepStarted / StepDone / LoopDone / Progress /
  RunFinished / RunAborted / RunFailed / Notice），服务层加信封
  `{seq, ts, run_id, node_path}`。`node_path` 是 pipeline 树里的位置
  （如 `T=250K/led=1.020V/bace`），三个计数器三个时间尺度直接从它渲染。
- **journal = 每 session 一个 append-only jsonl** + 现有 RunRecorder
  （HDF5 `bace-run/2` + legacy `.dat`）原样不动。append-only 是为将来的
  失败恢复留的地基（本轮不做恢复，但格式先立对）。
- 判决遵守三层（round2-brief）：crit 只有硬件安全；warn 只陈述证据；
  服务层不自动修任何东西——修正 = `/bench/actions/*` 的显式调用。

## Pipeline 执行器（唯一的新逻辑）

`service/pipeline.py`：树 = `loop(temperature | illumination | repeat)` +
`module` 叶子。执行器负责且只负责三条绑定（flow-model 原文）：

1. illumination loop 持有 `led_v`，注入每个子模块——模块自己不带；
2. `jv_bace` → V_oc → 同一 led_v 下的 `bace`（`centre_on_voc`）；无 V_oc 源的
   `bace` 在 validate 就拒绝；
3. 每个 `jv_* ↔ bace` 边界插入 relay 过渡（disable → switch → enable）。

**temperature 未接线的语义**（R3·3 那条 warn）：不是禁止——执行器在每个 T 节点
发 `NeedsOperator` 事件并暂停，等 `POST /runs/{id}/resume`。手动设温也能跑
完整棵树，接上 331 之后这个节点自动化，API 不变。

成本估计从 journal 的历史 settle 时间来（R3·3 的逐温度表），ETA 随实测重算。

## 动手之前先在 core 里清掉（顺序即优先级）

1. **`current_sign = -1` 进 rig.toml**，乘在 `Infiniium._fetch`，同时进协议和
   模拟器——handover 的「下一程第一件事」，也是符号问题的正解。
2. **第四批 14**：事件信封/层级标签（`Progress` 加 `node_path`）在 core 定，
   不在服务层拼。
3. **第二批 8**：`configure_trigger` 进 run 路径——服务层不能依赖
   `tools/scan.py` 的预检副作用。
4. **第三批 10–11**：参数来源（default / run.toml / last-used / edited /
   inherited）——`/modules` 的 provenance 直接消费它。
5. 第四批 15–17（`LedSource` 协议、删 `core/sequence.py`、拆 `checks.py`）
   顺路做，不挡路。
6. （2026-09-02 补记，实际做了、计划里没列的）`run_jv` 光曲线开快门、暗曲线
   关快门并 yield `InstrumentState({"shutter"})`；`run_intensity_series` 在
   `measure_dc` 周围开快门；`storage/jv.py` 因此升到 `bace-jv/2`。都是
   ui-brief 01-modules §4 记过的时序缺口，不是重写测量逻辑；`bace-run/2` 不变。
   审阅轮又加了 `run_transient_scan` 每段 yield `StepPhase`（同样的仪器调用、
   同样的顺序，只是多了 yield）。

## 分期

- **P0** 上面的 core 清理。
- **P1** bench tab 能用：单模块 run + 事件流 + journal + `/bench` 读与动作 +
  参数 provenance。用 `--sim`（现有 simulated 驱动）可以离台开发。
- **P2** pipeline tab：validate / dry run / 执行器（三条绑定 + NeedsOperator）+
  成本模型。
- **P3** results tab（尚未设计）、失败恢复、331 接线后的 temperature 自动化。

## 明确不做

不重写测量逻辑；不做任何自动修正与解读；不做多客户端写（单操作者，锁在
bench）；不做鉴权（127.0.0.1）；分析（intensity-dependent 结果）等 results tab
设计定了再说。
