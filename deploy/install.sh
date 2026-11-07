#!/bin/sh
# Ставит дашборд 7000 в ~/crm-ads-dashboard (или $DIR) отдельным docker compose проектом «crmadsdashboard».
# Чужие контейнеры, папки и настройки не трогает. Если 80/443 заняты — останавливается и ничего не делает.
set -eu
DIR=${DIR:-$HOME/crm-ads-dashboard}
PROJECT=crmadsdashboard
cd "$DIR"

[ -f .env ] || { echo "Нет $DIR/.env — сначала скопировать настройки"; exit 1; }
chmod 600 .env
grep -q '^DOMAIN=.\+' .env || { echo "В .env не задан DOMAIN"; exit 1; }
command -v docker >/dev/null || { echo "Docker не установлен — установку согласовать отдельно"; exit 1; }

# Порты 80/443 нужны Caddy для HTTPS. Если их держит кто-то кроме нашего проекта — стоп.
busy=$(ss -tlnH '( sport = :80 or sport = :443 )' 2>/dev/null || true)
ours=$(docker compose -p "$PROJECT" ps -q caddy 2>/dev/null || true)
if [ -n "$busy" ] && [ -z "$ours" ]; then
  echo "Порты 80/443 уже заняты другим проектом:"; echo "$busy"
  echo "Ничего не меняю. Нужен другой способ (обсудить)."; exit 2
fi

mkdir -p data
docker compose -p "$PROJECT" up -d --build
sleep 5
docker compose -p "$PROJECT" ps
docker compose -p "$PROJECT" exec -T app python -m unittest discover -s tests 2>&1 | tail -3
echo "Проверка: curl -s https://$(grep '^DOMAIN=' .env | cut -d= -f2)/health"
