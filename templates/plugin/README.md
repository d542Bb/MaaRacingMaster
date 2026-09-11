# 活动插件样板（Plugin Template）

开发新活动插件的起点：本目录是一份**最小完整、可运行骨架**，演示平台对插件的全套契约。

> **为什么放在 `templates/` 而不是 `maaracing_master/plugins/`**：
> [core/registry.py](../../maaracing_master/core/registry.py) 只扫描 `maaracing_master/plugins/*/manifest.py`。
> 样板在 `plugins/` 之外，**不会被主程序识别为活动模块**，GUI 模块列表里也看不到它；
> 复制进 `plugins/<id>/` 并完成三处改名后才会被装载。

## 目录结构

```
templates/plugin/
├── manifest.py          # 清单：ID + MODULE_CLASS（registry 发现入口）
├── __init__.py          # PLUGIN_DIR / RES_DIR 常量（资源自引用）
├── module.py            # SampleModule：ActivityModule 最小完整实现
└── resources/           # 插件专属资源（自包含，随插件分发）
    ├── image/           # 模板图（导航/阶段判定用）
    ├── pipeline/        # MAA Pipeline JSON（可选，任务驱动型活动）
    └── config/          # ROI / 资产配置（可选，建议对齐 NavKit schema v3）
```

## 启用步骤（复制即装）

1. **复制**：把 `templates/plugin/` 整个目录复制为 `maaracing_master/plugins/<你的插件id>/`。
2. **改 `manifest.py`**：`ID = "<你的插件id>"`（与目录名一致，全局唯一）。
3. **改 `module.py`**：类名 `SampleModule` → `<你的模块类名>`，同步 `manifest.py` 的
   `MODULE_CLASS = "module.<你的模块类名>"`；填写 `NAME` / `STAGE_ORDER` / `REQUIRES` / `REQUIRED_ASSETS`。
4. **重启 GUI**：模块下拉自动出现新模块（registry 启动时扫描，无需注册代码）。

> 删除 `plugins/<id>/` 目录即卸载。所有资源引用一律经 `__init__.py` 的常量自引用，
> 不依赖 `assets/` 或其它插件的路径，目录搬走即完整带走。

## 各文件职责

| 文件 | 职责 |
|------|------|
| `manifest.py` | 两行清单：`ID`（唯一标识，GUI 下拉/配置持久化用它）+ `MODULE_CLASS`（`文件名.类名`） |
| `__init__.py` | `PLUGIN_DIR` / `RES_DIR` 路径常量；插件内所有资源路径从这里派生 |
| `module.py` | 模块实现：生命周期（`start`/`stop`/`cleanup`）、阶段状态机、能力取用 |
| `resources/` | 插件专属资源；`REQUIRED_ASSETS` 声明的路径相对插件根目录 |

## 模块类契约速查（来自 `core/base.py`）

| 成员 | 必填 | 说明 |
|------|------|------|
| `ID` / `NAME` | ✅ | 标识与显示名 |
| `STAGE_ORDER` | ✅ | 阶段清单；GUI 阶段列表与断点下拉的唯一来源 |
| `REQUIRES` | ✅ | 需要的可选能力集，启动前校验；`"gamepad"` 需装 ViGEmBus 驱动 |
| `REQUIRES_GAMEPAD_EXCLUSIVE` | ✅ | 是否要求物理手柄断开（虚拟手柄独占） |
| `REQUIRED_ASSETS` | ✅ | 插件自带必需资源，启动前逐项存在性检查 |
| `current_stage` | ✅ | property；GUI 运行状态与断点定位读取 |
| `start(start_from)` | ✅ | 模块入口（独立线程）：`ctx.connect()` → 断点解析 → 主循环 |
| `stop()` | ✅ | `ctx.lifecycle.request_stop()`，主循环 100ms 级响应 |
| `cleanup()` | ✅ | 释放模块自有资源；`enter_context` 登记的资源由 Context 统一释放 |

**主循环铁律**：每帧检查 `ctx.lifecycle.running`；睡眠一律 `ctx.lifecycle.sleep()`，
禁止 `time.sleep`（无法响应停止）；单帧异常自行兜底，不要让一帧失败杀死整个模块。

## 能力门面（`ctx`）

模块与宿主的全部交互走 `ActivityContext` 窄接口，拿不到高权限宿主对象：

| 能力 | 用法 | 可用性 |
|------|------|--------|
| `ctx.capture.screenshot()` | 截图（RGB ndarray，底部 16:9 裁剪，失败返回 None） | 恒可用 |
| `ctx.lifecycle.running / sleep() / request_stop()` | 停止信号 / 可中断睡眠 | 恒可用 |
| `ctx.connect()` | 幂等连接游戏窗口（720p 校验内置） | 恒可用 |
| `ctx.debug_renderer.renderer()` | 调试 HUD 渲染租约（PEEP / 存盘），经 `ctx.enter_context()` 接管 | 恒可用 |
| `ctx.gamepad` | 虚拟手柄（摇杆/按键） | 需 `REQUIRES` 声明 + ViGEmBus |
| `ctx.bind_tasker(tasker, resource)` | MAA 深度绑定（Pipeline 驱动型活动） | 需已连接窗口 |
| `ctx.enter_context(cm)` | 登记资源到生命周期栈，模块退出（含异常）自动释放 | — |

## 常用扩展点

按活动类型从样板增补，均为现成基建：

- **模板匹配 / 归一化 ROI**：`core/template_match.py`、`core/roi_config.py`。
- **导航真源**：NavKit v3 资产 —— `plugins/<id>/resources/config/<id>_assets.json`（模块段）
  + `core/resources/config/global_assets.json`（跨模块共用段=大厅骨架），由 `core/navkit`
  校验与编译，运行期在 Python 侧经 `Assets.load` 直读执行；ROI/资产校准台见 `tools/navkit/`。
- **MAA Pipeline 执行通路**：`core/nav_graph.py`（`NavGraph` + `MRA_Template`/`MRA_Click` 桥），
  当前未接线（无生产实例化点），留作把跳转图交给框架跑的备选实现。
- **MAA Pipeline 任务驱动**：`Resource.post_bundle(RES_DIR)` + 自定义
  `CustomAction`（参考 `core/nav_graph.py` 的桥实现与 docs/MAAFW_GUIDE.md）。
- **目标检测**：`core/yolo_detector.py`（模型放 `resources/onnx/`，进 `REQUIRED_ASSETS`）。
- **调试 HUD**：参考 `core/debug.py` 的渲染分支；`ctx.debug` 会话落盘。

## 打包与分发

无需任何配置：`scripts/release/assemble.ps1` 按 robocopy 整包复制
`maaracing_master/` 目录，`plugins/<id>/`（含资源与模型）自动随包分发。
`REQUIRED_ASSETS` 声明的文件缺失时，启动前会给出插件内具体路径提示。
