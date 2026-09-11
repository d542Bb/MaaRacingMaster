# v4 节拍探针：`rate_limit` 与 `jump_back` 回弹闭环

## 问的问题

1. `treasure.policy_loop.rate_limit = 300` 与真机记录「v4 实际帧间隔约 125ms」互相矛盾（300 是下限），
   到底谁在决定决策帧的节拍？
2. 框架"为选中兜底节点而截图的那一帧"与"决策段在 CustomAction 内自己再取的帧"是不是同一帧？

## 怎么测

`pacing_probe.py` 不连游戏窗口、不读仓库真源，合成一张与项目同形态的最小图：

```
exp.boot  next=[exp.dwell]                                   rate_limit=50,  timeout=-1
exp.dwell next=[exp.act, {exp.policy_loop, jump_back:true}]  rate_limit=RATE_DWELL, timeout=-1
exp.act   recognition=Custom EXP_NoHit                       永不命中 → 逼出兜底位
exp.policy_loop  action=Custom EXP_Policy, next=[]           rate_limit=RATE_POLICY, pre/post_delay=0
```

截图侧是与 `core/nav_graph.py` 的 `WgcapController` 同形的帧注入控制器：只读一份按墙钟推导帧号的
"60fps 虚拟缓存"，过期即抛，不回退。决策段用一个可调的 `sleep` 复现真机 OCR 的 105ms 耗时。

```powershell
.\.venv\Scripts\python.exe tools\experiments\v4-frame-pacing\pacing_probe.py 4
```

## 结果（MaaFw 5.12.3 / Python 3.11）

### 路径一：回弹闭环（兜底位每轮必命中）—— 相邻决策帧间隔

| 组 | dwell.rate_limit | policy_loop.rate_limit | dwell pre/post_delay | 决策段耗时 | 间隔中位 | 次/秒 |
|---|---|---|---|---|---|---|
| A | 600 | 300 | 默认 200 | 105ms | **108.9ms** | 9 |
| B | 600 | 50 | 0 | 105ms | **108.9ms** | 9 |
| C | 50 | 300 | 0 | 105ms | **108.9ms** | 9 |
| D | 600 | 300 | 0 | 105ms | **108.9ms** | 9 |
| E | 600 | 300 | 0 | 0 | **3.5ms** | 290 |
| F | **2000** | **2000** | **500** | 0 | **3.4ms** | 291 |

### 路径二：全 miss 驻留（不走回弹）—— 相邻框架截图轮间隔

| 组 | dwell.rate_limit | 间隔中位 |
|---|---|---|
| G | 600 | **605.9ms** |
| H | 50 | **62.2ms** |
| I | 2000 | ≈2000ms（4s 预算内仅 4 轮，看最大样本 2003.5ms） |

## 结论

1. **`rate_limit` 的工作完全正常，但它的作用域只是「节点自己等后继命中的轮询」。** 路径二里 50/600/2000
   精确对应 62/606/2000ms，无任何旁路。
2. **`jump_back` 回弹闭环旁路父节点的 `rate_limit` / `pre_delay` / `post_delay`。** F 组把三项限速全部拉到
   2000/2000/500，间隔仍是 3.4ms。节拍唯一来源 = 兜底节点动作自身耗时 + ≈3.5ms 框架开销。
   ⇒ 真机那个"约 125ms"就是决策段自身的耗时，与图上写的 300/600 无关；反过来，**改 `rate_limit`
   调不动每帧重判的节奏**。
3. **`pre/post_delay` 在进入节点执行时付一次，回弹循环里不重复付。** 证据：A 组（默认 200）比 D 组
   （0）总 run 少 3~4 次、单次最大截图间隔 417.6ms ≈ 200+200+开销；F 组（500）最大 1014ms ≈ 500+500。
4. **决策段与识别帧基本同帧**：路径一全部组的"决策段自取帧 vs 识别帧"漂移中位 **0**、最大 1（60fps
   周期 16.7ms，决策紧跟选中）。担心的"一轮双时间线"在这条路径上不成立。
   但**决策段工作期间画面推进 6 帧**（105ms / 16.7ms）——真正的错位在"点击落地时判断已过期 ~100ms+"，
   不在取帧口径。

## 结构性风险（未修，待决策）

决策段一旦变轻（例如某阶段整轮 OCR 被跳过），回弹闭环会以 **≈290 次/秒** 自旋：CPU 打满、trace 与决策
契约落盘洪泛。图上的 `rate_limit` 拦不住它。可选修法：`PolicyBridge.run()` 内按单调时间戳自节流，
或在真源上给决策段加"最小帧间隔"元数据并由桥消费。

## 已知现象（非本实验引入）

脚本跑完在解释器退出阶段返回 `0xC0000005`。这是本项目已定案的 binding 句柄生命周期坑（P2b 实验 run13：
binding `__del__` 不等 C++ worker 收摊 → C 层回调悬空），生产侧由 `NavKitV4` 常驻 `self._v4_runner`
+ `stop()` 里 `job.wait()` 规避；探针把句柄留在 `KEEPALIVE` 里仍不能免于解释器 shutdown 期的销毁顺序，
**判读结果时请忽略退出码，看 stdout 表格。**
