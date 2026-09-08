# NavKit 控制台

模块无关的截图校准工作台：浏览模块运行时落盘的 debug 截图会话，框选/校准 ROI、裁剪模板、
调整阈值、测试匹配分与跨帧稳定性，保存回模块的 `treasure_assets.json`（schema v3 唯一真源；旧 `treasure_rois.json` 已于 M4 退役归档到仓库根 `archive/treasure_v2/`）。

架构 = **通用 server（`server.py`）+ 模块 adapter（`adapters/*.py`）**：
server 只做会话浏览 / 分类读写 / 模板匹配的通用路由；领域能力（如鉴宝的 OCR、彩蛋识别）
由 adapter 注册为领域端点。前端 API 契约对所有模块一致。

## 快速开始

| 方式 | 命令 |
|---|---|
| 双击（默认 treasure） | `tools/navkit/start.cmd` |
| PowerShell 脚本 | `powershell -ExecutionPolicy Bypass -File tools/navkit/start.cmd -Module treasure [-Port 8765]` |
| 手动 | `.venv\Scripts\python.exe tools\navkit\server.py --module treasure --port 8765` |

- 启动脚本流程：优先用项目 `.venv` 的 python（cv2 依赖齐全）→ 若端口已被监听则直接开浏览器复用现有实例 → 否则以独立进程启动 server（日志在 `%TEMP%\debug_studio_<端口>.{out,err}.log`）→ TCP 探活就绪后自动打开浏览器。
- server 是**常驻进程**，命令行不退出是预期行为；停止 = 结束对应 python 进程。
- 手动运行务必用 `.venv` 的 python，PATH 上的系统 python 通常缺 `cv2`。

## 目录结构

```
tools/navkit/
├── server.py            # 通用后端：通用路由 + adapter 领域端点转发
├── start.cmd            # 双击启动入口（module=treasure）
├── core/                # 模块无关能力
│   ├── session.py       # 会话/截图浏览（白名单正则，防目录穿越）
│   ├── categories.py    # 分类定义 + save_atomic（临时文件 + os.replace，校验通过才落盘）
│   ├── reader.py        # 模板灰度缓存 + match_local 多尺度匹配（与运行时同源实现）
│   └── renderer.py      # 灰度/BGR → base64 dataURL 预览
├── adapters/
│   └── treasure.py      # 鉴宝 adapter：类别清单/路径布局/缺省项/领域端点（OCR/彩蛋）
└── static/              # 前端三件套（index.html / style.css / app.js）
```

## 数据流与唯一真源

```
模块运行 ──截图──→ %APPDATA%\MaaRacingAssistant\debug\treasure\<时间戳会话>\*.png
                          │
调试台打开会话 ──框选/调阈值──→ 原子保存
                          │
                          ▼
    plugins/treasure/resources/config/treasure_assets.json   ←—— 唯一真源（schema v3）
                          │
模块下次启动 ──加载──→ ROI / 模板列表 / 阈值立即生效
```

- 调试台 `match_local` 与运行时 `detector._match_local` 是同一套多尺度匹配实现，
  **调试台看到的匹配分 = 运行时的匹配分**，校准所见即所得。
- 会话根目录必须与模块写盘目录一致（`user_data_dir()/debug/<module>`）；
  旧版 `PROJ/debug/treasure` 与用户数据目录解耦，已废弃。

## ROI 文件 v2 schema 速览

```jsonc
{
  "_schema_ver": 2,
  "reference_size": [1280, 720],
  "stage":   { "<key>": { "rect": [x1, y1, x2, y2], "templates": ["xxx.png"], "threshold": 0.9 } },
  "actions": { "...": {} }, "ocr": { "...": {} },
  "appraisers": { "...": {} }, "eggs": { "...": {} }
}
```

- `rect` 全部为归一化坐标 [0,1]，左上原点，`x2/y2` 为排他边界。
- 分类段（stage/actions/ocr/appraisers/eggs）由各模块 adapter 声明，缺省项在首次启动时幂等补填。

## 重要边界：调试台是几何校准器，不是语义编辑器

调试台**只按 key 读写 rect/templates/threshold，不理解任何领域语义**。

- 「这个 ROI 属于哪个阶段 / 优先级 / 多模板互斥策略」= 模块代码私有
  （鉴宝：`plugins/treasure/detector.py` 的 `_ROI_STAGE` + `module.py` 的 `_STAGE_PERCEPTION`）。
- 改语义 → 必须改代码（走 review）；改几何/阈值 → 调试台点两下即可，不碰代码。
- 语义字段在 JSON 里根本不存在，想乱也乱不起来。

## HTTP API 一览

通用 GET：
`/api/list_sessions`、`/api/list_images?session=`、`/api/list_templates`、
`/api/template_status`（未引用/悬空模板检查）、`/api/image?session=&name=`、
`/api/template?name=`、`/api/rois`

通用 POST：
`/api/rois`（原子保存）、`/api/template_upload`、`/api/crop_to_template`、
`/api/match_score`、`/api/cross_frame_test`（跨帧分数直方图 + 分位数）

treasure 领域 POST（adapter 注册）：
`/api/ocr_recognize`（RapidOCR 单 ROI 识别 + ROI 尺寸建议）、
`/api/eggs_recognize`（彩蛋图标匹配 + 计数 OCR）

所有图片/模板 API 只接受白名单相对名；ROI 保存校验失败返回 400 不落盘。

## 新模块接入（以 racing 为例，共 4 步）

1. 新建 `plugins/racing/resources/racing_rois.json`（同 v2 schema，racing 自己的 key）。
2. racing 模块代码内定义自己的阶段语义（`_ROI_STAGE` / `_STAGE_PERCEPTION` 等价物）。
3. 新建 `tools/navkit/adapters/racing.py`，声明：
   `CATEGORIES`、`make_category_defs()`、`rois_path()`、`session_dir()`
   （= `user_data_dir()/debug/racing`）、`template_dir()`，可选 `register_endpoints(state)`
   注册领域端点（复用 `adapters/treasure.py` 的写法即可）。
4. `server.py` 的 `_load_adapter()` 加分支；启动脚本传 `-Module racing`。

调试台 core、前端、启动脚本**零改动**。

## 常见问题

- **端口被占用**：脚本检测到端口已监听会直接开浏览器连现有实例，不会二次起服务。
- **读不到会话**：先确认模块跑过且落盘；会话根 = `%APPDATA%\MaaRacingAssistant\debug\treasure`，
  与模块 `user_data_dir()/debug/treasure` 严格一致。
- **模板状态检查**：`template_status` 的 `unassigned` = 模板存在但没被任何 ROI 引用；
  `dangling` = ROI 引用了但不存在的模板文件。
- **测试**：`pytest tests/test_navkit_studio_core.py tests/test_navkit_studio_server.py`
