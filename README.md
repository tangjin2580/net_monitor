# net_monitor — 子网断网诊断监控系统

针对 192.168.31.0/24 子网频繁断网（IPv4 掉线但 IPv6 存活）的诊断工具，包含 7 个监控子进程和实时 Web 仪表盘。

## 架构

```
net_monitor.sh          核心监控脚本 (Bash, 纯 stdlib)
├── ping_monitor        IPv4/IPv6 分离 Ping, 网关+外网双栈检测
├── arp_monitor         ARP/IPv6 ND 邻居变化监控
├── route_monitor       v4/v6 路由表变化监控
├── event_monitor       物理链路/DHCP/IP/dmesg 事件
├── dns_monitor         DNS 双栈解析测试
├── conntrack_monitor   NAT 连接追踪 (检测 conntrack 表满)
└── traceroute_monitor  域名解析 + Traceroute 路径变化检测

net_monitor_web.py      Web 仪表盘 (Python3 纯标准库, 零依赖)
├── HTTP API            /api/status /api/logs /api/events /api/targets /api/traceroute
├── SSE 实时推送        /api/stream
└── 内嵌 HTML/CSS/JS   暗色主题仪表盘, 含图表和 traceroute 可视化

net_monitor_ctl.sh      管理脚本 (start/stop/restart/status/web-*)
deploy.sh               一键部署脚本 (SCP + SSH)
```

## 远程服务器信息

- 主机: `li@192.168.31.251` (Linux Mint VM)
- 接口: `ens18`
- 网关 IPv4: `192.168.31.254`
- 网关 IPv6: `fe80::be24:11ff:fe03:4960%ens18`
- Web 端口: `9091`
- SSH 密钥: `~/.ssh/key/id_ed25519`

## 部署

### 方式一：一键部署（推荐）

从 Windows 本机执行：

```bash
# 使用 Git Bash 或 WSL
bash G:/code/net_monitor/deploy.sh

# 指定目标
bash G:/code/net_monitor/deploy.sh li@192.168.31.251

# 指定 SSH 密钥
bash G:/code/net_monitor/deploy.sh li@192.168.31.251 --key ~/.ssh/key/id_ed25519
```

deploy.sh 会自动：停止旧进程 → 上传文件 → 设置权限 → 创建目录 → 启动服务 → 验证部署。

### 方式二：手动部署

```bash
# 1. 上传文件
scp -i ~/.ssh/key/id_ed25519 -o StrictHostKeyChecking=no \
    net_monitor.sh net_monitor_web.py net_monitor_ctl.sh \
    li@192.168.31.251:/home/li/

# 2. 设置权限
ssh -i ~/.ssh/key/id_ed25519 li@192.168.31.251 \
    "chmod +x /home/li/net_monitor.sh /home/li/net_monitor_ctl.sh"

# 3. 创建数据目录
ssh -i ~/.ssh/key/id_ed25519 li@192.168.31.251 \
    "mkdir -p /home/li/net_monitor_logs/{trace_baselines,trace_current,snapshots}"

# 4. 启动
ssh -i ~/.ssh/key/id_ed25519 li@192.168.31.251 \
    "bash /home/li/net_monitor_ctl.sh start-all"
```

### 方式三：在远程服务器上直接操作

SSH 登录后：

```bash
# 启动全部（监控 + Web）
bash /home/li/net_monitor_ctl.sh start-all

# 仅启动监控
bash /home/li/net_monitor_ctl.sh start

# 仅启动 Web
bash /home/li/net_monitor_ctl.sh web-start
```

### 方式四：systemd 开机自启（生产推荐）

项目包含两个 systemd service 文件，支持开机自动启动：

```bash
# 1. 上传 service 文件
scp -i ~/.ssh/key/id_ed25519 -o StrictHostKeyChecking=no \
    net_monitor.service net_monitor_web.service \
    li@192.168.31.251:/tmp/

# 2. 安装到 systemd 目录
ssh -i ~/.ssh/key/id_ed25519 li@192.168.31.251 bash -s << 'EOF'
    sudo cp /tmp/net_monitor.service /etc/systemd/system/
    sudo cp /tmp/net_monitor_web.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable net_monitor.service
    sudo systemctl enable net_monitor_web.service
    sudo systemctl start net_monitor.service
    sudo systemctl start net_monitor_web.service
EOF

# 3. 验证
ssh -i ~/.ssh/key/id_ed25519 li@192.168.31.251 \
    "sudo systemctl status net_monitor net_monitor_web --no-pager"

# 日常管理
sudo systemctl restart net_monitor      # 重启监控
sudo systemctl stop net_monitor         # 停止监控
sudo journalctl -u net_monitor -f       # 查看日志 (journal)
sudo systemctl status net_monitor_web   # Web 状态
```

> 注意：使用 systemd 时，建议不要用 net_monitor_ctl.sh 的 start/stop 命令，避免冲突。systemd 会自动管理进程生命周期和失败重启。

## 管理命令

```bash
# 全部操作前缀: ssh -i ~/.ssh/key/id_ed25519 li@192.168.31.251 "bash /home/li/net_monitor_ctl.sh <命令>"

start           启动监控
stop            停止监控
restart         重启监控
status          查看状态 (含子进程/日志)
log             查看全部日志
tail            实时跟踪日志
events          只看事件和警告
disconnects     只看断网/恢复事件
stats           统计摘要
web-start       启动 Web 仪表盘
web-stop        停止 Web
web-restart     重启 Web
web-status      Web 状态
start-all       启动监控 + Web
stop-all        停止 Web + 监控
```

## 文件路径

| 路径 | 说明 |
|---|---|
| `/home/li/net_monitor.sh` | 监控脚本 |
| `/home/li/net_monitor_web.py` | Web 仪表盘 |
| `/home/li/net_monitor_ctl.sh` | 管理脚本 |
| `/home/li/net_monitor.pid` | 监控进程 PID |
| `/home/li/net_monitor_web.pid` | Web 进程 PID |
| `/home/li/net_monitor_logs/current.log` | 当前日志 |
| `/home/li/net_monitor_logs/snapshots/` | 断网快照 |
| `/home/li/net_monitor_logs/trace_baselines/` | Traceroute 基线 |
| `/home/li/net_monitor_logs/trace_current/` | Traceroute 当前 |

## 诊断目标

所有外网目标均使用 DNS 域名解析（不写死 IP），同时验证 DNS 可用性和连通性：

**域名类（DNS 解析 → ping，容错 fallback）：**

| 名称 | 域名 |
|---|---|
| 王者荣耀 | pvp.qq.com |
| 百度 | www.baidu.com |
| 京东 | www.jd.com |
| 腾讯 | qq.com |
| 阿里 | www.taobao.com |
| 小米 | www.mi.com |
| 华为 | www.huawei.com |

**IP 类（直接 ping，轮转容错）：**

| 名称 | IP |
|---|---|
| 公共DNS | 223.5.5.5, 114.114.114.114, 119.29.29.29, 8.8.8.8 |
| 本地DNS | 192.168.31.254, 192.168.31.251 |

## Web 仪表盘

访问 `http://192.168.31.251:9091`，功能：

- 实时状态卡片（网关/IPv4/IPv6/断网事件/Ping 统计）
- 逐服务连通性格子（含成功率百分比，颜色标识）
- Traceroute 路径可视化（基线对比，变化高亮，默认折叠）
- 成功率 & 延迟趋势图表
- 事件流 + 实时日志（可过滤）
- SSE 实时推送

## 配置修改

编辑 `net_monitor.sh` 顶部的配置区域：

```bash
IFACE="ens18"                    # 网卡接口
GW_V4="192.168.31.254"           # IPv4 网关
GW_V6="fe80::..."                # IPv6 网关
DIAG_RESOLVER="223.5.5.5"        # DNS 解析服务器
DIAG_INTERVAL=60                 # 诊断间隔(秒)
TRACE_INTERVAL=300               # Traceroute 间隔(秒)
PING_TIMEOUT=1                   # Ping 超时(秒)
FAIL_THRESHOLD=3                 # 连续失败阈值
```

## 注意事项

- 纯 stdlib，远程服务器无需安装任何 pip 包
- 同一时间只能运行一个监控实例，多实例会导致日志数据污染
- 日志自动轮转（>200MB 时归档），保留 7 天
- 断网快照自动保存最近 20 个
- SSH 密钥路径是 `~/.ssh/key/id_ed25519`（非默认位置）
