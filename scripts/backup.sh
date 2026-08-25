#!/usr/bin/env bash
# Резервная копия базы.
#
# Копировать файл SQLite обычным cp нельзя: база работает в режиме WAL, и часть
# свежих записей лежит в отдельном -wal файле. Простая копия основного файла
# рискует оказаться без них или вовсе битой. Штатный способ — команда
# `.backup`, которая снимает согласованный снимок на работающей базе.
#
#   ./scripts/backup.sh                      # в ./backups
#   ./scripts/backup.sh /mnt/disk/backups    # в указанный каталог
#
# В crontab на сервере, ежедневно в 04:00:
#   0 4 * * * cd /opt/alert-bot && ./scripts/backup.sh >> backups/backup.log 2>&1

set -euo pipefail

DB_PATH="${DB_PATH:-data/alert_bot.db}"
DEST="${1:-backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

if [[ ! -f "$DB_PATH" ]]; then
    echo "База не найдена: $DB_PATH" >&2
    exit 1
fi

mkdir -p "$DEST"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
TARGET="$DEST/alert_bot-$STAMP.db"

if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB_PATH" ".backup '$TARGET'"
else
    # sqlite3 может отсутствовать на голом сервере — тот же снимок средствами
    # Python, он есть всегда, раз работает сам бот.
    python3 - "$DB_PATH" "$TARGET" <<'PY'
import sqlite3, sys
source, target = sys.argv[1], sys.argv[2]
with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
PY
fi

gzip -f "$TARGET"
echo "$(date -u '+%Y-%m-%d %H:%M:%S') UTC  сохранено: $TARGET.gz ($(du -h "$TARGET.gz" | cut -f1))"

# Чистка старых копий.
find "$DEST" -name 'alert_bot-*.db.gz' -type f -mtime "+$KEEP_DAYS" -delete
REMAINING="$(find "$DEST" -name 'alert_bot-*.db.gz' -type f | wc -l | tr -d ' ')"
echo "копий в хранилище: $REMAINING (храним $KEEP_DAYS дней)"
