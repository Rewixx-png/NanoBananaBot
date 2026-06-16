#!/bin/bash
# Запускается кроном каждые 5 минут. flock не даёт двум копиям работать одновременно.
[ "${FLOCKER}" != "$0" ] && exec env FLOCKER="$0" flock -en "$0" "$0" "$@" || true

# Пересобирает hatani-sandbox только при превышении порогов:
#   - Docker (images+cache) > 2GB
#   - RAM системы > 90%
#   - CPU системы > 90%

IMAGE="hatani-sandbox:latest"
PROJECT_DIR="/root/Projects/NanoHatani"

# ── Размер Docker внутри (images + containers + cache) ──────────
DOCKER_SIZE_MB=$(docker system df --format '{{.Size}}' 2>/dev/null \
    | awk '
        function parse(s,   n,u) {
            n = s+0; u = s
            gsub(/[0-9.]/,"",u)
            if (u ~ /GB/) return n * 1024
            if (u ~ /TB/) return n * 1024 * 1024
            return n
        }
        { total += parse($0) }
        END { printf "%.0f", total }
    ')
DOCKER_SIZE_MB=${DOCKER_SIZE_MB:-0}

# ── RAM % ────────────────────────────────────────────────────────
MEM_TOTAL=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
MEM_AVAIL=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
MEM_USED=$(( MEM_TOTAL - MEM_AVAIL ))
MEM_PCT=$(( MEM_USED * 100 / MEM_TOTAL ))

# ── CPU % (два снимка /proc/stat с интервалом 1с) ────────────────
read_cpu_stat() {
    awk '/^cpu / {print $2+$3+$4+$5+$6+$7+$8, $5}' /proc/stat
}
read -r TOTAL1 IDLE1 <<< "$(read_cpu_stat)"
sleep 1
read -r TOTAL2 IDLE2 <<< "$(read_cpu_stat)"
TOTAL_DIFF=$(( TOTAL2 - TOTAL1 ))
IDLE_DIFF=$(( IDLE2 - IDLE1 ))
if [ "$TOTAL_DIFF" -gt 0 ]; then
    CPU_PCT=$(( (TOTAL_DIFF - IDLE_DIFF) * 100 / TOTAL_DIFF ))
else
    CPU_PCT=0
fi

# ── Проверка порогов ────────────────────────────────────────────
REBUILD=0
REASONS=()

if [ "$DOCKER_SIZE_MB" -gt 2048 ]; then   # 2GB
    REBUILD=1
    REASONS+=("docker=${DOCKER_SIZE_MB}MB>2GB")
fi

if [ "$MEM_PCT" -gt 90 ]; then
    REBUILD=1
    REASONS+=("RAM=${MEM_PCT}%>90%")
fi

if [ "$CPU_PCT" -gt 90 ]; then
    REBUILD=1
    REASONS+=("CPU=${CPU_PCT}%>90%")
fi

# ── Решение ──────────────────────────────────────────────────────
if [ "$REBUILD" -eq 1 ]; then
    REASON_STR=$(IFS=', '; echo "${REASONS[*]}")
    echo "[docker-health] Пересобираю образ: $REASON_STR"
    docker system prune -f --filter "label!=keep" 2>/dev/null || true
    cd "$PROJECT_DIR" && docker build -f Dockerfile.sandbox -t "$IMAGE" . 2>&1 | tail -10
    echo "[docker-health] Образ пересобран"
else
    echo "[docker-health] Норма — docker=${DOCKER_SIZE_MB}MB, RAM=${MEM_PCT}%, CPU=${CPU_PCT}% — пересборка не нужна"
fi
