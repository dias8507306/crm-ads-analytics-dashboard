#!/bin/sh
# Только смотрит, ничего не меняет: что уже стоит на сервере и какие порты заняты.
# На сервере уже есть другой проект — его не трогаем.
echo "== ОС";        head -3 /etc/os-release; uname -m
echo "== ресурсы";   nproc; free -h | head -2; df -h / | tail -1
echo "== docker";    docker --version 2>&1; docker compose version 2>&1
echo "== контейнеры"; docker ps --format '{{.Names}}  {{.Image}}  {{.Ports}}' 2>&1
echo "== кто слушает порты"; ss -tlnp 2>/dev/null | awk 'NR==1 || /LISTEN/' | head -40
echo "== веб-серверы"; for s in nginx apache2 caddy; do printf '%s: ' "$s"; systemctl is-active "$s" 2>&1; done
echo "== firewall";  ufw status 2>&1 | head -20
echo "== /opt";      ls -la /opt 2>&1
echo "== наша папка"; ls -la ~/crm-ads-dashboard 2>&1 | head -5
