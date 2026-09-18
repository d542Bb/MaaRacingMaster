# 公告规范（docs/announcement.json）

> 公告是「用户可感知变化」的通知卡，展示于关于页内嵌卡片（info=公告 / warn=重要）。
> 更新方式：编辑 `docs/announcement.json` 并推送 master。客户端按 CNB raw（主源）→ GitHub raw →
> jsdelivr CDN（回退）依次拉取，推送后约分钟级生效；网络异常、字段不合规或已过期时前端显示
> 「暂无公告」，不影响启动。

> **信源等级**：L1（正式规范）—— 可作「公告写什么、什么时候发」的直接依据；公告内容真源 = [`announcement.json`](./announcement.json)。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../AGENTS.md)。

## 发布时机

| 场景 | 发不发 | level | 示例 |
|------|--------|-------|------|
| 新版本发布（跟随 release tag） | ✅ 每次 release 一条 | info | "巅峰鉴宝全链路已上线" |
| 破坏性变更 | ✅ 优先 | warn | 配置/路径/驱动依赖变化 |
| 已知问题 + 绕行方案 | ✅ | warn | 高分屏模糊修复/临时方案 |
| 安全/数据风险 | ✅ | warn | 出价上限、余额相关提示 |
| 例行 bugfix 明细 | ❌ | — | 属 `docs/update_log.md` 职责 |
| 开发过程中间态 | ❌ | — | 用户不应看到 |

要点：公告只承载「用户打开关于页时值得看到」的信息；例行修复进 update_log 即可。

## 格式（JSON Schema）

```json
{
  "id": "2026-09-01-v2",
  "level": "info",
  "title": "一句话标题（≤30 字）",
  "body": "正文第一行\n正文第二行（用 \\n 分段，纯文本）",
  "date": "2026-09-01",
  "url": "https://github.com/d542Bb/MaaRacingMaster/releases",
  "url_text": "查看发布说明 ↗",
  "effective_until": "2026-09-30"
}
```

| 字段 | 必填 | 约束 |
|------|------|------|
| `id` | ✅ | `YYYY-MM-DD-<序号>`，改版覆盖时序号递增（如 `-v2`） |
| `level` | ✅ | 仅 `info` / `warn`；值域外**整条无效**（它决定告警配色，出现第三值说明数据不是本项目产的） |
| `title` | ✅ | 一句话 ≤30 字（风格建议，非硬拦）；空 title 或非字符串 → 整条无效 |
| `body` | ❌ | 纯文本，`\n` 分段（CSS `white-space: pre-line` 生效）；禁止 HTML |
| `date` | ✅ | 发布日 `YYYY-MM-DD`，必须是真实存在的日期 |
| `url` | ❌ | 详情链接：**仅 https + 官方域**（`github.com/d542Bb/MaaRacingMaster`、`cnb.cool/MaaRacingMaster`）；不合规则不显示详情按钮 |
| `url_text` | ❌ | 链接按钮文案，缺省「查看详情」 |
| `effective_until` | ✅ | 过期日 `YYYY-MM-DD`，必须是真实存在的日期 |

## 校验口径

客户端拉到的字段一律按不可信输入校验，判据与实现在
[`maaracing_master/core/remote_meta.py`](../maaracing_master/core/remote_meta.py)（只依赖标准库，
回归锁 [`tests/test_remote_meta.py`](../tests/test_remote_meta.py) 会连带校验本仓库实际投放的
`announcement.json`——**校验器收紧后线上公告静默消失**这类事故在 CI 即红）。

- **必填字段不合规 → 整条无效**，该源跳过、继续 fallback；宁可不显示，也不显示半条。
- **可选字段不合规 → 只丢该字段**（例：`url` 不合规就没有详情按钮，正文照常显示）。
- 长度上界（`id` / `title` / `body` / `url_text`）是**安全上界**，比上面的风格建议宽得多：
  超限按不合规处理，避免一条畸形数据把界面撑爆。

## 三条红线

1. **`effective_until` 严格 `YYYY-MM-DD`**：必须是真实存在的日期，服务端按字符串比较
   「`< 今天` 即跳过显示」。格式写错（如 `2026/09/30`、`20260930`）该条整条无效——
   不会有「看着像生效了、其实永不显示」的中间状态。
2. **title/body 纯文本**：渲染走 `textContent`（天然防 XSS），不要写 HTML 或内联链接；
   要放链接一律用 `url`/`url_text`。
3. **单文件单公告**：`announcement.json` 是单对象不是数组，新公告直接覆盖旧内容并递增 `id` 序号，不要保留历史公告（历史进 update_log）。

## 生效与失效

- `effective_until` 小于客户端当天日期 → 不显示（无需手动删除文件，可留作档案）。
- 建议有效期 ≤30 天：公告是「当下信息」，长期挂置会稀释关注度。
- 发版公告：与 release tag 同天写入；若提前预告破坏性变更，需在生效当天推送（schema 无起始日字段，不支持预写定时生效）。
