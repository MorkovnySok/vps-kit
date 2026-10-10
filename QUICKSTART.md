# Быстрый старт (новый VPS)

```bash
# На сервере: с сохранённым локальным install.sh
sudo bash install.sh --ip YOUR_PUBLIC_IPV4
sudo vpsctl project add myapp --repo OWNER/REPO --port 8081 --compose compose.yaml
sudo nano /opt/apps/myapp/shared/.env
sudo gh auth login --web
sudo vpsctl github configure myapp
sudo vpsctl registry login YOUR_GITHUB_LOGIN
vpsctl workflow myapp > deploy.yml
```

Скопируй `deploy.yml` в **проект приложения** как `.github/workflows/deploy.yml`, сделай commit/push в указанную ветку. Для Compose с обязательными переменными добавь безопасный `.env.ci` с не секретными заглушками. Порт веб-приложения в Compose: `127.0.0.1:8081:ВНУТРЕННИЙ_ПОРТ`.

Внешние порты: TCP 80/443 + SSH. После установки проверь `sudo vpsctl doctor` и HTTPS-адрес из вывода `project add`.

**Если используешь этот kit на уже настроенном VPS**, см. раздел «Уже работающий VPS и старые runners» в README. Никакой автоматической миграции не производится.
