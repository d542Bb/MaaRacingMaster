# 公告规范（docs/announcement.json）

> 公告是「用户可感知变化」的通知卡，展示于关于页内嵌卡片（info=公告 / warn=重要）。
> 更新方式：编辑 `docs/announcement.json` 并推送 master。客户端经 GitHub raw（主源）+ jsdelivr CDN（回退）拉取，推送后约分钟级生效；网络异常/解析失败时前端显示「暂无公告」，不影响启动。

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
| `level` | ✅ | 仅 `info` / `warn`（前端仅分这两档；其余值按 info 渲染） |
| `title` | ✅ | 一句话 ≤30 字；空 title 前端显示「暂无公告」 |
| `body` | ❌ | 纯文本，`\n` 分段（CSS `white-space: pre-line` 生效）；禁止 HTML |
| `date` | ✅ | 发布日 `YYYY-MM-DD`，仅作展示 |
| `url` | ❌ | 详情链接（放 release 页等） |
| `url_text` | ❌ | 链接按钮文案，缺省「查看详情」 |
| `effective_until` | ✅ | 过期日 `YYYY-MM-DD` |

## 三条红线

1. **`effective_until` 严格 `YYYY-MM-DD`**：服务端按字符串比较「`< 今天` 即跳过显示」，格式写错（如 `2026/09/30`、`20260930`）会静默永不生效。
2. **title/body 纯文本**：渲染走 `textContent`（天然防 XSS），不要写 HTML 或内联链接；要放链接一律用 `url`/`url_text`。
3. **单文件单公告**：`announcement.json` 是单对象不是数组，新公告直接覆盖旧内容并递增 `id` 序号，不要保留历史公告（历史进 update_log）。

## 生效与失效

- `effective_until` 小于客户端当天日期 → 不显示（无需手动删除文件，可留作档案）。
- 建议有效期 ≤30 天：公告是「当下信息」，长期挂置会稀释关注度。
- 发版公告：与 release tag 同天写入；若提前预告破坏性变更，需在生效当天推送（schema 无起始日字段，不支持预写定时生效）。
