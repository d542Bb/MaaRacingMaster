// 极速狂飙回合战绩看板 · 形态预览 fixture
// data / no_opponent / phase1_only 的数值来自 2026-11-05 本机两局实测。
// many 是分页演示用的合成行，不是实测数据。
// 行的 leading 是回合开始时间；group 是日期分组标签。
window.SPEEDRUSH_PAYLOADS = {
  data: {
    version: 1,
    title: '回合战绩',
    blocks: [
      {
        type: 'stats',
        items: [
          { label: '今日回合', value: '2' },
          { label: '今日胜场', value: '0' },
          { label: '总回合数', value: '2' },
          { label: '总胜场', value: '0' }
        ]
      },
      {
        type: 'list',
        page_size: 5,
        rows: [
          {
            group: '今日',
            leading: '19:57',
            parts: [
              { text: '深度 · ', tone: 'muted' },
              { text: '6,899', tone: 'info' },
              { text: ' / ', tone: 'muted' },
              { text: '8,929', tone: 'negative' }
            ],
            trailing: '负 -2,030'
          },
          {
            group: '今日',
            leading: '19:55',
            parts: [
              { text: '深度 · ', tone: 'muted' },
              { text: '5,693', tone: 'info' },
              { text: ' / ', tone: 'muted' },
              { text: '7,858', tone: 'negative' }
            ],
            trailing: '负 -2,165'
          }
        ]
      },
      { type: 'text', text: '胜负按终盘比分判断。' }
    ]
  },
  empty: {
    version: 1,
    title: '回合战绩',
    blocks: [
      { type: 'text', text: '还没有回合记录。' }
    ]
  },
  no_opponent: {
    version: 1,
    title: '回合战绩',
    blocks: [
      {
        type: 'stats',
        items: [
          { label: '今日回合', value: '2' },
          { label: '今日胜场', value: '—' },
          { label: '总回合数', value: '2' },
          { label: '总胜场', value: '—' }
        ]
      },
      {
        type: 'list',
        page_size: 5,
        rows: [
          { group: '今日', leading: '19:57', text: '深度 · 对手分未记录', trailing: '—' },
          { group: '今日', leading: '19:55', text: '深度 · 对手分未记录', trailing: '—' }
        ]
      }
    ]
  },
  phase1_only: {
    version: 1,
    title: '回合战绩',
    blocks: [
      {
        type: 'stats',
        items: [
          { label: '今日回合', value: '1' },
          { label: '今日胜场', value: '—' },
          { label: '总回合数', value: '1' },
          { label: '总胜场', value: '—' }
        ]
      },
      {
        type: 'list',
        page_size: 5,
        rows: [
          {
            group: '今日',
            leading: '19:57',
            badge: { text: '未完成', tone: 'warn' },
            text: '深度 · 阶段1',
            trailing: '2,233 分'
          }
        ]
      }
    ]
  },
  error: {
    version: 1,
    title: '回合战绩',
    badge: { text: '读取失败', tone: 'warn' },
    blocks: [
      { type: 'text', text: '稍后自动重试。' }
    ]
  },
  many: {
    version: 1,
    title: '回合战绩',
    blocks: [
      {
        type: 'stats',
        items: [
          { label: '今日回合', value: '2' },
          { label: '今日胜场', value: '0' },
          { label: '总回合数', value: '12' },
          { label: '总胜场', value: '2' }
        ]
      },
      {
        type: 'list',
        page_size: 5,
        rows: [
          { group: '今日', leading: '19:57', parts: [{ text: '深度 · ', tone: 'muted' }, { text: '6,899', tone: 'info' }, { text: ' / ', tone: 'muted' }, { text: '8,929', tone: 'negative' }], trailing: '负 -2,030' },
          { group: '今日', leading: '19:55', parts: [{ text: '深度 · ', tone: 'muted' }, { text: '5,693', tone: 'info' }, { text: ' / ', tone: 'muted' }, { text: '7,858', tone: 'negative' }], trailing: '负 -2,165' },
          { group: '11-04', leading: '21:08', parts: [{ text: '深度 · ', tone: 'muted' }, { text: '7,120', tone: 'info' }, { text: ' / ', tone: 'muted' }, { text: '6,890', tone: 'negative' }], trailing: '胜 +230' },
          { group: '11-04', leading: '21:05', parts: [{ text: '直行 · ', tone: 'muted' }, { text: '5,870', tone: 'info' }, { text: ' / ', tone: 'muted' }, { text: '7,850', tone: 'negative' }], trailing: '负 -1,980' },
          { group: '11-04', leading: '20:40', parts: [{ text: '深度 · ', tone: 'muted' }, { text: '5,990', tone: 'info' }, { text: ' / ', tone: 'muted' }, { text: '7,490', tone: 'negative' }], trailing: '负 -1,500' },
          { group: '11-04', leading: '20:36', parts: [{ text: '深度 · ', tone: 'muted' }, { text: '6,402', tone: 'info' }, { text: ' / ', tone: 'muted' }, { text: '7,122', tone: 'negative' }], trailing: '负 -720' },
          { group: '11-04', leading: '20:31', parts: [{ text: '直行 · ', tone: 'muted' }, { text: '5,610', tone: 'info' }, { text: ' / ', tone: 'muted' }, { text: '7,810', tone: 'negative' }], trailing: '负 -2,200' },
          { group: '11-03', leading: '20:10', parts: [{ text: '深度 · ', tone: 'muted' }, { text: '7,050', tone: 'info' }, { text: ' / ', tone: 'muted' }, { text: '6,900', tone: 'negative' }], trailing: '胜 +150' },
          { group: '11-03', leading: '20:05', parts: [{ text: '宏 · ', tone: 'muted' }, { text: '6,155', tone: 'info' }, { text: ' / ', tone: 'muted' }, { text: '7,135', tone: 'negative' }], trailing: '负 -980' },
          { group: '11-02', leading: '19:06', parts: [{ text: '深度 · ', tone: 'muted' }, { text: '5,788', tone: 'info' }, { text: ' / ', tone: 'muted' }, { text: '7,528', tone: 'negative' }], trailing: '负 -1,740' },
          { group: '11-02', leading: '19:02', parts: [{ text: '深度 · ', tone: 'muted' }, { text: '6,021', tone: 'info' }, { text: ' / ', tone: 'muted' }, { text: '7,126', tone: 'negative' }], trailing: '负 -1,105' },
          { group: '11-02', leading: '18:58', parts: [{ text: '深度 · ', tone: 'muted' }, { text: '5,902', tone: 'info' }, { text: ' / ', tone: 'muted' }, { text: '7,342', tone: 'negative' }], trailing: '负 -1,440' }
        ]
      },
      { type: 'text', text: '胜负按终盘比分判断。' }
    ]
  }
};
