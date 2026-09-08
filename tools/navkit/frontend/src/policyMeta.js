// 策略契约镜像与呈现元数据（两层，见 docs/plan/P2_PLAN.md §3）
//
// 层 1 CONTRACT_MIRROR：后端 policy.py 常量的机器可比对镜像。必须保持
// JSON 兼容字面量（双引号、无尾逗号）——tests/test_navkit_policy_meta_sync.py
// 会解析本对象与后端常量做集合比对，漂移即测试失败。
// 层 2 PRESENTATION：中文标签 / 提示文案，纯 UI 呈现，不参与比对；
// 展示顺序由本层自行定义，不绑定后端常量书写顺序。

export const CONTRACT_MIRROR = {
  "FACT_FIELDS": [
    "stage",
    "popup_kind",
    "session_decision",
    "appraiser_decision",
    "bidding_decision",
    "settle_income",
    "clicked_once",
    "retry_count",
    "retry_elapsed",
    "cooldown",
    "daily_high_score",
    "egg_reading",
    "egg_read_done",
    "reward_elapsed",
    "skip_cycle",
    "frame_counter"
  ],
  "OP_WHITELIST": ["eq", "neq", "gt", "gte", "lt", "lte"],
  "DECISION_SOURCES": ["session_decision", "appraiser_decision", "bidding_decision"],
  "EFFECT_WHITELIST": ["popup_cooldown_decr", "settle_skip_retry"],
  "WAIT_KEYS": [
    "stage_waiting",
    "session_waiting",
    "appraiser_waiting",
    "bid_waiting",
    "dividend_waiting",
    "popup_waiting",
    "popup_click_cooldown",
    "popup_high_continue",
    "popup_reward_continue",
    "fatal"
  ],
  "TUNING_KEYS": {
    "perception": [
      "appraiser_search_roi",
      "appraiser_match_threshold",
      "check_match_threshold",
      "session_match_threshold",
      "smart_bid_match_threshold"
    ],
    "policy": [
      "session_start_click_cooldown_frames",
      "click_retry_frames",
      "click_retry_max",
      "settle_skip_retry_frames",
      "settle_skip_retry_max",
      "popup_continue_retry_frames",
      "popup_click_cooldown_frames",
      "daily_high_timeout_frames",
      "egg_ocr_timeout_frames"
    ],
    "execution": ["click_cooldown_s"]
  }
};

// ---------- 层 2：PRESENTATION（纯呈现，不参与契约比对） ----------

export const FACT_LABELS = {
  stage: '阶段',
  popup_kind: '弹窗类型',
  session_decision: '场次决策（上游）',
  appraiser_decision: '鉴宝师决策（上游）',
  bidding_decision: '出价决策（上游）',
  settle_income: '本场收入',
  clicked_once: '已点击过',
  retry_count: '点击重试次数',
  retry_elapsed: '重试已等待帧数',
  cooldown: '弹窗冷却帧',
  daily_high_score: '今日最高积分',
  egg_reading: '彩蛋读取中',
  egg_read_done: '彩蛋读取完成',
  reward_elapsed: '奖励页已等待帧数',
  skip_cycle: '跳过周期（帧%3）',
  frame_counter: '帧计数器',
};

export const OP_LABELS = {
  eq: '等于',
  neq: '不等于',
  gt: '大于',
  gte: '大于等于',
  lt: '小于',
  lte: '小于等于',
};

export const DECISION_SOURCE_LABELS = {
  session_decision: '场次决策（session_decision）',
  appraiser_decision: '鉴宝师决策（appraiser_decision）',
  bidding_decision: '出价决策（bidding_decision）',
};

// 值类型顺序即 UI 下拉展示顺序（与契约无关）
export const OP_ORDER = ['eq', 'neq', 'gte', 'gt', 'lte', 'lt'];

export const EFFECT_LABELS = {
  popup_cooldown_decr: '弹窗冷却递减',
  settle_skip_retry: '结算跳过重试',
};

export const TUNING_SECTION_LABELS = {
  perception: '感知（perception）',
  policy: '策略（policy）',
  execution: '执行（execution）',
};

export const TUNING_KEY_LABELS = {
  appraiser_search_roi: '鉴宝师搜索 ROI',
  appraiser_match_threshold: '鉴宝师匹配阈值',
  check_match_threshold: '核验匹配阈值',
  session_match_threshold: '场次匹配阈值',
  smart_bid_match_threshold: '智能出价匹配阈值',
  session_start_click_cooldown_frames: '场次起始点击冷却帧',
  click_retry_frames: '点击重试间隔帧',
  click_retry_max: '点击重试上限',
  settle_skip_retry_frames: '结算跳过重试间隔帧',
  settle_skip_retry_max: '结算跳过重试上限',
  popup_continue_retry_frames: '弹窗继续重试间隔帧',
  popup_click_cooldown_frames: '弹窗点击冷却帧',
  daily_high_timeout_frames: '今日最高读取超时帧',
  egg_ocr_timeout_frames: '彩蛋 OCR 超时帧',
  click_cooldown_s: '点击冷却（秒）',
};

// ---------- 工具：深比较（JSON 值；对象键序无关、数组有序） ----------

export function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a === null || b === null || typeof a !== 'object') return a === b;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) {
    if (a.length !== b.length) return false;
    return a.every((v, i) => deepEqual(v, b[i]));
  }
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every(k => Object.prototype.hasOwnProperty.call(b, k) && deepEqual(a[k], b[k]));
}

// ---------- decision 序列化守卫（P2_PLAN §1.3-A） ----------
// "字段不存在"与"字段为空"不是同一语义：UI state → JSON 只写用户实际填写的
// 字段，禁止补齐 key:"" / fatal:null 之类无语义空值；切换输出源时"保留"仅作用
// 于原本存在的字段。

export function serializeDecision(state, existedFields) {
  const out = {};
  const put = (k, v) => {
    if (v === undefined) return;
    if (existedFields && !existedFields.has(k) && (v === '' || v === null)) return;
    if (v === '' || v === null) return;
    out[k] = v;
  };
  put('key', state.key);
  put('source', state.source);
  put('fatal', state.fatal);
  put('fallback_key', state.fallback_key);
  put('hint', state.hint);
  put('center', state.center);
  put('effect', state.effect);
  return out;
}

// ---------- key 候选集派生（P2_PLAN §1.3-E） ----------
// 合法候选 = anchors 键 ∪ WAIT_KEYS；当前 rule 自身已有 key 若不在合法集合，
// 只作为"当前非法值"特殊项展示（不入全局候选池，防错误值被 UI 洗白）。

export function deriveKeyCandidates(anchorKeys, ruleDecision) {
  const legal = new Set([...(anchorKeys || []), ...CONTRACT_MIRROR.WAIT_KEYS]);
  const options = [
    ...(anchorKeys || []).map(k => ({ value: k, group: '锚点' })),
    ...CONTRACT_MIRROR.WAIT_KEYS.map(k => ({ value: k, group: '保留等待 key' })),
  ];
  const cur = ruleDecision?.key;
  if (cur && !legal.has(cur)) {
    options.push({ value: cur, group: '当前值', invalid: true, label: `⚠ 当前值（后端校验失败）：${cur}` });
  }
  return options;
}

// ---------- when 解析（与后端 _parse_when 对齐的只读投影） ----------
// 返回三元组 [{field, op, value}]：标量 → eq；stage 字符串 → eq（存稳定 ID）；
// 比较对象 → 单算子。仅用于摘要展示与遮蔽比较，不做白名单放行。

export function parseWhen(when) {
  if (!when || typeof when !== 'object') return [];
  const out = [];
  for (const [field, spec] of Object.entries(when)) {
    if (spec === null || ['string', 'number', 'boolean'].includes(typeof spec)) {
      out.push({ field, op: 'eq', value: spec });
    } else if (typeof spec === 'object') {
      const ops = Object.keys(spec);
      if (ops.length === 1) out.push({ field, op: ops[0], value: spec[ops[0]] });
      else out.push({ field, op: ops.join('|'), value: spec });
    }
  }
  return out;
}

// ---------- 遮蔽提示（P2_PLAN §1.3-D：只做可证明正确的子集） ----------
// 复刻范围 = 后端 P06/P07 的两个确定子集，不做数值/算子蕴含推断：
//   ① 完全重复（三元组集合相等）＝ 后端 P07 的判定；
//   ② 前序存在无条件规则（when == {}）＝ 后端 P06 中 prev=∅ ⊆ cur 的判定。
// 同字段不同值、数值宽窄关系（>=3 覆盖 >=5）等一律不提示——少报比错报强。
// 本检测是提示而非判定，权威结果以保存后的后端 report 为准。

function condTriple(c) {
  return `${c.field}\u0000${c.op}\u0000${JSON.stringify(c.value ?? null)}`;
}

export function detectShadowing(rules) {
  const out = []; // [{index, code:'P06'|'P07', refIndex, message}]
  const seen = []; // [{index, triples:Set, empty:boolean}]
  (rules || []).forEach((rule, i) => {
    const conds = parseWhen(rule?.when);
    const triples = new Set(conds.map(condTriple));
    if (conds.length > 0) {
      for (const prev of seen) {
        if (prev.empty) {
          out.push({ index: i, code: 'P06', refIndex: prev.index, message: `被 #${prev.index + 1}（无条件规则，遮蔽其后一切规则）完全遮蔽，永不命中` });
          break;
        }
        if (prev.triples.size === triples.size && [...prev.triples].every(t => triples.has(t))) {
          out.push({ index: i, code: 'P07', refIndex: prev.index, message: `与 #${prev.index + 1} 条件完全重复（本规则永不命中，请删除或修正）` });
          break;
        }
      }
    }
    seen.push({ index: i, triples, empty: conds.length === 0 });
  });
  return out;
}

// ---------- 前端即时校验（后端 P01-P05 / P08 确定性子集的预演） ----------
// 返回 issues: [{level:'error'|'warn', code, path, message, ruleIndex?}]。
// error 级禁用保存（这三类后端必拦，提前挡掉省一次往返）；warn 级不阻断。

export function validateDraft(document) {
  const issues = [];
  if (!document || typeof document !== 'object') {
    return [{ level: 'error', code: 'P01', path: 'document', message: 'document 须为 object' }];
  }
  const policies = document.policies;
  if (!policies || typeof policies !== 'object') {
    return [{ level: 'error', code: 'P01', path: 'policies', message: 'policies 须为 object' }];
  }
  if (policies._schema_ver !== 1) {
    issues.push({ level: 'error', code: 'P01', path: 'policies._schema_ver', message: `需为 1，收到 ${JSON.stringify(policies._schema_ver)}` });
  }
  const anchorKeys = new Set(Object.keys(document.anchors || {}));
  const waitKeys = new Set(CONTRACT_MIRROR.WAIT_KEYS);
  const policyTuning = (policies.tuning && typeof policies.tuning === 'object') ? (policies.tuning.policy || {}) : {};
  const rules = Array.isArray(policies.rules) ? policies.rules : [];
  if (!Array.isArray(policies.rules)) {
    issues.push({ level: 'error', code: 'P01', path: 'policies.rules', message: 'rules 须为数组' });
  }

  rules.forEach((rule, i) => {
    const base = `policies.rules[${i}]`;
    // P02：decision 至少给出 key / source / fatal 之一
    const dec = rule?.decision;
    if (!dec || typeof dec !== 'object') {
      issues.push({ level: 'error', code: 'P01', path: `${base}.decision`, message: 'decision 须为 object', ruleIndex: i });
      return;
    }
    const noOutput = dec.key == null && dec.source == null && dec.fatal == null;
    if (noOutput) {
      issues.push({ level: 'error', code: 'P02', path: `${base}.decision`, message: 'decision 必须给出 key、source 或 fatal 之一', ruleIndex: i });
    }
    // P02：key 悬空（无 source 时，key 须为锚点或保留等待 key）
    if (dec.source == null && dec.key != null && !anchorKeys.has(dec.key) && !waitKeys.has(dec.key)) {
      issues.push({ level: 'error', code: 'P02', path: `${base}.decision.key`, message: `decision.key ${JSON.stringify(dec.key)} 未命中任何锚点，且非保留等待 key`, ruleIndex: i });
    }
    // P03：source 白名单
    if (dec.source != null && !CONTRACT_MIRROR.DECISION_SOURCES.includes(dec.source)) {
      issues.push({ level: 'error', code: 'P03', path: `${base}.decision.source`, message: `source 须为 ${CONTRACT_MIRROR.DECISION_SOURCES.join('/')} 之一`, ruleIndex: i });
    }
    // P05：effect 白名单
    if (dec.effect != null && !CONTRACT_MIRROR.EFFECT_WHITELIST.includes(dec.effect)) {
      issues.push({ level: 'error', code: 'P05', path: `${base}.decision.effect`, message: `effect 须为 ${CONTRACT_MIRROR.EFFECT_WHITELIST.join('/')} 之一`, ruleIndex: i });
    }
    // P08：center 锚点悬空 / 数组形态与越界
    if (dec.center != null) {
      if (typeof dec.center === 'string') {
        if (!dec.center) issues.push({ level: 'error', code: 'P05', path: `${base}.decision.center`, message: 'center 锚点 ID 不能为空字符串', ruleIndex: i });
        else if (!anchorKeys.has(dec.center)) issues.push({ level: 'error', code: 'P08', path: `${base}.decision.center`, message: `引用不存在的锚点 ${JSON.stringify(dec.center)}`, ruleIndex: i });
      } else if (Array.isArray(dec.center)) {
        if (dec.center.length !== 2) {
          issues.push({ level: 'error', code: 'P08', path: `${base}.decision.center`, message: 'center 数组须为 [x, y] 二元组', ruleIndex: i });
        } else {
          dec.center.forEach((v, axis) => {
            if (typeof v !== 'number' || Number.isNaN(v) || v < 0 || v > 1) {
              issues.push({ level: 'error', code: 'P05', path: `${base}.decision.center.${axis === 0 ? 'x' : 'y'}`, message: `坐标 ${axis === 0 ? 'x' : 'y'} 须为 [0,1] 内数字，收到 ${JSON.stringify(v)}`, ruleIndex: i });
            }
          });
        }
      } else {
        issues.push({ level: 'error', code: 'P05', path: `${base}.decision.center`, message: 'center 须为锚点 ID 或 [x, y] 坐标', ruleIndex: i });
      }
    }
    // when：P04 字段白名单 / P05 算子白名单 / P05 @tuning 悬空
    const when = rule?.when;
    if (!when || typeof when !== 'object' || Array.isArray(when)) {
      issues.push({ level: 'error', code: 'P01', path: `${base}.when`, message: 'when 须为 object', ruleIndex: i });
    } else {
      for (const [field, spec] of Object.entries(when)) {
        if (!CONTRACT_MIRROR.FACT_FIELDS.includes(field)) {
          issues.push({ level: 'error', code: 'P04', path: `${base}.when.${field}`, message: '条件字段不在 DecisionFacts 白名单', ruleIndex: i });
          continue;
        }
        // stage 与后端 policy.py 逐条同构：字符串 = 稳定 ID 等值（须在 stage_map 声明）；
        // 对象形态只接受 {prefix: 字符串}，且要求非空 stage_map；其余一律 P04/P05。
        if (field === 'stage') {
          const stageMap = (policies.stage_map && typeof policies.stage_map === 'object' && !Array.isArray(policies.stage_map))
            ? policies.stage_map : {};
          const stageIds = Object.keys(stageMap);
          if (typeof spec === 'string') {
            if (!stageIds.includes(spec)) {
              issues.push({ level: 'error', code: 'P04', path: `${base}.when.stage`, message: `稳定 ID ${JSON.stringify(spec)} 未在 policies.stage_map 中声明`, ruleIndex: i });
            }
          } else if (spec && typeof spec === 'object' && !Array.isArray(spec)) {
            for (const [op, val] of Object.entries(spec)) {
              if (op !== 'prefix') {
                issues.push({ level: 'error', code: 'P05', path: `${base}.when.stage`, message: `stage 条件仅支持 prefix，收到 op=${JSON.stringify(op)}`, ruleIndex: i });
              }
              if (typeof val !== 'string') {
                issues.push({ level: 'error', code: 'P05', path: `${base}.when.stage`, message: 'stage 前缀匹配值须为字符串', ruleIndex: i });
              } else if (op === 'prefix' && stageIds.length === 0) {
                issues.push({ level: 'error', code: 'P04', path: `${base}.when.stage`, message: 'stage 前缀匹配需要非空 stage_map', ruleIndex: i });
              }
            }
          } else {
            issues.push({ level: 'error', code: 'P05', path: `${base}.when.stage`, message: 'stage 条件须为字符串或 {prefix: ...}', ruleIndex: i });
          }
          continue;
        }
        const conds = parseWhen({ [field]: spec });
        for (const c of conds) {
          if (!CONTRACT_MIRROR.OP_WHITELIST.includes(c.op)) {
            issues.push({ level: 'error', code: 'P05', path: `${base}.when.${field}`, message: `非法算子 ${JSON.stringify(c.op)}`, ruleIndex: i });
          }
          if (typeof c.value === 'string' && c.value.startsWith('@')) {
            const name = c.value.slice(1);
            if (!(name in policyTuning)) {
              issues.push({ level: 'error', code: 'P05', path: `${base}.when.${field}.${c.op}`, message: `@tuning 引用 ${JSON.stringify(name)} 未在 policies.tuning.policy 中定义`, ruleIndex: i });
            }
          }
        }
      }
    }
  });

  // 遮蔽提示（守卫 D 子集，warn 级不阻断）
  for (const s of detectShadowing(rules)) {
    issues.push({ level: 'warn', code: s.code, path: `policies.rules[${s.index}]`, message: s.message, ruleIndex: s.index });
  }
  return issues;
}

// ---------- JSON 高级编辑结构守卫（P2_PLAN §4.2，守卫 C） ----------
// JSON 高级编辑是文档编辑器，不是绕过约束的后门：只放行 policies.rules /
// policies.tuning 的修改，其余一律拒绝。返回 {ok, reason?}。

const READONLY_SECTIONS = ['anchors', 'stages', 'transitions', 'routes'];
const PROTECTED_KEYS = ['_schema_ver', '_module'];

export function guardApplyJson(original, parsed) {
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, reason: '顶层须为 object' };
  }
  for (const k of PROTECTED_KEYS) {
    if (!deepEqual(original[k], parsed[k])) {
      return { ok: false, reason: `顶层 ${k} 不可修改（改动 → 拒绝；如确需修改请手工编辑文件）` };
    }
  }
  for (const sec of READONLY_SECTIONS) {
    if (!deepEqual(original[sec], parsed[sec])) {
      return { ok: false, reason: `${sec} 段与当前文档不一致（策略页不改这些段，请到对应视图操作）` };
    }
  }
  const policies = parsed.policies;
  if (policies === null || typeof policies !== 'object' || Array.isArray(policies)) {
    return { ok: false, reason: 'policies 须为 object' };
  }
  if (!Array.isArray(policies.rules)) {
    return { ok: false, reason: 'policies.rules 须为数组' };
  }
  if (policies.tuning === null || typeof policies.tuning !== 'object' || Array.isArray(policies.tuning)) {
    return { ok: false, reason: 'policies.tuning 须为 object' };
  }
  // 守卫 C：stage_map 属 P2 只读结构——可展示、不得修改，修改 → 拒绝应用
  if (!deepEqual(original.policies?.stage_map, policies.stage_map)) {
    return { ok: false, reason: 'policies.stage_map 属只读结构（P2 编辑目标仅 rules / tuning），修改 → 拒绝应用' };
  }
  return { ok: true };
}
