---
status: active
---

# 活动模块下拉重设计试验区（select_lab）

## 协议（沿用日志卡试验区）

- 试验区即原版：`index.html` 复刻生产「活动模块」卡 + 令牌，每步设计改动一个 commit；
- 有问题即回撤（revert 单步），验收通过后并入生产 `frontend/`，删本目录收一个出口 commit；
- 浏览器验证：仓库根起临时 HTTP 服务指向本目录（退出时清理）。

## 目标

现网为原生 `<select>`（Windows 系统渲染的下拉列表与全应用视觉语言脱节）。
参考 OriginUI Select（shadcn/Radix 家族）重设计为自绘 combobox：

- **触发器**：40px 高、radius-md、chevron 开合旋转；
- **面板**：与触发器等宽、浮起阴影、4px 内衬、选项行圆角 hover；
- **选中项**：左侧 checkmark + 加粗（不整行反色——选中是状态不是焦点）；
- **过期项**：灰字 + 「（已过期）」后缀，仍可选中（选中确认弹窗逻辑归 onModuleChange，不动）；
- **键盘/无障碍**：Enter/Space/方向键开合、↑↓/Home/End/typeahead 移动、Enter 选中、
  Esc 归还焦点、aria-expanded/aria-activedescendant、复用全局 :focus-visible 环。

**集成架构**：隐藏原生 `<select id="module-select">` 保持值真源与全部现有读写点
（run.js 读 value、app.js 填充 options、settings.js 切 disabled），自绘层经
`sync()` 单向投影；不建第二份真相。

## 步骤记录

- step 1：OriginUI 风格 combobox 全交互样本（含运行中禁用演示、过期项、键盘导航）。
