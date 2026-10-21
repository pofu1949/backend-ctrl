#!/usr/bin/env bash
# CtrlStore 停写备份（企划第 21.1 节）。
#
# 顺序：停 API 与 worker -> WAL checkpoint -> SQLite backup -> 复制 originals /
# derivatives / 配置 -> 写清单 -> 重启服务。public/ 与 tmp/ 不备份，恢复后由
# `ctrlstore fsck --fix-public-links` 按已提交的发布快照重建。
#
# 清单最后一步才写，并且 status=complete；中途失败的目录会被改名成 *.failed，
# 不会被 restore.sh 当成恢复源。
#
# 可用环境变量覆盖默认路径：
#   CTRLSTORE_DATA_DIR / CTRLSTORE_CONFIG / CTRLSTORE_BACKUP_DIR /
#   CTRLSTORE_SERVICES / CTRLSTORE_BIN
set -euo pipefail

DATA_DIR="${CTRLSTORE_DATA_DIR:-/var/lib/ctrlstore}"
CONFIG="${CTRLSTORE_CONFIG:-/etc/ctrlstore/config.toml}"
SECRETS="${CTRLSTORE_SECRETS_FILE:-/etc/ctrlstore/secrets.env}"
BACKUP_ROOT="${CTRLSTORE_BACKUP_DIR:-/var/backups/ctrlstore}"
SERVICES="${CTRLSTORE_SERVICES:-ctrlstore-api@0 ctrlstore-api@1 ctrlstore-worker}"
CTRLSTORE_BIN="${CTRLSTORE_BIN:-ctrlstore}"

DB="${DATA_DIR}/ctrlstore.db"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
TARGET="${BACKUP_ROOT}/${STAMP}"
MANIFEST="${TARGET}/manifest.json"
RESTARTED=0

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

finish() {
    status=$?
    if [ "${status}" -ne 0 ] && [ -d "${TARGET}" ]; then
        # 失败的目录不能当恢复源：改名保留现场，方便查原因。
        mv "${TARGET}" "${TARGET}.failed" 2>/dev/null || true
        log "备份未完成，目录已标记为 ${TARGET}.failed"
    fi
    if [ "${RESTARTED}" -eq 1 ]; then
        log "重新启动服务：${SERVICES}"
        # shellcheck disable=SC2086
        systemctl start ${SERVICES} || true
    fi
    exit "${status}"
}
trap finish EXIT

if [ "$(id -u)" -ne 0 ]; then
    log "需要 root 运行（要停写并复制 /var/lib/ctrlstore）"
    exit 1
fi
command -v sqlite3 >/dev/null 2>&1 || { log "缺少 sqlite3"; exit 1; }
[ -f "${DB}" ] || { log "找不到数据库：${DB}"; exit 1; }

umask 077
mkdir -p "${TARGET}"
STARTED="$(date -u +%FT%TZ)"

if [ -n "${SERVICES}" ]; then
    log "暂停服务：${SERVICES}"
    # 先置位再停：即使 stop 失败，退出钩子也会尝试把已经停掉的服务拉起来。
    RESTARTED=1
    # shellcheck disable=SC2086
    systemctl stop ${SERVICES}
fi

log "WAL checkpoint"
sqlite3 "${DB}" "PRAGMA wal_checkpoint(TRUNCATE);"
log "备份 SQLite"
sqlite3 "${DB}" ".backup '${TARGET}/ctrlstore.db'"

for name in originals derivatives; do
    log "复制 ${name}"
    mkdir -p "${TARGET}/${name}"
    if [ -d "${DATA_DIR}/${name}" ]; then
        cp -a "${DATA_DIR}/${name}/." "${TARGET}/${name}/"
    fi
done

if [ -f "${CONFIG}" ]; then
    cp -a "${CONFIG}" "${TARGET}/config.toml"
fi

# 密钥单独备份并保持 0600：整机丢失后要能恢复出同一套会话密钥与访问统计
# HMAC，否则同一天会被算成两批人，已有会话也全部失效。
if [ -f "${SECRETS}" ]; then
    install -m 0600 "${SECRETS}" "${TARGET}/secrets.env"
else
    log "警告：找不到密钥文件 ${SECRETS}，这份备份无法恢复登录与会话"
fi

APP_VERSION="$("${CTRLSTORE_BIN}" --version 2>/dev/null | awk '{print $2}')"
SCHEMA_VERSION="$(sqlite3 "${TARGET}/ctrlstore.db" 'SELECT version_num FROM alembic_version;')"
ORIGINALS="$(find "${TARGET}/originals" -type f | wc -l)"
DERIVATIVES="$(find "${TARGET}/derivatives" -type f | wc -l)"
COMPLETED="$(date -u +%FT%TZ)"

cat > "${MANIFEST}" <<JSON
{"status":"complete","app_version":"${APP_VERSION}","schema_version":"${SCHEMA_VERSION}","started_at":"${STARTED}","completed_at":"${COMPLETED}","files":{"originals":${ORIGINALS},"derivatives":${DERIVATIVES}}}
JSON

log "备份完成：${TARGET}（应用 ${APP_VERSION}，迁移 ${SCHEMA_VERSION}）"
