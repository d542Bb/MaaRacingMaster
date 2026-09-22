# egg_lab —— 员工守则彩蛋调试试验区（重建）

status: active

## 目的

生产实现（6a048c2）用户报告有 bug、症状待述。本区承载**当前生产代码的逐行同构副本**
（常量 / `wireEggDodge` / `showRulesDialog` / `openModal`+`onClose` / 掉落逻辑全部照搬），
供复现、打补丁、自检；确认后修回生产并再次退役。

## 自审已发现的嫌疑（lab 内已修，标注 `[lab-fix]`）

1. **躲避越界**：`.mra-modal-actions` 是 `justify-content: flex-end` 的右对齐行，原
   `room = (行宽-按钮宽)/2 - inset` 按居中假设计算——按钮向右最多可越出弹层右缘
   约 (行宽-按钮宽)/2 - inset 像素，飞进暗区。修法：按按钮在行内的**实际 offsetLeft**
   算不对称余量（左移空间 = offsetLeft - inset；右移空间 = 行宽 - offsetLeft - 按钮宽 - inset）。
2. **按钮宽度漂移**：taunt 换字会改变按钮布局宽度，flex-end 下左缘整体跳动，translate
   叠加后视觉上"瞬移"。修法：接线时钉住 `min-width` = 初始 offsetWidth。
3. （顺带）躲避余量改为**每帧实测**（按钮 offsetLeft/宽度实时取），文案换字后边界恒正确。

## 自检

demo 页「跑自检」按钮断言：抽样形状（8 条不重复 + 0/末条固定）、掉落混入频率（25 点 5 中）、
onClose 单次触发、躲避状态机（4 次后 relent、放行文案）、**越界断言**（扫一串指针位，
按钮右缘不得越过操作行右缘 - inset）。

## 退出计划

修复并入生产（pathspec）→ 结论迁 home（app.js 注释）→ 仪器留档（git 历史）→ 删目录。
