# Быстрый старт

Полные требования, описание команд и устранение ошибок — в [README](README.md). Если на VPS уже работают приложения, сначала прочитай [раздел о переносе](README.md#уже-работающий-vps).

## Подключение телефона к ПК через свою VPN

После установки или обновления `install.sh` выполни на VPS:

```bash
sudo vpsctl vpn setup
```

Мастер настроит Headscale с собственным DERP и HTTPS через существующий Caddy. Нужны DNS-адрес на VPS и открытые TCP 80/443, UDP 3478. На телефон и ПК устанавливаются клиенты Tailscale с адресом твоего сервера. Подтверждение устройств: `sudo vpsctl vpn register AUTH_ID`; проверка: `sudo vpsctl vpn status`. Полные шаги — в [HEADSCALE.md](HEADSCALE.md).

## На VPS

Скачай и просмотри `install.sh`, затем выполни:

```bash
sudo bash install.sh --ip YOUR_PUBLIC_IPV4
sudo vpsctl
```

Если SSH уже настроен на другой порт, добавь `--ssh-port 2222` при установке. Скрипт не меняет sshd/firewall. Открой TCP 80, 443 и фактический SSH-порт.

В меню выбери **«Добавить проект»** или запусти `sudo vpsctl project add` напрямую. Мастер спросит репозиторий, имя, Compose-файл, ветку, порт, домен и вариант настройки Caddy. Выбирай стрелками/номером и Enter. Перед созданием можно изменить ответы или отменить ввод.

После создания проекта `myapp`:

```bash
sudo nano /opt/apps/myapp/shared/.env
sudo gh auth login --web
sudo vpsctl github configure myapp
sudo vpsctl registry login YOUR_GITHUB_LOGIN   # для приватных GHCR-образов
vpsctl workflow myapp --output "$HOME/deploy-myapp.yml"
```

Для `registry login` введи GitHub PAT (classic) с `read:packages` на запрос Docker Password. Production-переменные остаются в `.env` на сервере.

## В репозитории приложения на своём компьютере

1. В Compose публикуй веб-порт как `127.0.0.1:8081:ВНУТРЕННИЙ_ПОРТ`, где `8081` — порт из мастера. Для БД используй именованные volumes.
2. Для обязательных переменных сборки добавь `.env.ci` в корень репозитория с безопасными заглушками.
3. Скопируй `deploy-myapp.yml` с VPS в `.github/workflows/deploy.yml`. Например: `scp USER@VPS_IP:~/deploy-myapp.yml .github/workflows/deploy.yml` (сначала создай каталог; для другого SSH-порта используй `scp -P 2222`).
4. Сделай commit/push в выбранную ветку и посмотри результат во вкладке GitHub Actions.

## Проверка на VPS

```bash
sudo vpsctl doctor
sudo vpsctl status
vpsctl help project add
```

HTTP 401 от GHCR в `doctor` ожидаем. HTTPS-адрес из мастера заработает после успешного деплоя, доступного DNS и выпуска сертификата.
