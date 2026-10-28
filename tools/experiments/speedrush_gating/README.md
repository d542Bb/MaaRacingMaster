status: active

# speedrush_gating — 门控判定帧取证 + 录制节拍三拆

两个脚本都不产新结论，是两条**已定案**结论的取证 / 量测工具；补这份 README 是因为此前
目录无索引，脚本只在 docstring 里说明问什么（2026-10-28）。

## `export_gating_marks.py` — 判定落在什么画面上

把门控日志里的判定帧号导成可直接看的画面拼图，回答「这次判定判早了还是判晚了」。

为什么必须用帧号：`logger` 的时间戳只到秒且是墙钟，录制器用 `perf_counter_ns`，两个时基
对不上；唯一的共同坐标是帧号——`plugins/speedrush/module.py` 的 `_frame_note` 把 `frame=`
写进每条门控判定日志。被 `plugins/speedrush/CODE_WIKI.md` 当门控校准工具点名引用。

## `probe_frame_pace.py` — 帧率拆成三个数

把「帧率」拆成有效帧率 / 帧间隔中位 / **P95** 三个量，并定位瓶颈在**源**、**写盘**还是
**主循环**——三者修法完全不同，只看平均 fps 会把信息全丢掉。

结论已入 `plugins/speedrush/CODE_WIKI.md` §2.1「录制帧率不是独立旋钮 = 主循环迭代率」
（2026-09-18，九场 5586 帧）：有效 15.1~15.3 fps、帧间隔中位 49.9 ms、P95 200 ms、
慢间隔每 8 帧必来一次。本脚本留作升版 / 换机后的复测仪器。
