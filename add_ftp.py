#!/usr/bin/env python3
"""给 net_monitor_web.py 添加 FTP 配置功能"""
import re

with open('net_monitor_web.py', 'r', encoding='utf-8') as f:
    content = f.read()

# ─── 1. 在 MAX_EVENTS 行后插入 FTP 配置常量 ──────────────────
ftp_constants = '''
# ─── FTP 日志归档配置 ─────────────────────────────────────────────
FTP_CONF = os.path.join(LOG_DIR, "ftp.conf")
FTP_DEFAULT = {
    "enabled": False,
    "host": "192.168.31.5",
    "port": 21,
    "user": "lixin",
    "password": "Tangjin0.",
    "remote_path": "/fnOS/tmp/mint_log",
    "upload_on_rotate": True,
    "keep_local": True,
}
'''
content = content.replace(
    'MAX_EVENTS = 2000\n',
    'MAX_EVENTS = 2000\n' + ftp_constants,
    1
)

# ─── 2. 在 MonitorHandler 类之前插入 FTP 辅助函数 ─────────────
ftp_functions = r'''
# ─── FTP 配置读写 ─────────────────────────────────────────────────────
def read_ftp_config():
    """读取 FTP 配置，返回 dict"""
    default = dict(FTP_DEFAULT)
    if not os.path.exists(FTP_CONF):
        return default
    try:
        with open(FTP_CONF, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        for k in default:
            if k not in saved:
                saved[k] = default[k]
        return saved
    except Exception:
        return default

def write_ftp_config(cfg):
    """保存 FTP 配置到文件"""
    os.makedirs(os.path.dirname(FTP_CONF), exist_ok=True)
    with open(FTP_CONF, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def test_ftp_connection(host, port, user, password):
    """测试 FTP 连接，返回 (success, message)"""
    try:
        import ftplib
        ftp = ftplib.FTP()
        ftp.connect(host, int(port), timeout=10)
        ftp.login(user, password)
        welcome = ftp.getwelcome()
        try:
            ftp.cwd("/")
        except Exception:
            pass
        ftp.quit()
        return True, "连接成功: " + welcome
    except Exception as e:
        return False, "连接失败: " + str(e)

def upload_file_to_ftp(local_path, cfg):
    """上传单个文件到 FTP，返回 (success, message)"""
    if not cfg.get("enabled"):
        return False, "FTP 未启用"
    try:
        import ftplib
        host = cfg["host"]
        port = int(cfg["port"])
        user = cfg["user"]
        pwd = cfg["password"]
        remote_path = cfg.get("remote_path", "/").rstrip("/")
        fname = os.path.basename(local_path)

        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=30)
        ftp.login(user, pwd)

        # 确保远程目录存在
        try:
            ftp.cwd(remote_path)
        except Exception:
            parts = remote_path.strip("/").split("/")
            current = ""
            for p in parts:
                current += "/" + p
                try:
                    ftp.cwd(current)
                except Exception:
                    try:
                        ftp.mkd(current)
                        ftp.cwd(current)
                    except Exception:
                        pass

        with open(local_path, 'rb') as f:
            ftp.storbinary("STOR " + fname, f)
        ftp.quit()
        return True, "上传成功: " + fname + " -> " + remote_path + "/" + fname
    except Exception as e:
        return False, "上传失败: " + str(e)

def upload_logs_to_ftp(cfg):
    """上传所有日志文件到 FTP，返回结果列表"""
    results = []
    if not cfg.get("enabled"):
        return results
    if not os.path.isdir(LOG_DIR):
        return results
    files_to_upload = []
    for fname in os.listdir(LOG_DIR):
        if fname.endswith(".gz") or fname.endswith(".log") or fname == "current.log":
            files_to_upload.append(os.path.join(LOG_DIR, fname))
    for fpath in sorted(files_to_upload):
        ok, msg = upload_file_to_ftp(fpath, cfg)
        results.append({"file": os.path.basename(fpath), "ok": ok, "msg": msg})
    return results

'''

# 在 "class MonitorHandler" 之前插入
content = content.replace(
    'class MonitorHandler(',
    ftp_functions + '\nclass MonitorHandler(',
    1
)

# ─── 3. 在 do_GET 里添加 FTP API 端点 ────────────────────────
# 在 'elif path == \'/api/stream\':' 之前插入 FTP 相关路由
ftp_api_get = r'''
        elif path == '/ftp.html' or path == '/ftp':
            self.send_html(FTP_HTML)

        elif path == '/api/ftp_config':
            if self.command == 'POST':
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    data = json.loads(body)
                    old = read_ftp_config()
                    new_cfg = dict(old)
                    for k in ["enabled", "host", "port", "user", "remote_path", "upload_on_rotate", "keep_local"]:
                        if k in data:
                            new_cfg[k] = data[k]
                    if "password" in data and data["password"] and data["password"] != "********":
                        new_cfg["password"] = data["password"]
                    write_ftp_config(new_cfg)
                    self.send_json({"success": True, "message": "FTP 配置已保存"})
                except Exception as e:
                    self.send_json({"success": False, "message": str(e)}, 500)
                return
            # GET
            cfg = read_ftp_config()
            out = dict(cfg)
            if out.get("password"):
                out["password"] = "********"
            self.send_json(out)

        elif path == '/api/ftp_test':
            cfg = read_ftp_config()
            ok, msg = test_ftp_connection(
                cfg.get("host", ""), cfg.get("port", 21),
                cfg.get("user", ""), cfg.get("password", "")
            )
            self.send_json({"success": ok, "message": msg})

        elif path == '/api/ftp_upload':
            cfg = read_ftp_config()
            if not cfg.get("enabled"):
                self.send_json({"success": False, "message": "FTP 未启用"})
                return
            results = upload_logs_to_ftp(cfg)
            self.send_json({"success": True, "results": results})

'''

content = content.replace(
    "        elif path == '/api/stream':",
    ftp_api_get + "        elif path == '/api/stream':",
    1
)

# ─── 4. 在 WEBHOOK_HTML 定义之后插入 FTP_HTML ─────────────
ftp_html = r'''FTP_HTML = r\'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FTP 日志归档配置</title>
<style>
:root {
    --bg: #0d1117; --surface: #161b22; --surface2: #21262d;
    --border: #30363d; --text: #e6edf3; --text2: #8b949e;
    --green: #3fb950; --red: #f85149; --blue: #58a6ff;
    --radius: 8px;
}
* { margin:0; padding:0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }
.topbar { display:flex; align-items:center; justify-content:space-between; padding:12px 24px; background:var(--surface); border-bottom:1px solid var(--border); }
.topbar h1 { font-size:16px; font-weight:600; }
.topbar a { color:var(--blue); text-decoration:none; font-size:13px; }
.container { max-width: 800px; margin: 24px auto; padding: 0 16px; }
.card { background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:20px; margin-bottom:16px; }
.card h2 { font-size:14px; color:var(--text2); margin-bottom:16px; }
.form-row { display:flex; gap:12px; margin-bottom:12px; flex-wrap:wrap; }
.form-group { flex:1; min-width:200px; }
.form-group label { display:block; font-size:11px; color:var(--text2); margin-bottom:4px; text-transform:uppercase; letter-spacing:.5px; }
.form-group input, .form-group select { width:100%; background:var(--bg); border:1px solid var(--border); color:var(--text); padding:8px 10px; border-radius:6px; font-size:13px; }
.form-group input:focus { outline:none; border-color:var(--blue); }
.toggle-row { display:flex; align-items:center; gap:12px; margin-bottom:16px; }
.toggle { position:relative; width:40px; height:22px; background:var(--border); border-radius:11px; cursor:pointer; transition:background .2s; }
.toggle.on { background:var(--green); }
.toggle::after { content:''; position:absolute; top:2px; left:2px; width:18px; height:18px; background:#fff; border-radius:50%; transition:transform .2s; display:block; }
.toggle.on::after { transform:translateX(18px); }
.btn { padding:8px 18px; border-radius:6px; border:1px solid var(--border); background:var(--surface2); color:var(--text); cursor:pointer; font-size:13px; }
.btn:hover { border-color:var(--blue); }
.btn-primary { background:var(--blue); border-color:var(--blue); color:#fff; }
.btn-primary:hover { opacity:.85; }
.status { padding:8px 12px; border-radius:6px; font-size:12px; margin-top:12px; display:none; }
.status.ok { display:block; background:rgba(63,185,80,.12); color:var(--green); border:1px solid rgba(63,185,80,.3); }
.status.err { display:block; background:rgba(248,81,73,.12); color:var(--red); border:1px solid rgba(248,81,73,.3); }
.hint { font-size:11px; color:var(--text2); margin-top:4px; }
#results table { width:100%; border-collapse:collapse; }
#results th { text-align:left; color:var(--text2); font-size:11px; padding:4px 8px; border-bottom:1px solid var(--border); }
#results td { padding:4px 8px; font-size:12px; border-bottom:1px solid var(--border); }
</style>
</head>
<body>
<div class="topbar">
    <h1>📁 FTP 日志归档配置</h1>
    <a href="/">← 返回仪表盘</a>
</div>
<div class="container">
    <div class="card">
        <h2>FTP 服务器配置</h2>
        <div class="toggle-row">
            <span style="font-size:13px;">启用 FTP 归档</span>
            <div class="toggle" id="toggleEnabled" onclick="toggleSwitch()"></div>
            <span style="font-size:12px;color:var(--text2);" id="enabledLabel">已禁用</span>
        </div>
        <div class="form-row">
            <div class="form-group">
                <label>FTP 服务器地址</label>
                <input type="text" id="ftpHost" placeholder="192.168.31.5">
            </div>
            <div class="form-group" style="flex:.3;">
                <label>端口</label>
                <input type="number" id="ftpPort" placeholder="21">
            </div>
        </div>
        <div class="form-row">
            <div class="form-group">
                <label>用户名</label>
                <input type="text" id="ftpUser" placeholder="用户名">
            </div>
            <div class="form-group">
                <label>密码</label>
                <input type="password" id="ftpPass" placeholder="留空表示不修改">
                <div class="hint">留空表示不修改密码</div>
            </div>
        </div>
        <div class="form-row">
            <div class="form-group">
                <label>远程路径</label>
                <input type="text" id="ftpPath" placeholder="/fnOS/tmp/mint_log">
                <div class="hint">日志将上传到该目录</div>
            </div>
        </div>
        <div class="toggle-row">
            <span style="font-size:13px;">日志轮转时自动上传</span>
            <div class="toggle on" id="toggleUpload" onclick="toggleUpload()"></div>
        </div>
        <div class="toggle-row">
            <span style="font-size:13px;">保留本地日志</span>
            <div class="toggle on" id="toggleKeep" onclick="toggleKeep()"></div>
        </div>
        <div style="display:flex; gap:8px; margin-top:16px;">
            <button class="btn btn-primary" onclick="saveConfig()">保存配置</button>
            <button class="btn" onclick="testConnection()">测试连接</button>
            <button class="btn" onclick="triggerUpload()">立即上传日志</button>
        </div>
        <div class="status" id="statusMsg"></div>
    </div>
    <div class="card">
        <h2>上传结果</h2>
        <div id="results">暂无上传记录</div>
    </div>
</div>
<script>
let cfg = {};
async function loadConfig() {
    const r = await fetch('/api/ftp_config');
    cfg = await r.json();
    document.getElementById('ftpHost').value = cfg.host || '';
    document.getElementById('ftpPort').value = cfg.port || 21;
    document.getElementById('ftpUser').value = cfg.user || '';
    document.getElementById('ftpPath').value = cfg.remote_path || '';
    setToggle('toggleEnabled', cfg.enabled, (v)=>{ cfg.enabled=v; });
    setToggle('toggleUpload', cfg.upload_on_rotate!==false, (v)=>{ cfg.upload_on_rotate=v; });
    setToggle('toggleKeep', cfg.keep_local!==false, (v)=>{ cfg.keep_local=v; });
    updateLabels();
}
function setToggle(id, on, setter) {
    const el = document.getElementById(id);
    if(on) el.classList.add('on'); else el.classList.remove('on');
    setter(on);
}
function toggleSwitch() {
    const el = document.getElementById('toggleEnabled');
    el.classList.toggle('on');
    cfg.enabled = el.classList.contains('on');
    updateLabels();
}
function toggleUpload() {
    const el = document.getElementById('toggleUpload');
    el.classList.toggle('on');
    cfg.upload_on_rotate = el.classList.contains('on');
}
function toggleKeep() {
    const el = document.getElementById('toggleKeep');
    el.classList.toggle('on');
    cfg.keep_local = el.classList.contains('on');
}
function updateLabels() {
    document.getElementById('enabledLabel').textContent = cfg.enabled ? '已启用' : '已禁用';
}
async function saveConfig() {
    const data = {
        enabled: cfg.enabled,
        host: document.getElementById('ftpHost').value.trim(),
        port: parseInt(document.getElementById('ftpPort').value)||21,
        user: document.getElementById('ftpUser').value.trim(),
        remote_path: document.getElementById('ftpPath').value.trim(),
        upload_on_rotate: cfg.upload_on_rotate,
        keep_local: cfg.keep_local,
    };
    const pwd = document.getElementById('ftpPass').value;
    if(pwd) data.password = pwd;
    const r = await fetch('/api/ftp_config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    const j = await r.json();
    showStatus(j.success, j.message || '配置已保存');
    if(j.success) loadConfig();
}
async function testConnection() {
    showStatus(null, '正在测试连接...');
    const r = await fetch('/api/ftp_test');
    const j = await r.json();
    showStatus(j.success, j.message);
}
async function triggerUpload() {
    showStatus(null, '正在上传...');
    const r = await fetch('/api/ftp_upload');
    const j = await r.json();
    if(j.success && j.results) {
        let html = '<table><tr><th>文件</th><th>状态</th><th>信息</th></tr>';
        j.results.forEach(r=>{
            const c = r.ok ? 'var(--green)' : 'var(--red)';
            const s = r.ok ? '✅' : '❌';
            html += '<tr><td>'+r.file+'</td><td style="color:'+c+'">'+s+'</td><td>'+r.msg+'</td></tr>';
        });
        html += '</table>';
        document.getElementById('results').innerHTML = html;
        showStatus(j.success, j.success ? '上传完成' : '部分文件上传失败');
    } else {
        showStatus(false, j.message || '上传失败');
    }
}
function showStatus(ok, msg) {
    const el = document.getElementById('statusMsg');
    el.className = 'status ' + (ok===null ? '' : (ok ? 'ok' : 'err'));
    el.textContent = msg;
}
loadConfig();
</script>
</body>
</html>
\'''

# 在 WEBHOOK_HTML = r''' 之前插入 FTP_HTML
# 先找到 WEBHOOK_HTML 的位置，在其前面插入
webhook_marker = 'WEBHOOK_HTML = r\'\'\''
if webhook_marker in content:
    content = content.replace(webhook_marker, ftp_html + '\n' + webhook_marker, 1)
else:
    # 备用：在文件末尾之前插入
    content = content.replace("if __name__ == '__main__':", ftp_html + '\n\nif __name__ == \'__main__\':')

# ─── 5. 在仪表盘 HTML 里添加 FTP 配置入口链接 ─────────
# 在顶栏的 actions 区域添加一个链接
ftp_link = '''        <a href="/ftp.html" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:6px 14px;border-radius:var(--radius);cursor:pointer;font-size:12px;text-decoration:none;">⚙️ FTP 配置</a>'''
# 在顶栏的 actions div 里插入（在 </div> 之前）
topbar_actions_pattern = r'(<div class="topbar actions">.*?)</div>'
match = re.search(topbar_actions_pattern, content, re.DOTALL)
if match:
    old = match.group(1)
    new = old + '\n' + ftp_link + '\n    </div>'
    content = content.replace(old, new, 1)

# 写回文件
with open('net_monitor_web.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("✅ FTP 配置功能已添加")
print("   访问 http://192.168.31.251:9091/ftp.html 配置 FTP")
