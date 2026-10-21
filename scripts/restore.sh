#!/usr/bin/env bash
# CtrlStore 恢复（企划第 21.2 节）。
#
#   scripts/restore.sh /var/backups/ctrlstore/20261021-030000
#
# 只接受清单 status=complete 的目录。顺序：停服务 -> 恢复数据库与文件 ->
# 恢复配置 -> fsck 重建公开链接 -> 启动服务。恢复与升级是两件事：先回到
# 清单上的应用版本，核对无误后再单独升级。
#
# 恢复前请先确认目录内容；本脚本会覆盖数据目录下的数据库与 originals /
# derivatives。public/ 与 tmp/ 不恢复，公开链接由 fsck 重建。
set -euo pipefail

SOURCE="${1:-}"
DATA_DIR="${CTRLSTORE_DATA_DIR:-/var/lib/ctrlstore}"
CONFIG="${CTRLSTORE_CONFIG:-/etc/ctrlstore/config.toml}"
SECRETS="${CTRLSTORE_SECRETS_FILE:-/etc/ctrlstore/secrets.env}"
SERVICES="${CTRLSTORE_SERVICES:-ctrlstore-api@0 ctrlstore-api@1 ctrlstore-worker}"
CTRLSTORE_BIN="${CTRLSTORE_BIN:-ctrlstore}"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
die() { log "恢复中止：$*"; exit 1; }

RESTARTED=0
finish() {
    status=$?
    if [ "${RESTARTED}" -eq 1 ]; then
        log "重新启动服务：${SERVICES}"
        # shellcheck disable=SC2086
        systemctl start ${SERVICES} || true
    fi
    exit "${status}"
}
trap finish EXIT

if [ "$(id -u)" -ne 0 ]; then
    die "需要 root 运行"
fi
[ -n "${SOURCE}" ] || die "用法：restore.sh <备份目录>"
[ -d "${SOURCE}" ] || die "备份目录不存在：${SOURCE}"
MANIFEST="${SOURCE}/manifest.json"
[ -f "${MANIFEST}" ] || die "缺少清单：${MANIFEST}"
grep -q '"status":"complete"' "${MANIFEST}" || die "清单不是完整备份，不能用于恢复"
[ -f "${SOURCE}/ctrlstore.db" ] || die "备份里没有 ctrlstore.db"

log "清单：$(cat "${MANIFEST}")"

if [ -n "${SERVICES}" ]; then
    log "暂停服务：${SERVICES}"
    # 先置位再停：stop 失败时退出钩子仍会尝试拉起服务。
    RESTARTED=1
    # shellcheck disable=SC2086
    systemctl stop ${SERVICES}
fi

log "恢复数据库"
mkdir -p "${DATA_DIR}"
rm -f "${DATA_DIR}/ctrlstore.db-wal" "${DATA_DIR}/ctrlstore.db-shm"
install -m 0640 "${SOURCE}/ctrlstore.db" "${DATA_DIR}/ctrlstore.db"

for name in originals derivatives; do
    log "恢复 ${name}"
    rm -rf "${DATA_DIR:?}/${name}"
    mkdir -p "${DATA_DIR}/${name}"
    if [ -d "${SOURCE}/${name}" ]; then
        cp -a "${SOURCE}/${name}/." "${DATA_DIR}/${name}/"
    fi
done

if [ -f "${SOURCE}/config.toml" ]; then
    log "恢复配置到 ${CONFIG}"
    install -m 0640 "${SOURCE}/config.toml" "${CONFIG}"
fi

if [ -f "${SOURCE}/secrets.env" ]; then
    log "恢复密钥到 ${SECRETS}"
    install -m 0600 "${SOURCE}/secrets.env" "${SECRETS}"
else
    log "警告：备份里没有 secrets.env；已有会话会失效，访问统计口径也会断"
fi

log "修正属主"
chown -R ctrlstore:ctrlstore "${DATA_DIR}"

# fsck 需要与 API 相同的配置与密钥。systemd 的 EnvironmentFile 不会自动出现在
# 这里，所以手动读一次；文件是 VAR=value 形式。
# shellcheck disable=SC1090
if [ -f "${SECRETS}" ]; then
    set -a
    . "${SECRETS}"
    set +a
fi

log "按已提交的发布快照重建公开链接"
runuser -u ctrlstore -- env CTRLSTORE_CONFIG="${CONFIG}" \
    "${CTRLSTORE_BIN}" fsck --fix-public-links --skip-hashes

log "恢复完成。请按第 21.2 节核对：集合 API、一条原图、一条视频、权限与版本。"
log "确认无误后，若需要升级，请单独执行迁移。"
