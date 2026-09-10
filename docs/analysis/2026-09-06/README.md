# 2026-09-06 — 示波器平均器缺陷，与 220–295 K 扫描的分析

样品 260826-6 / 6，PTQ10:ITIC，pxa。所有原始数据在
`Q:\Huotian\bace-python\service-layer-dev-04d292\runs\`。

## 1. "第三个点电荷特别少" —— 平均器从未清零

凌晨 01:24–01:31 的四次 3 点 vpre 扫描（`260826-6_*_20260906_01xxxx`）里，第三点的 Q 只有
前两点的 1/3。诊断（`averager-bug-*.png`）：

- `averages_dark = averages_light + ~27`：`:WAV:COUN?` 累计，DSO9054H 的平均器在 light 和
  dark 之间没有清零；LabVIEW 的"双重配置"（COUN 64 → RUN → COUN 200）、`:STOP`/`:RUN`
  都不清零，只有 autorange **改变量程**时才清。
- 量程落在 2 % 死区内的步（多为第三步）把上一步的 dark 缓冲叠进了 light 曲线
  （`averager-bug-light-dark-differences.png`：light2 − light0 是一个正向瞬态）。
- 因此每条 dark 里约 40 % 是 light；"正常"点读到真值的约 60 %，丢了重启的点读到 20–35 %。
  `Q ≈ 25 / count`。

`probe_averager_20260906_030246.txt`（`tools/probe_averager.py` 在实验电脑上的实测）：

| 操作 | 计数 | 结论 |
|---|---|---|
| `:STOP` / `:RUN` | 52 → 125 | 续叠 |
| COUN 64 → RUN → COUN 200 | 52 → 125 | 续叠 |
| 重写相同量程 | 125 → 191 | 续叠 |
| 改变量程 | 191 → 47 | 清零 |
| `:CDIS` | 200 → 0 | 清零 |
| AVER OFF → 单次 → AVER ON | 1 → 0 | 清零 |
| `:DIG` + `*OPC?`（32 次） | 0.42 s 后 32 | 等满 |
| 运行中读 `:WAV:COUN?` | 一直答 200 | 答的是设定值 |

修法（PR #59、#60，`bace/drivers/infiniium.py`）：每次采集前 STOP → AVER OFF → `:CDIS` →
AVER ON，停止状态下读回计数必须为 0；autorange 每趟单次 `:DIG`；正式采集 `:DIG;` +
`*OPC?`，取数后核对计数；`configure_edge_trigger` 保持触发源通道显示打开（`:DIG CHAN2`
会关掉其它通道的显示）。验证 run 032529-001 / -003：三点 Q = −5.58 / −5.81 / −5.69 e−10 C，
200/200 次，修复前同条件读到 −3.5 / −3.5 / −1.2 e−10。

**修复前的所有 BACE 数据 Q 偏小 35–40 %；9 月 2 日与 LabVIEW 的一致性不能作为绝对值依据
（LabVIEW 同样等 `:ADER?`）。**

## 2. 升温 pipeline 03:50–07:29（`pipeline_20260906_035040`）

220 → 295 K 步 5 K × LED 1.010–1.030 V 五档 × (jv_bace + bace)，bace 为 vpre = Voc ± 1 mV
三点，vcoll −4 V，**delay 0 ns**（bench 上 03:41 从 90 改到 0 的 edited 值）。逐格数据见
`pipeline-220-295K-cells.txt`，图见 `pipeline-220-295K-overview.png`。

- 光强每档全程波动 ≤2.4 %，格内 ≤0.3 %；J–V 期间 86.9–90.0 µW。光强不是变量。
- −Q 从 0.66 nC（220 K）单调升到 1.31 nC（295 K）；Voc 1.113 → 1.017 V 线性；峰值电流
  4.5 → 11.4 mA。
- 温度读数除第一点外均为 x.1 K：331 从下方接近时在设定值上方 0.1–0.15 K 进入 0.2 K 容差带。

### 285–295 K 的 Q 饱和（`pipeline-220-295K-saturation.png`）

仪器侧逐项排除：瞬态在 1.0 µs 内完成（窗内 98 %，窗后 <0.001 nC，窗前漏 ≤1 %）；无削顶，
分辨率 1.5–1.6 µA；基线差 ≤0.02 mA；光/暗尖峰幅度差 0.23 → 0.66 mA、时间差
0.14 → 0.46 ns 随温度单调增长（89 条 "trigger jittered" 警告是规则误判），差分积分约
0.005 nC；叠加 200/200。场实际到达约 235 ns，比 `trigger_offset_s` 假设的 246.6 ns 早约 10 ns
（47.1 ns 是 delay = 90 时校准的），影响 ≤1 %。

**这一节的解释在 2026-09-07 被修正**：见 `spike-lag-and-dark-charge.md`。饱和不是
"某个与光无关的分量主导"这么简单——关快门对照显示**光生电荷本身在 250 K 达峰、
到 290 K 只剩一半**，而注入电荷同时涨了三倍，饱和是两者交叉。下面的 α 与 dQ/dVpre
仍然成立，"电路 RC" 的解释作废。

指向物理的信号：

| T / K | α = dlnQ/dlnI | dQ/dVpre |
|---|---|---|
| 220 | 0.18 | 0.3 %/mV |
| 250 | 0.13 | 0.05 %/mV |
| 280 | 0.09 | 1.7 %/mV |
| 295 | 0.01 | 2.4 %/mV |

Q(T) 翻倍而 Jsc 同档只增约 30 %；室温下 Q 与光强无关却随 Vpre 以 exp(qV/kT) 的斜率变化；
9 月 5 日 vpre = 0（light/dark 电压区间重合）时 Q 仅 0.02 nC。结论：室温下 Voc 处的电荷由一个
与光无关的分量主导——器件的暗电荷（掺杂/浅陷阱，热激活）或平移暗参考的电容失配
（∫C(V)dV 在 [−4, Voc] 与 [−4−Voc, 0] 不等）。区分实验：`light_shutter = shut` 的 bace
（recipe `T220-295K_led1010-1030mV_jvbace_bace-shutter-shut`）、`dark_reference = same`、
以及 295 K 与 220 K 的 vpre 宽扫（Voc ± 20 mV）。

## 3. 续篇

`spike-lag-and-dark-charge.md`（2026-09-07）：控制台那条 "spikes are 0.46 ns apart"
警告的排查，三个检验证明它是误报；以及关快门对照把 Q 拆成光生与注入两部分的结果。
图 `spike-lag-diagnosis.png`、`spike-lag-explained.png`（其中面板 D/E/F 的解释已被
`spike-lag-revised.png` 取代）。
