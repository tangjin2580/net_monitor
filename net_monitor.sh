#!/usr/bin/env bash
###############################################################################
# net_monitor.sh v4 — 优化配置版
# 优化方向: 更低资源占用 + 更准确事件检测 + NAS soft 挂载
# 部署位置: /home/li/net_monitor.sh
# 用法: nohup bash /home/li/net_monitor.sh &
# 停止: kill $(cat /home/li/net_monitor.pid)
###############################################################################
set -uo pipefail

# ─── 配置 ─────────────────────────────────────────────────────────────────
# JSON 配置文件路径 (可通过 web 配置页修改)
MONITOR_CONF="${LOCAL_LOG_DIR:-/home/li/net_monitor_logs}/monitor_targets.json"

# 默认值 (JSON 不存在或解析失败时使用)
IFACE="ens18"
GW_V4="192.168.31.254"
GW_V6="fe80::be24:11ff:fe03:4960%${IFACE}"
INET_V4_TARGETS=("223.5.5.5" "119.29.29.29" "1.1.1.1")
INET_V6_TARGETS=("2400:3200::1" "240e:ff:e020:99b:0:ff:b099:cff1")
DNS_HOST="www.baidu.com"
DIAG_RESOLVER="223.5.5.5"
DIAG_DOMAINS=(
    "王者荣耀:pvp.qq.com" "百度:www.baidu.com" "京东:www.jd.com"
    "腾讯:qq.com" "阿里:www.taobao.com" "小米:www.mi.com" "华为:www.huawei.com"
)
DIAG_IP_TARGETS=(
    "公共DNS:223.5.5.5,119.29.29.29,8.8.8.8,1.1.1.1"
    "本地DNS:192.168.31.254,192.168.31.251"
)
HTTP_TARGETS=("https://www.baidu.com" "https://www.taobao.com" "https://www.jd.com")
LAN_HOSTS=()

# 尝试从 JSON 加载配置
_load_json_config() {
    local conf="${1:-$MONITOR_CONF}"
    [[ -f "$conf" ]] || return 1
    command -v jq &>/dev/null || return 1
    # 网关
    local j_iface; j_iface=$(jq -r '.gateway.iface // empty' "$conf" 2>/dev/null)
    [[ -n "$j_iface" ]] && IFACE="$j_iface"
    local j_gw4; j_gw4=$(jq -r '.gateway.v4 // empty' "$conf" 2>/dev/null)
    [[ -n "$j_gw4" ]] && GW_V4="$j_gw4"
    local j_gw6; j_gw6=$(jq -r '.gateway.v6 // empty' "$conf" 2>/dev/null)
    [[ -n "$j_gw6" ]] && GW_V6="${j_gw6}%${IFACE}"
    # 外网
    local j_dns; j_dns=$(jq -r '.wan.dns_host // empty' "$conf" 2>/dev/null)
    [[ -n "$j_dns" ]] && DNS_HOST="$j_dns"
    local v4_json; v4_json=$(jq -r '.wan.v4_targets // empty' "$conf" 2>/dev/null)
    if [[ -n "$v4_json" ]]; then
        INET_V4_TARGETS=()
        while IFS= read -r ip; do [[ -n "$ip" ]] && INET_V4_TARGETS+=("$ip"); done < <(jq -r '.wan.v4_targets[]' "$conf" 2>/dev/null)
    fi
    local v6_json; v6_json=$(jq -r '.wan.v6_targets // empty' "$conf" 2>/dev/null)
    if [[ -n "$v6_json" ]]; then
        INET_V6_TARGETS=()
        while IFS= read -r ip; do [[ -n "$ip" ]] && INET_V6_TARGETS+=("$ip"); done < <(jq -r '.wan.v6_targets[]' "$conf" 2>/dev/null)
    fi
    local http_json; http_json=$(jq -r '.wan.http_targets // empty' "$conf" 2>/dev/null)
    if [[ -n "$http_json" ]]; then
        HTTP_TARGETS=()
        while IFS= read -r url; do [[ -n "$url" ]] && HTTP_TARGETS+=("$url"); done < <(jq -r '.wan.http_targets[]' "$conf" 2>/dev/null)
    fi
    # DNS
    local j_resolver; j_resolver=$(jq -r '.dns.resolver // empty' "$conf" 2>/dev/null)
    [[ -n "$j_resolver" ]] && DIAG_RESOLVER="$j_resolver"
    local dns_srv; dns_srv=$(jq -r '.dns.servers // empty' "$conf" 2>/dev/null)
    if [[ -n "$dns_srv" ]]; then
        DIAG_IP_TARGETS=()
        local srv_str=""; while IFS= read -r s; do srv_str="${srv_str}${srv_str:+,}$s"; done < <(jq -r '.dns.servers[]' "$conf" 2>/dev/null)
        [[ -n "$srv_str" ]] && DIAG_IP_TARGETS=("本地DNS:${srv_str}")
        # 公共DNS保持默认
        DIAG_IP_TARGETS+=("公共DNS:223.5.5.5,119.29.29.29,8.8.8.8,1.1.1.1")
    fi
    local domains; domains=$(jq -r '.dns.domains // empty' "$conf" 2>/dev/null)
    if [[ -n "$domains" ]]; then
        DIAG_DOMAINS=()
        while IFS= read -r entry; do [[ -n "$entry" ]] && DIAG_DOMAINS+=("$entry"); done < <(jq -r '.dns.domains[] | "\(.name):\(.domain)"' "$conf" 2>/dev/null)
    fi
    # 内网设备
    local lan_json; lan_json=$(jq -r '.lan.hosts // empty' "$conf" 2>/dev/null)
    if [[ -n "$lan_json" ]]; then
        LAN_HOSTS=()
        while IFS= read -r entry; do [[ -n "$entry" ]] && LAN_HOSTS+=("$entry"); done < <(jq -r '.lan.hosts[] | "\(.name) \(.ip)"' "$conf" 2>/dev/null)
    fi
    return 0
}
_load_json_config "${MONITOR_CONF}"

PING_INTERVAL=2
PING_TIMEOUT=1
ARP_INTERVAL=15
ROUTE_INTERVAL=10
EVENT_INTERVAL=5
STATS_INTERVAL=300
CONN_INTERVAL=30
DIAG_INTERVAL=60
HTTP_INTERVAL=60   # HTTP 延迟检测间隔

# ─── 迟滞(hysteresis)配置 ─────────────────────────────────────
# 状态恢复需要连续 N 次成功才确认恢复（避免抖动）
HYSTERESIS_OK=3
# 状态异常需要连续 N 次失败才确认异常（避免误报）
HYSTERESIS_FAIL=3

# Webhook
WEBHOOK_CONF="/opt/net_monitor/webhook.conf"
WEBHOOK_MIN_INTERVAL=60

# ─── 日志与 NAS 配置 ───────────────────────────────────────────
# NAS 挂载点 (soft 模式，超时 10s，避免挂载失败导致程序挂起)
NAS_MOUNT="/mnt/nas_netmon"
NAS_SHARE="//192.168.31.5/fs/1000/nfs"
NAS_USER=""
NAS_PASS=""
NAS_MOUNT_OPTS="soft,timeo=10,intr,vers=3"
USE_NAS_FALLBACK=true

LOCAL_LOG_DIR="/home/li/net_monitor_logs"
LOG_COMPRESS=true
LOG_RETENTION_DAYS=7
MAX_LOG_SIZE=$((10 * 1024 * 1024))   # 10M
PID_FILE="/home/li/net_monitor.pid"
FAIL_THRESHOLD=3

COMPACT_LOG=true
LOG_FORMAT="compact"

# ─── 全局状态 ────────────────────────────────────────────────
declare -A _COOLDOWN_MAP
declare -A _COOLDOWN_LAST

# 迟滞计数器 (避免状态抖动)
declare -A _HYST_GW_OK=0 _HYST_GW_FAIL=0
declare -A _HYST_V4_OK=0 _HYST_V4_FAIL=0
declare -A _HYST_V6_OK=0 _HYST_V6_FAIL=0

# ─── 工具函数 ────────────────────────────────────────────────────────────────

# NAS 挂载 (soft 模式，失败不阻塞)
ensure_nas_mount() {
    # 已挂载则直接返回
    if mountpoint -q "$NAS_MOUNT" 2>/dev/null; then
        return 0
    fi
    # 尝试挂载 (soft 模式)
    mkdir -p "$NAS_MOUNT" 2>/dev/null || true
    if [[ -n "$NAS_USER" ]]; then
        mount -t nfs4 -o "$NAS_MOUNT_OPTS" "$NAS_SHARE" "$NAS_MOUNT" 2>/dev/null \
            || mount -t nfs -o "$NAS_MOUNT_OPTS" "$NAS_SHARE" "$NAS_MOUNT" 2>/dev/null \
            || true
    else
        mount -t nfs4 -o "$NAS_MOUNT_OPTS" "$NAS_SHARE" "$NAS_MOUNT" 2>/dev/null \
            || mount -t nfs -o "$NAS_MOUNT_OPTS" "$NAS_SHARE" "$NAS_MOUNT" 2>/dev/null \
            || true
    fi
    if mountpoint -q "$NAS_MOUNT" 2>/dev/null; then
        echo "[NAS] 挂载成功: $NAS_SHARE → $NAS_MOUNT" >&2
        return 0
    fi
    return 1
}

# 解析日志目录 (NAS优先, 本地回退, soft 不阻塞)
resolve_log_dir() {
    if ensure_nas_mount; then
        echo "${NAS_MOUNT}/net_monitor_logs"
    else
        echo "$LOCAL_LOG_DIR"
    fi
}

LOG_DIR="$(resolve_log_dir)"
LOG_FILE="${LOG_DIR}/current.log"
SNAPSHOT_DIR="${LOG_DIR}/snapshots"

# 重新初始化日志路径
mkdir -p "$LOG_DIR" "$SNAPSHOT_DIR"
echo $$ > "$PID_FILE"

# ─── 日志函数 (直接 >> 追加，避免 tee 子进程) ─────────────
log() {
    local level="$1"
    shift
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S.%3N')
    printf '[%s] [%-5s] %s\n' "$ts" "$level" "$*" >> "$LOG_FILE"
}

log_event()  { log "EVENT"  "$@"; }
log_warn()   { log "WARN"   "$@"; }
log_err()    { log "ERROR"  "$@"; }
log_info()   { log "INFO"   "$@"; }
log_stats()  { log "STATS"  "$@"; }
log_diag()   { log "DIAG"   "$@"; }
log_heartbeat() { log "HEARTBEAT" "$@"; }

# 事件冷却: 同一事件类型在冷却期内不重复记录
event_cooldown() {
    local event_type="$1"
    local seconds="${2:-60}"
    local now
    now=$(date +%s)
    local last
    last="${_COOLDOWN_LAST[$event_type]:-0}"
    if (( now - last < seconds )); then
        return 1
    fi
    _COOLDOWN_LAST[$event_type]=$now
    return 0
}

# 日志轮转 (严格 10M 上限)
rotate_log() {
    local size
    size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if (( size > MAX_LOG_SIZE )); then
        local rotated="${LOG_DIR}/monitor_$(date '+%Y%m%d_%H%M%S').log"
        mv "$LOG_FILE" "$rotated"
        # 通知 web 服务重新打开日志文件
        touch "$LOG_FILE"
        if $LOG_COMPRESS; then
            gzip -f "$rotated" &
        fi
        # 清理过期日志 (括号确保 -mtime 对两个 -name 都生效)
        find "$LOG_DIR" \( -name "monitor_*.log" -o -name "monitor_*.log.gz" \
            -o -name "current.log.*" \) -mtime +$LOG_RETENTION_DAYS -delete 2>/dev/null || true
        log_info "[LOG_ROTATE] 日志已轮转: $(basename "$rotated") (${size} bytes)"
        # FTP 自动上传（如果已配置）
        if [[ -f "${LOG_DIR}/ftp.conf" ]]; then
            curl -s --connect-timeout 5 http://127.0.0.1:9091/api/ftp_upload > /dev/null 2>&1 &
        fi
    fi
}

# NAS 可用性检查 (每60秒主循环调用，不阻塞)
check_nas_switch() {
    local current_dir="$LOG_DIR"
    local new_dir
    ensure_nas_mount
    new_dir="$(resolve_log_dir)"
    if [[ "$new_dir" != "$current_dir" ]]; then
        LOG_DIR="$new_dir"
        LOG_FILE="${LOG_DIR}/current.log"
        SNAPSHOT_DIR="${LOG_DIR}/snapshots"
        mkdir -p "$LOG_DIR" "$SNAPSHOT_DIR"
        touch "$LOG_FILE"
        log_info "[LOG_SWITCH] 日志路径切换: $current_dir → $LOG_DIR"
    fi
}

# ─── Webhook 告警 ─────────────────────────────────────────────────
send_webhook() {
    local title="$1" content="$2" severity="${3:-warning}" event_type="${4:-unknown}"
    local url="" platform="generic" secret=""
    if [[ -f "$WEBHOOK_CONF" ]]; then
        url=$(sed -n '1p' "$WEBHOOK_CONF" 2>/dev/null | tr -d '\n\r ')
        platform=$(sed -n '2p' "$WEBHOOK_CONF" 2>/dev/null | tr -d '\n\r ')
        [[ -z "$platform" ]] && platform="generic"
        secret=$(sed -n '3p' "$WEBHOOK_CONF" 2>/dev/null | tr -d '\n\r ')
    fi
    [[ -z "$url" ]] && return 0

    # 冷却：同一 event_type 60 秒内只发一次
    local last_key="_webhook_last_${event_type}"
    local last_time
    eval "last_time=\${$last_key:-0}"
    local now_epoch
    now_epoch=$(date +%s)
    if (( now_epoch - last_time < WEBHOOK_MIN_INTERVAL )); then
        return 0
    fi
    eval "$last_key=$now_epoch"

    local time_str hostname
    time_str=$(date '+%Y-%m-%d %H:%M:%S')
    hostname=$(hostname)

    python3 -c '
import json, sys, urllib.request, urllib.error, urllib.parse
import hmac, hashlib, base64, time

url = sys.argv[1]
platform = sys.argv[2]
title = sys.argv[3]
content = sys.argv[4]
severity = sys.argv[5]
event_type = sys.argv[6]
hostname = sys.argv[7]
time_str = sys.argv[8]
secret = sys.argv[9] if len(sys.argv) > 9 else ""

try:
    if platform == "feishu":
        payload = json.dumps({"msg_type": "text", "content": {"text": f"【{title}】{content}\n主机: {hostname}\n时间: {time_str}"}})
    elif platform == "dingtalk":
        # 钉钉加签：timestamp + "\n" + secret → HMAC SHA256 → Base64
        timestamp = str(int(time.time() * 1000))
        sign_str = timestamp + "\n" + secret
        sign = base64.b64encode(
            hmac.new(secret.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        # 拼上签名参数
        sep = "&" if "?" in url else "?"
        url = url + sep + "timestamp=" + timestamp + "&sign=" + urllib.parse.quote(sign)
        payload = json.dumps({"msgtype": "text", "text": {"content": f"【{title}】{content}\n主机: {hostname}\n时间: {time_str}"}})
    elif platform == "wecom":
        payload = json.dumps({"msgtype": "text", "text": {"content": f"【{title}】{content}\n主机: {hostname}\n时间: {time_str}"}})
    else:
        payload = json.dumps({
            "title": title, "content": content, "severity": severity,
            "event_type": event_type, "hostname": hostname, "time": time_str
        })
    req = urllib.request.Request(url, data=payload.encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10)
except Exception:
    pass
' "$url" "$platform" "$title" "$content" "$severity" "$event_type" "$hostname" "$time_str" "$secret" >/dev/null 2>&1 || true
}
# ─── DNS 污染检测 ────────────────────────────────────────────────────
DNS_POLLUTION_RESOLVERS=(
    "223.5.5.5"
    "114.114.114.114"
    "8.8.8.8"
)

dns_pollution_detect() {
    local domain="${1:-$DNS_HOST}"
    local results=""
    local first_ips=""
    for resolver in "${DNS_POLLUTION_RESOLVERS[@]}"; do
        local ips
        ips=$(timeout 3 getent hosts "$domain" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ',')
        [[ -z "$ips" ]] && ips=$(timeout 3 nslookup "$domain" "$resolver" 2>/dev/null \
            | awk 'index($0, "Address:") == 1 { gsub(/#.*/, "", $2); if ($2 != "" && $2 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) print $2 }' | sort -u | tr '\n' ',')
        if [[ -z "$first_ips" ]]; then
            first_ips="$ips"
        elif [[ "$ips" != "$first_ips" && -n "$ips" && -n "$first_ips" ]]; then
            if event_cooldown "dns_pollution" 300; then
                log_err "[DNS_POLLUTION] 解析不一致! $domain | 期望: $first_ips | 实际($resolver): $ips"
                send_webhook "DNS污染检测" "域名 $domain 在不同DNS服务器解析结果不一致" "critical" "dns_pollution"
            fi
            return 1
        fi
        results="${results}${resolver}=$ips "
    done
    return 0
}

# ─── 链路质量评分 ────────────────────────────────────────────────────────────
link_quality_score() {
    local loss="$1"   # 丢包率 0-100
    local avg_rtt="$2" # 平均延迟 ms
    local jitter="$3"  # 抖动 ms
    local score=100
    # 丢包扣分: 每1%扣10分
    score=$((score - loss * 10))
    # 延迟扣分: >50ms 后每10ms扣1分
    if (( avg_rtt > 50 )); then
        score=$((score - (avg_rtt - 50) / 10))
    fi
    # 抖动扣分: >20ms 后每5ms扣1分
    if (( jitter > 20 )); then
        score=$((score - (jitter - 20) / 5))
    fi
    (( score < 0 )) && score=0
    echo "$score"
}

# ─── HTTP/HTTPS 延迟检测 ────────────────────────────────────────────────────
LAST_HTTP_RESULTS=()  # 最近一次各目标延迟

http_latency_monitor() {
    set +e
    log_info "[http_monitor] 启动 (HTTP/HTTPS 延迟检测)"
    local idx=0
    while true; do
        local target="${HTTP_TARGETS[$(( idx % ${#HTTP_TARGETS[@]} ))]}"
        local start_ns end_ns dur_ms http_code
        start_ns=$(date +%s%N)
        http_code=$(timeout 5 curl -4 -sI -o /dev/null -w "%{http_code}" "$target" 2>/dev/null || echo "0")
        end_ns=$(date +%s%N)
        dur_ms=$(( (end_ns - start_ns) / 1000000 ))
        [[ "$http_code" == "0" ]] && dur_ms=-1

        if (( dur_ms >= 0 )); then
            log_info "[HTTP] ${target} → ${dur_ms}ms (HTTP ${http_code})"
            if (( dur_ms > 3000 )); then
                log_warn "[HTTP_SLOW] ${target} 延迟过高: ${dur_ms}ms"
            fi
        else
            log_warn "[HTTP_FAIL] ${target} → 连接失败 (HTTP ${http_code})"
        fi

        idx=$((idx + 1))
        sleep "$HTTP_INTERVAL"
    done
}

###############################################################################
# 子进程 8: 系统资源 + 带宽 + TCP 重传采集 (写入管道供 Web 读取)
###############################################################################
stats_collector() {
    set +e
    log_info "[stats_collector] 启动 (采集间隔 10s)"

    local PIPE="/tmp/net_monitor_stats.fifo"
    local prev_rx=0 prev_tx=0 prev_time=0
    local _prev_cpu_total=0 _prev_cpu_idle=0
    local _debug_cnt=0
    local prev_retries=0 prev_tcp_sends=0 prev_tcp_sent=0

    # 等待管道出现 (Web 服务创建)
    local waited=0
    while [[ ! -p "$PIPE" ]] && (( waited < 60 )); do
        sleep 2
        waited=$((waited + 2))
    done
    (( waited >= 60 )) && log_warn "[stats_collector] 管道未找到: $PIPE (Web 服务未启动?)"

    while true; do
        # ─── 系统资源采集 ────────────────────────────────
        local cpu_pct=0 mem_pct=0 disk_pct=0 load1=0

        # CPU 使用率 (从 /proc/stat 两次采样计算)
        if [[ -f /proc/stat ]]; then
            local cpu_fields=($(awk '/^cpu / {print $2, $3, $4, $5, $6, $7, $8}' /proc/stat))
            local user=${cpu_fields[0]:-0} nice=${cpu_fields[1]:-0} system=${cpu_fields[2]:-0} idle=${cpu_fields[3]:-0}
            local iowait=${cpu_fields[4]:-0} irq=${cpu_fields[5]:-0} softirq=${cpu_fields[6]:-0}
            local total=$((user + nice + system + idle + iowait + irq + softirq))
            if [[ -n "$_prev_cpu_total" && -n "$_prev_cpu_idle" ]]; then
                local total_diff=$((total - _prev_cpu_total))
                local idle_diff=$(( (idle + iowait) - _prev_cpu_idle ))
                [[ $total_diff -gt 0 ]] && cpu_pct=$(( (total_diff - idle_diff) * 100 / total_diff ))
            fi
            _prev_cpu_total=$total
            _prev_cpu_idle=$((idle + iowait))
            [[ ! "$cpu_pct" =~ ^[0-9]+$ ]] && cpu_pct=0
        fi

        # 内存使用率 (精确匹配 /proc/meminfo 字段，兼容中文 locale)
        mem_total=$(awk '/^MemTotal:/ {print $2}')
        mem_free=$(awk '/^MemFree:/ {print $2}')
        mem_buffers=$(awk '/^Buffers:/ {print $2}')
        mem_cached=$(awk '/^Cached:/ {print $2}')
        mem_sreclaimable=$(awk '/^SReclaimable:/ {print $2}')
        [[ -z "$mem_sreclaimable" ]] && mem_sreclaimable=0
        mem_avail=$((mem_free + mem_buffers + mem_cached + mem_sreclaimable))
        [[ $mem_total -gt 0 ]] && mem_pct=$(( (mem_total - mem_avail) * 100 / mem_total ))
        [[ ! "$mem_pct" =~ ^[0-9]+$ ]] && mem_pct=0

        # 磁盘使用率 (根分区)
        disk_pct=$(df / 2>/dev/null | awk 'NR==2 {gsub(/%/,""); print $5}')
        [[ ! "$disk_pct" =~ ^[0-9]+$ ]] && disk_pct=0

        # 负载
        load1=$(cat /proc/loadavg 2>/dev/null | awk '{print $1}' | cut -d. -f1)
        [[ ! "$load1" =~ ^[0-9]+$ ]] && load1=0

        # 写入日志 (Web 服务会解析)
        log_info "[SYSTEM_STATS] cpu=${cpu_pct} mem=${mem_pct} disk=${disk_pct} load=${load1}"

        # ─── 带宽采集 ────────────────────────────────────
        local rx_bytes=0 tx_bytes=0 rx_kbps=0 tx_kbps=0
        if [[ -f /proc/net/dev ]]; then
            local dev_stats=( $(grep "$IFACE:" /proc/net/dev | awk -F'[: ]+' '{print $3, $11}') )
            rx_bytes=${dev_stats[0]:-0}
            tx_bytes=${dev_stats[1]:-0}
            local now_ts=$(date +%s)
            if (( prev_time > 0 )); then
                local dt=$((now_ts - prev_time))
                (( dt > 0 )) && {
                    rx_kbps=$(( (rx_bytes - prev_rx) * 8 / 1024 / dt ))
                    tx_kbps=$(( (tx_bytes - prev_tx) * 8 / 1024 / dt ))
                    (( rx_kbps < 0 )) && rx_kbps=0
                    (( tx_kbps < 0 )) && tx_kbps=0
                }
            fi
            prev_rx=$rx_bytes
            prev_tx=$tx_bytes
            prev_time=$now_ts
        fi
        if [[ -p "$PIPE" ]]; then
            log_info "[BANDWIDTH] rx=${rx_kbps}kbps tx=${tx_kbps}kbps"
        fi

        # ─── TCP 重传率采集 ──────────────────────────────
        local retrans_rate=0
        if [[ -f /proc/net/netstat ]]; then
            local tcp_stats=( $(grep "TcpExt:" /proc/net/netstat 2>/dev/null | awk '{print $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12}') )
            # 用 /proc/net/snmp 获取重传计数
            local retrans=$(grep "Tcp:" /proc/net/snmp 2>/dev/null | awk 'NR==2 {print $8}')  # RetransSegs
            local out_segs=$(grep "Tcp:" /proc/net/snmp 2>/dev/null | awk 'NR==2 {print $7}')  # OutSegs
            if [[ -n "$retrans" && -n "$out_segs" && "$out_segs" -gt 0 ]]; then
                if (( prev_tcp_sends > 0 )); then
                    local retrans_delta=$((retrans - prev_retries))
                    local out_delta=$((out_segs - prev_tcp_sent))
                    (( out_delta > 0 )) && retrans_rate=$((retrans_delta * 100 / out_delta))
                    (( retrans_rate < 0 )) && retrans_rate=0
                    (( retrans_rate > 100 )) && retrans_rate=100
                fi
                prev_retries=$retrans
                prev_tcp_sent=$out_segs
            fi
        fi
        if [[ -p "$PIPE" ]]; then
            log_info "[TCP_RETRANS] rate=${retrans_rate}%"
        fi

        sleep 10
    done
}


# ─── 断网快照 ────────────────────────────────────────────────────────
take_disconnect_snapshot() {
    local reason="$1"
    local tag
    tag="disc_$(date '+%Y%m%d_%H%M%S')_${reason}"
    local snap="${SNAPSHOT_DIR}/${tag}.txt"

    log_err "[SNAPSHOT] 断网快照: $reason → $snap"

    {
        echo "=============================================="
        echo " 断网快照: $(date '+%Y-%m-%d %H:%M:%S')"
        echo " 原因: $reason"
        echo "=============================================="
        echo ""
        echo "── 接口状态 ──"
        ip link show "$IFACE"
        echo ""
        echo "── ethtool 检查 ──"
        if command -v ethtool >/dev/null 2>&1; then
            ethtool "$IFACE" 2>/dev/null || echo "ethtool 不可用"
        else
            echo "ethtool 未安装"
        fi
        echo ""
        echo "── IPv4 地址 ──"
        ip -4 addr show "$IFACE"
        echo ""
        echo "── IPv6 地址 ──"
        ip -6 addr show "$IFACE" scope global
        echo ""
        echo "── IPv4 路由表 ──"
        ip -4 route show
        echo ""
        echo "── IPv6 路由表 ──"
        ip -6 route show
        echo ""
        echo "── ARP 表 ──"
        cat /proc/net/arp
        echo ""
        echo "── IPv6 邻居 ──"
        ip -6 neigh show dev "$IFACE" | head -20
        echo ""
        echo "── Conntrack 统计 ──"
        echo "count: $(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || echo N/A)"
        echo "max:   $(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || echo N/A)"
        echo ""
        echo "── 接口流量统计 ──"
        ip -s link show "$IFACE"
        echo ""
        echo "── dmesg (最后20行) ──"
        dmesg --time-format iso 2>/dev/null | tail -20 || dmesg | tail -20
        echo ""

        if command -v mtr >/dev/null 2>&1; then
            echo "── mtr IPv4 (网关) ──"
            timeout 10 mtr -4 -n -r -c 3 "$GW_V4" 2>&1 || echo "mtr 超时"
            echo ""
            echo "── mtr IPv4 (223.5.5.5) ──"
            timeout 15 mtr -4 -n -r -c 3 223.5.5.5 2>&1 || echo "mtr 超时"
            echo ""
            echo "── mtr IPv6 (2400:3200::1) ──"
            timeout 15 mtr -6 -n -r -c 3 2400:3200::1 2>&1 || echo "mtr 超时"
        else
            echo "── traceroute IPv4 (网关) ──"
            timeout 10 traceroute -4 -n -m 5 -w 1 "$GW_V4" 2>&1 || echo "traceroute 超时/不可用"
            echo ""
            echo "── traceroute IPv4 (223.5.5.5) ──"
            timeout 15 traceroute -4 -n -m 15 -w 1 223.5.5.5 2>&1 || echo "traceroute 超时/不可用"
            echo ""
            echo "── traceroute IPv6 (2400:3200::1) ──"
            timeout 15 traceroute -6 -n -m 15 -w 1 2400:3200::1 2>&1 || echo "traceroute 超时/不可用"
        fi
        echo ""

        if [[ "$reason" == "v4_only" ]]; then
            echo "=============================================="
            echo " ▶ IPv4 掉线但 IPv6 存活 — 专项诊断"
            echo "=============================================="
            echo ""
            echo "── IPv4 连通性详细 ──"
            for target in "${INET_V4_TARGETS[@]}"; do
                echo -n "  ping -4 $target: "
                ping -4 -c 2 -W 2 "$target" 2>&1 | tail -1 || echo "FAIL"
            done
            echo ""
            echo "── IPv6 连通性详细 ──"
            for target in "${INET_V6_TARGETS[@]}"; do
                echo -n "  ping -6 $target: "
                ping -6 -c 2 -W 2 "$target" 2>&1 | tail -1 || echo "FAIL"
            done
            echo ""
            echo "── IPv4 DNS 解析 ──"
            echo "  getent hosts $DNS_HOST (default):"
            timeout 5 getent hosts "$DNS_HOST" 2>&1 || echo "  TIMEOUT/FAIL"
            echo ""
            echo "── 当前 DNS 配置 ──"
            cat /etc/resolv.conf
            echo ""
            echo "── MTU / Path MTU ──"
            echo "  interface MTU: $(cat /sys/class/net/$IFACE/mtu)"
        fi
    } > "$snap" 2>&1

    log_err "[SNAPSHOT] 快照已保存: $snap ($(du -h "$snap" | cut -f1))"

    # 只保留最近 20 个快照
    ls -t "$SNAPSHOT_DIR"/disc_*.txt 2>/dev/null | tail -n +21 | xargs rm -f 2>/dev/null || true
}

# ─── 逐服务诊断 ──────────────────────────────────────────────────────────────
_svc_diag_idx=0

diag_ping_domain() {
    local name="$1" domain="$2"
    local ips
    ips=$(timeout 3 getent hosts "$domain" 2>/dev/null | awk '{print $1}' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | sort -u)
    [[ -z "$ips" ]] && ips=$(timeout 3 nslookup "$domain" "$DIAG_RESOLVER" 2>/dev/null \
        | awk 'index($0, "Address:") == 1 { gsub(/#.*/, "", $2); if ($2 != "" && $2 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) print $2 }' | sort -u)
    if [[ -z "$ips" ]]; then
        log_warn "[DIAG] ${name}|${domain}|DNS_FAIL|-"
        return
    fi
    local first_ip tried=0
    while IFS= read -r ip; do
        [[ -z "$ip" ]] && continue
        if (( tried == 0 )); then
            first_ip="$ip"
            tried=1
        fi
        local out rtt
        if out=$(ping -4 -c 1 -W "$PING_TIMEOUT" "$ip" 2>&1); then
            rtt=$(echo "$out" | awk '/time=/{gsub(/.*time=/,""); gsub(/ ms.*/,""); print; exit}')
            [[ -z "$rtt" ]] && rtt="?"
            log_info "[DIAG] ${name}|${ip}|OK|${rtt}ms"
            return
        fi
    done <<< "$ips"
    log_warn "[DIAG] ${name}|${first_ip}|FAIL|-"
}

diag_ping_ips() {
    local name="$1" ip_list="$2"
    IFS=',' read -ra all_ips <<< "$ip_list"
    local idx=$_svc_diag_idx
    local start=$(( idx % ${#all_ips[@]} ))
    local tried=0 first_ip=""
    for (( i=0; i<${#all_ips[@]} && tried<2; i++ )); do
        local ci=$(( (start + i) % ${#all_ips[@]} ))
        local ip="${all_ips[$ci]}"
        if (( tried == 0 )); then first_ip="$ip"; fi
        tried=$((tried + 1))
        local out rtt
        if out=$(ping -4 -c 1 -W "$PING_TIMEOUT" "$ip" 2>&1); then
            rtt=$(echo "$out" | awk '/time=/{gsub(/.*time=/,""); gsub(/ ms.*/,""); print; exit}')
            [[ -z "$rtt" ]] && rtt="?"
            log_info "[DIAG] ${name}|${ip}|OK|${rtt}ms"
            return
        fi
    done
    log_warn "[DIAG] ${name}|${first_ip}|FAIL|-"
}

service_diagnostic() {
    local now_epoch
    now_epoch=$(date +%s)
    if (( now_epoch - _last_diag < DIAG_INTERVAL )); then
        return
    fi
    _last_diag=$now_epoch
    _svc_diag_idx=$(( (_svc_diag_idx + 1) % 10 ))

    log_info "[DIAG_BEGIN] 逐服务诊断 #${_svc_diag_idx}"

    for entry in "${DIAG_DOMAINS[@]}"; do
        local name="${entry%%:*}"
        local domain="${entry#*:}"
        diag_ping_domain "$name" "$domain"
    done

    for entry in "${DIAG_IP_TARGETS[@]}"; do
        local name="${entry%%:*}"
        local ips="${entry#*:}"
        diag_ping_ips "$name" "$ips"
    done

    for ldns in 192.168.31.254 192.168.31.251; do
        local dns_start dns_end dns_dur
        dns_start=$(date +%s%N)
        if timeout 3 getent hosts "$DNS_HOST" "$ldns" 2>/dev/null | grep -q "."; then
            dns_end=$(date +%s%N)
            dns_dur=$(( (dns_end - dns_start) / 1000000 ))
            log_info "[DIAG] 本地DNS解析|${ldns}|OK|${dns_dur}ms"
        else
            dns_end=$(date +%s%N)
            dns_dur=$(( (dns_end - dns_start) / 1000000 ))
            log_warn "[DIAG] 本地DNS解析|${ldns}|FAIL|${dns_dur}ms"
        fi
    done

    log_info "[DIAG_END] 诊断完成"
}

# ─── 内网设备 Ping 检测 ──────────────────────────────────────────────────
_last_lan_ping=0
LAN_PING_INTERVAL=30  # 每 30 秒检测一次

lan_ping_check() {
    local now_epoch
    now_epoch=$(date +%s)
    if (( now_epoch - _last_lan_ping < LAN_PING_INTERVAL )); then
        return
    fi
    _last_lan_ping=$now_epoch
    [[ ${#LAN_HOSTS[@]} -eq 0 ]] && return
    for entry in "${LAN_HOSTS[@]}"; do
        local name="${entry%% *}"
        local ip="${entry#* }"
        local out rtt
        if out=$(ping -4 -c 1 -W 2 "$ip" 2>&1); then
            rtt=$(echo "$out" | grep -oP 'time=\K[\d.]+' | head -1)
            [[ -z "$rtt" ]] && rtt="?"
            log_info "[LAN_PING] ${name}|${ip}|OK|${rtt}ms"
        else
            log_warn "[LAN_PING] ${name}|${ip}|FAIL|-"
        fi
    done
}

# ─── 主 Ping 监控 (核心，含迟滞机制) ─────────────────────────────────────
ping_monitor() {
    set +e
    log_info "[ping_monitor] v4 启动 (迟滞机制: OK=${HYSTERESIS_OK}, FAIL=${HYSTERESIS_FAIL})"

    local v4_fail=0 v6_fail=0 gw_fail=0
    local gw_down=false v4_down=false v6_down=false
    local gw_down_since="" v4_down_since="" v6_down_since=""
    local total=0 gw_ok=0 v4_ok=0 v6_ok=0
    local disc_events=0
    local last_stats=$(date +%s)
    local _last_diag=0
    local last_gw_rtt="?" last_v4_rtt="?" last_v6_rtt="?"
    # 丢包滑动窗口: 最近10个包
    local -a loss_window=()
    local loss_idx=0
    # 延迟历史用于计算抖动
    local -a rtt_history=()
    local rtt_idx=0

    while true; do
        total=$((total + 1))

        # ── Ping 网关 (IPv4) ──
        local gw_rtt="?"
        if gw_result=$(ping -4 -c 1 -W "$PING_TIMEOUT" -I "$IFACE" "$GW_V4" 2>&1); then
            gw_rtt=$(echo "$gw_result" | awk '/time=/{gsub(/.*time=/,""); gsub(/ ms.*/,""); print; exit}')
            [[ -z "$gw_rtt" ]] && gw_rtt="?"
            last_gw_rtt="$gw_rtt"
            gw_ok=$((gw_ok + 1))
            loss_window[$loss_idx]=0
            _HYST_GW_FAIL=0

            # 迟滞: 需要连续 N 次成功才确认恢复
            if $gw_down; then
                _HYST_GW_OK=$((_HYST_GW_OK + 1))
                if (( _HYST_GW_OK >= HYSTERESIS_OK )); then
                    log_event "[RECOVER_GW] 网关恢复! rtt=${gw_rtt}ms (迟滞:${_HYST_GW_OK})"
                    send_webhook "网关恢复" "网关 ${GW_V4} 已恢复, RTT=${gw_rtt}ms" "info" "recover_gw"
                    gw_down=false; gw_fail=0; _HYST_GW_OK=0
                fi
            else
                _HYST_GW_OK=0
            fi
        else
            loss_window[$loss_idx]=1
            gw_fail=$((gw_fail + 1))
            _HYST_GW_OK=0

            if (( gw_fail >= FAIL_THRESHOLD )) && ! $gw_down; then
                gw_down=true
                gw_down_since=$(date '+%Y-%m-%d %H:%M:%S')
                disc_events=$((disc_events + 1))
                log_err "[GW_DOWN] 网关不可达! 连续 ${gw_fail} 次 #$disc_events"
                send_webhook "网关不可达" "网关 ${GW_V4} 连续 ${FAIL_THRESHOLD} 次 ping 失败" "critical" "gw_down"
                take_disconnect_snapshot "gw"
            elif $gw_down && (( gw_fail % 5 == 0 )); then
                log_warn "[GW_STILL_DOWN] 网关仍不可达, 失败 ${gw_fail} 次"
            fi
        fi
        loss_idx=$(((loss_idx + 1) % 10))

        # 记录 RTT 用于抖动计算
        if [[ "$gw_rtt" != "?" ]]; then
            rtt_history[$rtt_idx]=$gw_rtt
            rtt_idx=$(((rtt_idx + 1) % 10))
        fi

        # ── Ping 外网 IPv4 (交替目标) ──
        local v4_idx=$(( total % ${#INET_V4_TARGETS[@]} ))
        local v4_target="${INET_V4_TARGETS[$v4_idx]}"
        local v4_rtt="?"
        if v4_result=$(ping -4 -c 1 -W "$PING_TIMEOUT" "$v4_target" 2>&1); then
            v4_rtt=$(echo "$v4_result" | awk '/time=/{gsub(/.*time=/,""); gsub(/ ms.*/,""); print; exit}')
            [[ -z "$v4_rtt" ]] && v4_rtt="?"
            last_v4_rtt="$v4_rtt"
            v4_ok=$((v4_ok + 1))
            _HYST_V4_FAIL=0

            if $v4_down; then
                _HYST_V4_OK=$((_HYST_V4_OK + 1))
                if (( _HYST_V4_OK >= HYSTERESIS_OK )); then
                    local dur=$(( $(date +%s) - $(date -d "$v4_down_since" +%s 2>/dev/null || echo 0) ))
                    log_event "[RECOVER_V4] IPv4 外网恢复! 断网 ${dur}s | $v4_target rtt=${v4_rtt}ms"
                    send_webhook "IPv4 恢复" "IPv4 外网已恢复, 断网 ${dur}s, RTT=${v4_rtt}ms" "info" "recover_v4"
                    v4_down=false; v4_fail=0; _HYST_V4_OK=0
                fi
            else
                _HYST_V4_OK=0
            fi
        else
            v4_fail=$((v4_fail + 1))
            _HYST_V4_OK=0

            if (( v4_fail >= FAIL_THRESHOLD )) && ! $v4_down; then
                v4_down=true
                v4_down_since=$(date '+%Y-%m-%d %H:%M:%S')
                disc_events=$((disc_events + 1))
                if ! $v6_down; then
                    log_err "[V4_DOWN_V6_ALIVE] IPv4 外网断开但 IPv6 正常! ($v4_target) #$disc_events"
                    send_webhook "IPv4 断连" "IPv4 外网断开但 IPv6 正常! 目标: $v4_target" "critical" "v4_down_v6_alive"
                    take_disconnect_snapshot "v4_only"
                else
                    log_err "[V4_DOWN] IPv4 外网断开 ($v4_target) #$disc_events"
                    send_webhook "IPv4 断连" "IPv4 外网断开! 目标: $v4_target" "critical" "v4_down"
                    take_disconnect_snapshot "all"
                fi
            elif $v4_down && (( v4_fail % 5 == 0 )); then
                if ! $v6_down; then
                    log_warn "[V4_STILL_DOWN_V6_OK] IPv4 仍断, IPv6 正常, 失败 ${v4_fail} 次"
                else
                    log_warn "[V4_STILL_DOWN] IPv4 仍断, 失败 ${v4_fail} 次"
                fi
            fi
        fi

        # ── Ping 外网 IPv6 (交替目标) ──
        local v6_idx=$(( total % ${#INET_V6_TARGETS[@]} ))
        local v6_target="${INET_V6_TARGETS[$v6_idx]}"
        local v6_rtt="?"
        if v6_result=$(ping -6 -c 1 -W "$PING_TIMEOUT" "$v6_target" 2>&1); then
            v6_rtt=$(echo "$v6_result" | awk '/time=/{gsub(/.*time=/,""); gsub(/ ms.*/,""); print; exit}')
            [[ -z "$v6_rtt" ]] && v6_rtt="?"
            last_v6_rtt="$v6_rtt"
            v6_ok=$((v6_ok + 1))
            _HYST_V6_FAIL=0

            if $v6_down; then
                _HYST_V6_OK=$((_HYST_V6_OK + 1))
                if (( _HYST_V6_OK >= HYSTERESIS_OK )); then
                    local dur=$(( $(date +%s) - $(date -d "$v6_down_since" +%s 2>/dev/null || echo 0) ))
                    log_event "[RECOVER_V6] IPv6 外网恢复! 断网 ${dur}s | $v6_target rtt=${v6_rtt}ms"
                    send_webhook "IPv6 恢复" "IPv6 外网已恢复, 断网 ${dur}s, RTT=${v6_rtt}ms" "info" "recover_v6"
                    v6_down=false; v6_fail=0; _HYST_V6_OK=0
                fi
            else
                _HYST_V6_OK=0
            fi
        else
            v6_fail=$((v6_fail + 1))
            _HYST_V6_OK=0

            if (( v6_fail >= FAIL_THRESHOLD )) && ! $v6_down; then
                v6_down=true
                v6_down_since=$(date '+%Y-%m-%d %H:%M:%S')
                if $v4_down; then
                    log_err "[V6_DOWN] IPv6 也断了! v4+v6 全断 | $v6_target"
                    send_webhook "IPv6 断连" "IPv6 也断了! v4+v6 全断 | $v6_target" "critical" "v6_down"
                else
                    log_err "[V6_DOWN_V4_ALIVE] IPv6 断开但 IPv4 正常! ($v6_target)"
                    send_webhook "IPv6 断连" "IPv6 断开但 IPv4 正常! $v6_target" "critical" "v6_down_v4_alive"
                    take_disconnect_snapshot "v6_only"
                fi
            elif $v6_down && (( v6_fail % 5 == 0 )); then
                log_warn "[V6_STILL_DOWN] IPv6 仍断, 失败 ${v6_fail} 次"
            fi
        fi

        # ── 计算丢包率和链路质量 ──
        local packet_loss=0 avg_rtt=0 jitter=0 quality=0
        local win_len=${#loss_window[@]}
        if (( win_len > 0 )); then
            local loss_sum=0
            for v in "${loss_window[@]}"; do
                loss_sum=$((loss_sum + v))
            done
            packet_loss=$((loss_sum * 100 / win_len))
        fi
        local rtt_len=${#rtt_history[@]}
        if (( rtt_len > 0 )); then
            local rtt_sum=0 rtt_min=999999 rtt_max=0
            for r in "${rtt_history[@]}"; do
                local ir=${r%.*}
                [[ -z "$ir" ]] && ir=0
                rtt_sum=$((rtt_sum + ir))
                (( ir < rtt_min )) && rtt_min=$ir
                (( ir > rtt_max )) && rtt_max=$ir
            done
            avg_rtt=$((rtt_sum / rtt_len))
            jitter=$((rtt_max - rtt_min))
            quality=$(link_quality_score "$packet_loss" "$avg_rtt" "$jitter")
        fi

        # ── 正常心跳 (~100 秒) ──
        if (( total % 50 == 0 )) && ! $gw_down && ! $v4_down && ! $v6_down; then
            if $COMPACT_LOG; then
                log_heartbeat "gw=${last_gw_rtt}ms v4=${last_v4_rtt}ms v6=${last_v6_rtt}ms loss=${packet_loss}% qlty=${quality}"
            else
                log_heartbeat "正常 | gw=${last_gw_rtt}ms v4=${last_v4_rtt}ms v6=${last_v6_rtt}ms | total=$total gw=$gw_ok v4=$v4_ok v6=$v6_ok loss=${packet_loss}% qlty=${quality}"
            fi
        fi

        # ── 定期统计 ──
        local now_epoch
        now_epoch=$(date +%s)
        if (( now_epoch - last_stats >= STATS_INTERVAL )); then
            local gw_pct=0 v4_pct=0 v6_pct=0
            (( total > 0 )) && gw_pct=$((gw_ok * 100 / total)) && v4_pct=$((v4_ok * 100 / total)) && v6_pct=$((v6_ok * 100 / total))
            log_stats "── 5分钟统计 ──"
            log_stats "  总: $total | GW: ${gw_pct}% ($gw_ok) | V4: ${v4_pct}% ($v4_ok) | V6: ${v6_pct}% ($v6_ok)"
            log_stats "  丢包率: ${packet_loss}% | 链路质量: ${quality} | 断网事件: $disc_events"
            log_stats "  状态: gw=$( $gw_down && echo DOWN || echo UP ) v4=$( $v4_down && echo DOWN || echo UP ) v6=$( $v6_down && echo DOWN || echo UP )"
            total=0; gw_ok=0; v4_ok=0; v6_ok=0; last_stats=$now_epoch
        fi

        # ── 逐服务诊断 ──
        service_diagnostic

        # ── 内网设备检测 ──
        lan_ping_check

        sleep "$PING_INTERVAL"
    done
}

###############################################################################
# 子进程 2: ARP + IPv6 ND 监控 (使用 mtime 代替 md5sum，降低 CPU)
###############################################################################
arp_monitor() {
    set +e
    log_info "[arp_monitor] 启动 (含 IPv6 ND)"
    local prev_v4_mtime=0 prev_v6_hash=""
    local prev_v4_file="/tmp/arp_v4_prev_$$"
    local prev_v6_file="/tmp/nd_v6_prev_$$"

    while true; do
        # IPv4 ARP: 用文件 mtime 检测变化 (避免 md5sum)
        local arp_file="/proc/net/arp"
        local cur_v4_mtime
        cur_v4_mtime=$(stat -c%Y "$arp_file" 2>/dev/null || echo 0)

        if (( prev_v4_mtime != 0 && cur_v4_mtime != prev_v4_mtime )); then
            local v4_arp
            v4_arp=$(cat "$arp_file" | grep "$IFACE" | grep -v "00:00:00:00:00:00")
            local now_file="/tmp/arp_v4_now_$$"
            echo "$v4_arp" | awk '{print $1}' | sort > "$now_file"
            if [[ -f "$prev_v4_file" ]]; then
                local disappeared appeared
                disappeared=$(comm -23 "$prev_v4_file" "$now_file")
                appeared=$(comm -13 "$prev_v4_file" "$now_file")
                if [[ -n "$disappeared" ]]; then
                    local cnt
                    cnt=$(echo "$disappeared" | wc -l)
                    log_event "[ARP_LOST] $cnt 个 IPv4 设备离开"
                    if (( cnt > 5 )); then
                        log_err "[MASS_LEAVE] $cnt 个设备同时消失! 可能本机断网"
                    fi
                fi
                if [[ -n "$appeared" ]]; then
                    log_event "[ARP_NEW] $(echo "$appeared" | wc -l) 个 IPv4 新设备"
                fi
            fi
            cp "$now_file" "$prev_v4_file"
            rm -f "$now_file"
        elif (( prev_v4_mtime == 0 )); then
            local v4_arp
            v4_arp=$(cat "$arp_file" | grep "$IFACE" | grep -v "00:00:00:00:00:00")
            echo "$v4_arp" | awk '{print $1}' | sort > "$prev_v4_file"
        fi
        prev_v4_mtime=$cur_v4_mtime

        # IPv6 邻居发现
        local v6_nd v6_hash
        v6_nd=$(ip -6 neigh show dev "$IFACE" 2>/dev/null | grep -v "^$" || true)
        v6_hash=$(echo "$v6_nd" | md5sum | cut -d' ' -f1)

        if [[ -n "$prev_v6_hash" ]] && [[ "$v6_hash" != "$prev_v6_hash" ]]; then
            local v6_file="/tmp/nd_v6_now_$$"
            echo "$v6_nd" | awk '{print $1}' | sort > "$v6_file"
            if [[ -f "$prev_v6_file" ]]; then
                local v6_disappeared v6_appeared
                v6_disappeared=$(comm -23 "$prev_v6_file" "$v6_file")
                v6_appeared=$(comm -13 "$prev_v6_file" "$v6_file")
                if [[ -n "$v6_disappeared" ]]; then
                    log_event "[ND_LOST] $(echo "$v6_disappeared" | wc -l) 个 IPv6 邻居消失"
                fi
                if [[ -n "$v6_appeared" ]]; then
                    log_event "[ND_NEW] $(echo "$v6_appeared" | wc -l) 个 IPv6 新邻居"
                fi
            fi
            cp "$v6_file" "$prev_v6_file"
            rm -f "$v6_file"
        elif [[ -z "$prev_v6_hash" ]]; then
            echo "$v6_nd" | awk '{print $1}' | sort > "$prev_v6_file"
        fi
        prev_v6_hash="$v6_hash"

        sleep "$ARP_INTERVAL"
    done
}

###############################################################################
# 子进程 3: 路由表监控
###############################################################################
route_monitor() {
    set +e
    log_info "[route_monitor] 启动 (v4+v6)"
    local prev_v4_route prev_v6_route prev_v4_hash prev_v6_hash
    prev_v4_route=$(ip -4 route show)
    prev_v4_hash=$(echo "$prev_v4_route" | md5sum)
    prev_v6_route=$(ip -6 route show)
    prev_v6_hash=$(echo "$prev_v6_route" | md5sum)

    while true; do
        local cur_v4 cur_v6 cur_v4_hash cur_v6_hash
        cur_v4=$(ip -4 route show)
        cur_v4_hash=$(echo "$cur_v4" | md5sum)
        cur_v6=$(ip -6 route show)
        cur_v6_hash=$(echo "$cur_v6" | md5sum)

        if [[ "$cur_v4_hash" != "$prev_v4_hash" ]]; then
            local has_default_v4
            has_default_v4=$(echo "$cur_v4" | grep "^default" | head -1)
            if [[ -n "$has_default_v4" ]]; then
                log_event "[ROUTE_V4_CHANGE] IPv4 路由变化 (涉及默认路由)"
            else
                log_info "[ROUTE_V4_INFO] IPv4 路由变化 (不涉及默认路由)"
            fi
            diff <(echo "$prev_v4_route") <(echo "$cur_v4") | head -10 | while read -r line; do
                [[ -n "$line" ]] && log_event "  $line"
            done
            [[ -z "$has_default_v4" ]] && log_err "[NO_V4_DEFAULT] IPv4 默认路由丢失!"
            prev_v4_route="$cur_v4"
            prev_v4_hash="$cur_v4_hash"
        fi

        if [[ "$cur_v6_hash" != "$prev_v6_hash" ]]; then
            local v6_diff real_changes
            v6_diff=$(diff <(echo "$prev_v6_route") <(echo "$cur_v6") 2>/dev/null | grep -E '^<|^>' || true)
            real_changes=$(echo "$v6_diff" | grep -v 'via fe80::' || true)
            if [[ -n "$real_changes" ]]; then
                local has_default_v6
                has_default_v6=$(echo "$cur_v6" | grep "^default" | head -1)
                if [[ -n "$has_default_v6" ]]; then
                    log_event "[ROUTE_V6_CHANGE] IPv6 路由变化 (实质性, 涉及默认路由)"
                else
                    log_info "[ROUTE_V6_INFO] IPv6 路由变化 (不涉及默认路由)"
                fi
                echo "$v6_diff" | head -10 | while read -r line; do
                    [[ -n "$line" ]] && log_event "  $line"
                done
            else
                log_info "[ROUTE_V6_SLAAC] IPv6 SLAAC 邻居刷新 (已忽略)"
            fi
            prev_v6_route="$cur_v6"
            prev_v6_hash="$cur_v6_hash"
        fi

        sleep "$ROUTE_INTERVAL"
    done
}

###############################################################################
# 子进程 4: 系统事件监控
###############################################################################
event_monitor() {
    set +e
    log_info "[event_monitor] 启动"
    local prev_carrier="" prev_operstate="" prev_v4_ip="" prev_v6_ip=""
    local last_dmesg_ts=""
    local last_dhcp_v4_log=0 last_dhcp_v6_log=0 last_traffic_log=0

    while true; do
        local now_epoch
        now_epoch=$(date +%s)

        # 一次 ip 命令获取所有地址信息
        local ip4_out ip6_out
        ip4_out=$(ip -4 addr show "$IFACE" 2>/dev/null)
        ip6_out=$(ip -6 addr show "$IFACE" scope global 2>/dev/null)

        local carrier operstate v4_ip v6_ip
        carrier=$(cat /sys/class/net/$IFACE/carrier 2>/dev/null || echo "unknown")
        operstate=$(cat /sys/class/net/$IFACE/operstate 2>/dev/null || echo "unknown")
        v4_ip=$(echo "$ip4_out" | grep inet | awk '{print $2}' | head -1 || echo "none")
        v6_ip=$(echo "$ip6_out" | grep inet6 | awk '{print $2}' | head -1 || echo "none")

        if [[ -n "$prev_carrier" ]]; then
            [[ "$carrier" != "$prev_carrier" ]] && {
                log_event "[CARRIER_CHANGE] 物理链路: $prev_carrier → $carrier"
                [[ "$carrier" == "0" ]] && log_err "[LINK_DOWN] 物理链路断开!"
            }
            [[ "$operstate" != "$prev_operstate" ]] && log_event "[OPERSTATE] $prev_operstate → $operstate"
            [[ "$v4_ip" != "$prev_v4_ip" ]] && {
                log_event "[V4_IP_CHANGE] IPv4: $prev_v4_ip → $v4_ip"
                [[ "$v4_ip" == "none" ]] && log_err "[V4_IP_LOST] IPv4 地址丢失! DHCP?"
            }
            [[ "$v6_ip" != "$prev_v6_ip" ]] && log_event "[V6_IP_CHANGE] IPv6: $prev_v6_ip → $v6_ip"
        fi
        prev_carrier="$carrier"; prev_operstate="$operstate"
        prev_v4_ip="$v4_ip"; prev_v6_ip="$v6_ip"

        sleep "$EVENT_INTERVAL"
    done
}

###############################################################################
# 子进程 5: DNS 双栈监控
###############################################################################
dns_monitor() {
    set +e
    log_info "[dns_monitor] 启动 (v4+v6)"

    while true; do
        local v4_start v4_end v4_dur v6_start v6_end v6_dur

        v4_start=$(date +%s%N)
        if timeout 5 getent hosts "$DNS_HOST" 2>/dev/null | grep -q "."; then
            v4_end=$(date +%s%N); v4_dur=$(( (v4_end - v4_start) / 1000000 ))
            (( v4_dur > 2000 )) && log_warn "[DNS_V4_SLOW] IPv4 DNS ${v4_dur}ms"
        else
            v4_end=$(date +%s%N); v4_dur=$(( (v4_end - v4_start) / 1000000 ))
            if timeout 5 nslookup "$DNS_HOST" 2>&1 | grep -q "Address:"; then
                log_info "[DNS_V4] getent 失败但 nslookup 成功 (${v4_dur}ms)"
            else
                log_warn "[DNS_V4_FAIL] IPv4 DNS 失败 (${v4_dur}ms)"
            fi
        fi

        v6_start=$(date +%s%N)
        if timeout 5 getent hosts "$DNS_HOST" 2400:3200::1 2>/dev/null | grep -q "."; then
            v6_end=$(date +%s%N); v6_dur=$(( (v6_end - v6_start) / 1000000 ))
            (( v6_dur > 2000 )) && log_warn "[DNS_V6_SLOW] IPv6 DNS ${v6_dur}ms"
        else
            v6_end=$(date +%s%N); v6_dur=$(( (v6_end - v6_start) / 1000000 ))
            if timeout 5 nslookup "$DNS_HOST" 2400:3200::1 2>&1 | grep -q "Address:"; then
                log_info "[DNS_V6] getent 失败但 nslookup 成功 (${v6_dur}ms)"
            else
                log_warn "[DNS_V6_FAIL] IPv6 DNS 失败 (${v6_dur}ms)"
            fi
        fi

        # v4 失败但 v6 成功 → 专项告警
        if ! timeout 5 getent hosts "$DNS_HOST" 2>/dev/null | grep -q "."; then
            if timeout 5 getent hosts "$DNS_HOST" 2400:3200::1 2>/dev/null | grep -q "."; then
                log_err "[DNS_V4_DOWN_V6_OK] IPv4 DNS 失败但 IPv6 DNS 正常!"
            fi
        fi

        dns_pollution_detect "$DNS_HOST"

        sleep 20
    done
}

###############################################################################
# 子进程 6: 连接追踪监控
###############################################################################
conntrack_monitor() {
    set +e
    log_info "[conntrack_monitor] 启动"

    while true; do
        local count max pct
        count=$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || echo 0)
        max=$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || echo 1)
        pct=$((count * 100 / max))

        if (( pct > 80 )); then
            log_err "[CONNTRACK_HIGH] conntrack ${pct}% ($count/$max) — 可能 NAT 表满导致 v4 断网!"
        elif (( pct > 50 )); then
            log_warn "[CONNTRACK_WARN] conntrack ${pct}% ($count/$max)"
        fi

        if (( $(date +%s) % STATS_INTERVAL < CONN_INTERVAL )); then
            log_info "[CONNTRACK] $count/$max (${pct}%)"
        fi

        sleep "$CONN_INTERVAL"
    done
}

###############################################################################
# 子进程 7: Traceroute 路径监控
###############################################################################
TRACE_INTERVAL=300
TRACE_BASE_DIR="${LOG_DIR}/trace_baselines"
TRACE_CUR_DIR="${LOG_DIR}/trace_current"
TRACE_TARGETS_V4=("baidu.com" "pvp.qq.com")
TRACE_TARGETS_V6=("pvp.qq.com")
TRACE_RESOLVER_V4="223.5.5.5"
TRACE_RESOLVER_V6="2400:3200::1"

resolve_domain() {
    local domain="$1" resolver="$2" proto="$3"
    local result=""
    if [[ "$proto" == "v6" ]]; then
        result=$(timeout 5 getent hosts "$domain" "$resolver" 2>/dev/null | awk '{print $1}' | grep ':' | tail -1)
        [[ -z "$result" ]] && result=$(timeout 5 nslookup -type=AAAA "$domain" "$resolver" 2>/dev/null \
            | awk 'index($0, "Address:") == 1 { gsub(/#.*/, "", $2); if ($2 != "" && $2 ~ /:/) print $2 }' | tail -1)
    else
        result=$(timeout 5 getent hosts "$domain" "$resolver" 2>/dev/null | awk '{print $1}' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | tail -1)
        [[ -z "$result" ]] && result=$(timeout 5 nslookup "$domain" "$resolver" 2>/dev/null \
            | awk 'index($0, "Address:") == 1 { gsub(/#.*/, "", $2); if ($2 != "" && $2 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) print $2 }' | tail -1)
    fi
    echo "$result"
}

extract_path() {
    awk '/^[[:space:]]*[0-9]+[[:space:]]+[0-9a-fA-F.:]+/ {
             ip = $2
             if (ip !~ /^[0-9a-fA-F.:]+$/) ip = $3
             print $1":"ip
         }' "$1" 2>/dev/null | head -30
}

HOP_TOLERANCE=2
compare_paths() {
    local base="$1" cur="$2"
    local diff_count=0
    local all_hops
    all_hops=$(echo -e "$base\n$cur" | awk -F: '{print $1}' | sort -n -u)
    while IFS= read -r hop; do
        [[ -z "$hop" ]] && continue
        local base_ip cur_ip
        base_ip=$(echo "$base" | awk -F: -v h="$hop" '$1==h{print $2}')
        cur_ip=$(echo "$cur" | awk -F: -v h="$hop" '$1==h{print $2}')
        [[ "$base_ip" != "$cur_ip" ]] && diff_count=$((diff_count + 1))
    done <<< "$all_hops"
    echo "$diff_count"
}

traceroute_monitor() {
    set +e
    log_info "[traceroute_monitor] 启动 (每 ${TRACE_INTERVAL}s)"
    mkdir -p "$TRACE_BASE_DIR" "$TRACE_CUR_DIR"

    # 采集基线
    log_info "[traceroute_monitor] 采集基线路由..."
    for domain in "${TRACE_TARGETS_V4[@]}"; do
        local resolved
        resolved=$(resolve_domain "$domain" "$TRACE_RESOLVER_V4" "v4")
        if [[ -z "$resolved" ]]; then
            log_warn "[TRACE_BASE] DNS 解析失败: $domain via $TRACE_RESOLVER_V4"
            continue
        fi
        log_info "[TRACE_BASE] 解析 $domain → $resolved"
        timeout 15 traceroute -4 -n -m 20 -w 1 "$resolved" > "${TRACE_BASE_DIR}/v4_${domain}.txt" 2>&1 || true
    done
    for domain in "${TRACE_TARGETS_V6[@]}"; do
        local resolved
        resolved=$(resolve_domain "$domain" "$TRACE_RESOLVER_V6" "v6")
        if [[ -z "$resolved" ]]; then
            log_warn "[TRACE_BASE] DNS 解析失败: $domain via $TRACE_RESOLVER_V6"
            continue
        fi
        log_info "[TRACE_BASE] 解析 $domain → $resolved"
        timeout 15 traceroute -6 -n -m 20 -w 1 "$resolved" > "${TRACE_BASE_DIR}/v6_${domain}.txt" 2>&1 || true
    done

    while true; do
        sleep "$TRACE_INTERVAL"

        for domain in "${TRACE_TARGETS_V4[@]}"; do
            local resolved
            resolved=$(resolve_domain "$domain" "$TRACE_RESOLVER_V4" "v4")
            if [[ -z "$resolved" ]]; then
                log_warn "[TRACE_V4_DNS_FAIL] DNS 解析失败: $domain"
                continue
            fi
            local cur_file="${TRACE_CUR_DIR}/v4_${domain}.txt"
            local base_file="${TRACE_BASE_DIR}/v4_${domain}.txt"
            timeout 15 traceroute -4 -n -m 20 -w 1 "$resolved" > "$cur_file" 2>&1 || true

            if [[ -f "$base_file" ]] && [[ -f "$cur_file" ]]; then
                local base_ips cur_ips hop_diff
                base_ips=$(extract_path "$base_file")
                cur_ips=$(extract_path "$cur_file")
                hop_diff=$(compare_paths "$base_ips" "$cur_ips")

                if (( hop_diff > HOP_TOLERANCE )); then
                    log_event "[TRACE_V4_CHANGE] IPv4 路径变化 → $domain ($resolved) ($hop_diff 跳不同)"
                    cp "$cur_file" "${base_file}.prev"
                    cp "$cur_file" "$base_file"
                elif (( hop_diff > 0 )); then
                    log_info "[TRACE_V4] 路径有 $hop_diff 跳变化(在容差内) → $domain"
                else
                    log_info "[TRACE_V4] 路径正常 → $domain ($resolved)"
                fi
            fi
        done
    done
}

###############################################################################
# 子进程 9: 爱快 WAN 状态监控 (含联通2告警)
###############################################################################
ikuai_wan_monitor() {
    set +e
    log_info "[ikuai_wan_monitor] 启动 (联通2告警监控)"

    local IKUAI_MON="/home/li/ikuai_mon.py"
    local HYST_OK=3
    local HYST_FAIL=3
    local _HYST_WAN2_OK=0
    local _HYST_WAN2_FAIL=0
    local wan2_down=false
    local last_log_line=0

    # 确保 ikuai_mon.py 在运行
    if [[ ! -f "$IKUAI_MON" ]]; then
        log_err "[ikuai_wan] $IKUAI_MON 不存在!"
        return 1
    fi

    while true; do
        # 从 ikuai_mon.py --monitor 的日志中提取 WAN 状态
        # 日志格式: [IKUAI_WAN] WAN1: status=2 internet=2 pppoe_ip=...
        local log_file="$LOG_FILE"
        [[ ! -f "$log_file" ]] && log_file="${LOG_DIR}/current.log"

        if [[ ! -f "$log_file" ]]; then
            sleep 15
            continue
        fi

        # 取最新的 WAN 状态行
        local wan_lines
        wan_lines=$(tail -50 "$log_file" 2>/dev/null | grep "\[IKUAI_WAN\]" | tail -10)

        if [[ -z "$wan_lines" ]]; then
            sleep 15
            continue
        fi

        # 解析每个 WAN 口状态
        local wan2_status="" wan2_internet=""
        while IFS= read -r line; do
            local wan_name wan_status wan_internet
            wan_name=$(echo "$line" | grep -oP 'WAN\d+:' | head -1 | tr -d ':')
            wan_status=$(echo "$line" | grep -oP 'status=\K\d+' | head -1)
            wan_internet=$(echo "$line" | grep -oP 'internet=\K\d+' | head -1)

            if [[ "$wan_name" == "WAN2" ]]; then
                wan2_status="$wan_status"
                wan2_internet="$wan_internet"
            fi

            # 记录所有 WAN 状态到主日志
            log_info "[IKUAI_WAN_STATUS] ${wan_name}: status=${wan_status} internet=${wan_internet}"
        done <<< "$wan_lines"

        # ── 联通2 告警判断 ──
        if [[ -n "$wan2_status" ]]; then
            local wan2_online=false
            # status=2 且 internet=2 表示在线
            if [[ "$wan2_status" == "2" ]] && [[ "$wan2_internet" == "2" ]]; then
                wan2_online=true
            fi

            if $wan2_online; then
                _HYST_WAN2_FAIL=0
                _HYST_WAN2_OK=$((_HYST_WAN2_OK + 1))

                if $wan2_down && (( _HYST_WAN2_OK >= HYSTERESIS_OK )); then
                    wan2_down=false
                    _HYST_WAN2_OK=0
                    log_event "[WAN2_RECOVER] 联通2 (WAN2) 已恢复在线"
                    send_webhook "联通2恢复" "联通2 (WAN2) PPPoE状态: 在线, 互联网: 可达" "info" "wan2_recover"
                fi
            else
                _HYST_WAN2_OK=0
                _HYST_WAN2_FAIL=$((_HYST_WAN2_FAIL + 1))

                if (( _HYST_WAN2_FAIL >= HYSTERESIS_FAIL )) && ! $wan2_down; then
                    wan2_down=true
                    log_err "[WAN2_DOWN] 联通2 (WAN2) 离线! status=$wan2_status internet=$wan2_internet"
                    send_webhook "联通2离线" "联通2 (WAN2) 检测到离线\nPPPoE状态: ${wan2_status:-未知}\n互联网: ${wan2_internet:-未知}" "critical" "wan2_down"
                elif $wan2_down && (( _HYST_WAN2_FAIL % 10 == 0 )); then
                    log_warn "[WAN2_STILL_DOWN] 联通2 仍离线 (${_HYST_WAN2_FAIL}次检查)"
                fi
            fi
        fi

        sleep 15
    done
}

###############################################################################
# 清理与启动
###############################################################################
cleanup() {
    log_info "监控脚本停止 (PID $$)"
    rm -f "$PID_FILE"
    kill $(jobs -p) 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT EXIT

log_info "=============================================="
log_info "网络监控 v4 启动 (优化版)"
log_info "接口: $IFACE"
log_info "网关: v4=$GW_V4 v6=$GW_V6"
log_info "外网v4: ${INET_V4_TARGETS[*]}"
log_info "外网v6: ${INET_V6_TARGETS[*]}"
log_info "日志目录: $LOG_DIR (COMPACT=$COMPACT_LOG)"
log_info "迟滞: OK=${HYSTERESIS_OK} FAIL=${FAIL_THRESHOLD}"
log_info "=============================================="

ping_monitor &
arp_monitor &
route_monitor &
event_monitor &
dns_monitor &
conntrack_monitor &
traceroute_monitor &
http_latency_monitor &
    stats_collector &
    # 爱快路由器监控（独立进程）
    python3 /home/li/ikuai_mon.py --monitor >> "$LOG_FILE" 2>&1 &

    # 爱快 WAN 状态监控 (含联通2告警)
    ikuai_wan_monitor &

log_info "9 个监控子进程已启动"

# 主循环 (每60秒检查 NAS 可用性 + 日志轮转)
while true; do
    sleep 60
    rotate_log
    check_nas_switch
    # 健康检查: 核心进程必须存活
    alive=0
    for pid in $(jobs -p); do
        kill -0 "$pid" 2>/dev/null && alive=$((alive + 1))
    done
    (( alive < 8 )) && log_warn "[HEALTH] 子进程异常: $alive (预期 9 个)"
done
