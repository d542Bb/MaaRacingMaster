#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NavKit v4 策略表薄页——独立小工具（P3c，C 形态：与 MPE 并列，无壳无嵌套）。

作用：以表格形式读/改 `treasure.policy.json` 的 `policy.rules`（id / when 条件 /
decision.key / decision.hint），保存回写真源原子替换。字段契约以
tools/navkit/schema/ 三件套为准，本工具**不内嵌第二套 schema 理解**——只做
展示 + 基础字段编辑，深层校验交给 `check_truth.py`（图闭合+数据面装配+交叉互洽）。

用法：
    python policy_server.py [--port 26530]   # 默认 26530，与 mpelb 端口错开
浏览器打开 http://localhost:26530 即见薄页。

写盘安全：PUT 校验 JSON 合法后 temp + os.replace 原子替换；非法 JSON 拒绝。
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 真源 policy 表（仓库内绝对定位，兼容从任意 cwd 启动）
POLICY_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "policy" / "treasure.policy.json"
)

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>NavKit v4 策略表</title><style>
body{font-family:system-ui,sans-serif;margin:16px;background:#161b22;color:#e6edf3}
h1{font-size:18px;margin:0 0 8px} .bar{display:flex;gap:8px;align-items:center;margin-bottom:8px}
button{background:#238636;color:#fff;border:none;padding:6px 12px;border-radius:6px;cursor:pointer}
button.ghost{background:#30363d} table{border-collapse:collapse;width:100%}
th,td{border:1px solid #30363d;padding:4px 8px;font-size:13px;vertical-align:top}
th{background:#21262d;position:sticky;top:0}
input,textarea{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:2px 4px;width:100%;box-sizing:border-box}
.when{color:#9da7b3;font-family:monospace;font-size:11px;white-space:pre-wrap}
#msg{min-height:18px;font-size:13px} .ok{color:#3fb950} .err{color:#f85149}
</style></head><body>
<h1>NavKit v4 策略表 · <span id="path"></span></h1>
<div class="bar">
  <button onclick="load()">重新加载</button>
  <button onclick="addRow()" class="ghost">新增规则</button>
  <button onclick="save()">保存</button>
  <span id="msg"></span>
</div>
<table><thead><tr>
  <th style="width:150px">id</th><th style="width:34%">when（只读）</th>
  <th style="width:120px">decision.key</th><th>decision.hint</th><th style="width:44px"></th>
</tr></thead><tbody id="tb"></tbody></table>
<script>
let data=null;
async function api(path,opts){ const r=await fetch(path,opts); const t=await r.text();
  if(!r.ok){ setMsg(t,'err'); throw new Error(t);} return t?JSON.parse(t):null; }
function setMsg(m,ok){ const e=document.getElementById('msg'); e.className=ok||'ok'; e.textContent=m; }
function render(){
  const tb=document.getElementById('tb'); tb.innerHTML='';
  data.policy.rules.forEach((r,i)=>{
    const tr=document.createElement('tr');
    let td=document.createElement('td'); const id=document.createElement('input'); id.value=r.id; id.disabled=true; td.appendChild(id); tr.appendChild(td);
    td=document.createElement('td'); td.className='when'; td.textContent=JSON.stringify(r.when??{}); tr.appendChild(td);
    td=document.createElement('td'); const k=document.createElement('input'); k.value=r.decision?.key??''; k.oninput=e=>saveKey(i,e.target.value); td.appendChild(k); tr.appendChild(td);
    td=document.createElement('td'); const h=document.createElement('textarea'); h.rows=2; h.value=r.decision?.hint??''; h.oninput=e=>saveHint(i,e.target.value); td.appendChild(h); tr.appendChild(td);
    td=document.createElement('td'); const b=document.createElement('button'); b.className='ghost'; b.textContent='删'; b.onclick=()=>{ data.policy.rules.splice(i,1); render(); }; td.appendChild(b); tr.appendChild(td);
    tb.appendChild(tr);
  });
}
function saveKey(i,v){ const r=data.policy.rules[i]; r.decision=r.decision||{}; r.decision.key=v; }
function saveHint(i,v){ const r=data.policy.rules[i]; r.decision=r.decision||{}; r.decision.hint=v; }
function addRow(){ data.policy.rules.push({id:'new_rule',when:{},decision:{key:'',hint:''}}); render();
  setMsg('已新增一行（id 留 new_ 前缀占位，保存前请改名）',''); }
async function load(){ try{ data=await api('/api/policy'); document.getElementById('path').textContent='policy.rules = '
  + data.policy.rules.length + ' 条'; render(); setMsg('已加载',''); }catch(e){ setMsg('加载失败: '+e.message,'err'); } }
async function save(){ setMsg('保存中...','');
  try{ const need=new Set(['id','when','decision']); for(const r of data.policy.rules){
    if(r.id&&r.id.startsWith('new_')) throw new Error('存在未命名规则 id='+r.id+'，请先命名'); }
    await api('/api/policy',{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(data)}); setMsg('已保存。深层校验请跑: tools\\navkit\\check_truth.py','ok');
  }catch(e){ setMsg('保存失败: '+e.message,'err'); } }
load();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _txt(self, code, text, ctype="text/html; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._txt(200, PAGE)
        elif self.path == "/api/policy":
            try:
                self._json(200, json.loads(POLICY_FILE.read_text(encoding="utf-8")))
            except Exception as e:  # noqa: BLE001
                self._txt(500, f"读取失败: {e}")
        else:
            self._txt(404, "not found")

    def do_PUT(self):
        if self.path != "/api/policy":
            self._txt(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            doc = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            self._txt(400, f"非法 JSON: {e}")
            return
        # 原子写盘：temp + 校验通过再 replace；保留原文件权限
        fd, tmp = tempfile.mkstemp(dir=POLICY_FILE.parent, suffix=".tmp", prefix=".policy-")
        mode = POLICY_FILE.stat().st_mode if POLICY_FILE.exists() else 0o644
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, POLICY_FILE)
            os.chmod(POLICY_FILE, mode)
        except Exception as e:  # noqa: BLE001
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self._txt(500, f"写盘失败: {e}")
            return
        self._json(200, {"status": "ok", "rules": len(doc.get("policy", {}).get("rules", []))})

    def log_message(self, *a):  # 静默访问日志
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=26530)
    args = ap.parse_args()
    if not POLICY_FILE.exists():
        print(f"[policy] 真源不存在: {POLICY_FILE}")
        return 1
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[policy] 策略表薄页 http://127.0.0.1:{args.port}  <- {POLICY_FILE}")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())