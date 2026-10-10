# vps-kit — GitHub-hosted Docker Compose deployment for a small VPS

Однокомандная подготовка **чистого Ubuntu 22.04/24.04 или Debian 12/13 VPS**, затем управление несколькими Docker Compose-проектами. **На VPS нет GitHub Actions runners и сборок**: GitHub-hosted Actions собирает сервисы с `build:`, отправляет образы в GHCR, затем по SSH отправляет конфигурацию на VPS. Сервер скачивает готовые образы и выполняет `docker compose up`.

**Публичный GitHub:** в репозитории vps-kit нет токенов, SSH-ключей и адреса конкретного VPS. `install.sh` — самодостаточный установщик; его исходные части в `src/`. `build.sh` собирает `install.sh` из исходников.

## 1. Новый VPS: одна команда

Сначала ознакомься с содержимым `install.sh`. После публикации репозитория можно скачать именно файл `install.sh` и выполнить:

```bash
curl -fsSLO https://raw.githubusercontent.com/OWNER/vps-kit/main/install.sh
sudo bash install.sh --ip PUBLIC_IPV4
```

При необходимости укажи `--ssh-port 2222` **только если SSH-сервер уже слушает этот порт**. Скрипт **не меняет sshd_config и firewall**. Если IP не указан, установщик пытается узнать его автоматически; если не получается, повтори с `--ip`.

Устанавливает Docker Engine, Compose Plugin, Caddy, GitHub CLI (`gh`), `jq`, `git`, SSH и Python; создаёт `deploy` (с доступом к Docker), `/opt/apps`, `/etc/vps-kit`, безопасный SSH-приёмник и команду `vpsctl`.

**Что установщик НЕ делает:** не меняет SSH-порт, не отключает парольный вход, не переписывает существующий Caddyfile, не удаляет контейнеры/volumes/runner, не открывает firewall сам, не авторизуется в GitHub автоматически. Повторный запуск ориентирован на сохранение существующих настроек.

Открой TCP 80, 443 для Caddy и SSH-порт в firewall/панели провайдера. Не открывай Docker API. HTTPS заработает после доступности DNS `sslip.io` и выпускa сертификата.

## 2. Добавить проект

Пример для **нового** VPS (указанный в скрипте публичный IP будет преобразован в бесплатный адрес `slug.IP-ЧЕРЕЗ-ДЕФИС.sslip.io`):

```bash
sudo vpsctl project add viki --repo OWNER/Viki --port 8080 --compose compose.yaml --branch main
sudo nano /opt/apps/viki/shared/.env   # production-переменные: только на сервере
```

Система создаёт `/opt/apps/viki/shared/.env` с режимом `600`, отдельный SSH-ключ и сайт Caddy `viki.<IP>.sslip.io`. `vpsctl` не публикует исходники на GitHub и не изменяет репозитории самостоятельно.

Для настоящего домена: `--domain app.example.com` (настрой DNS A-запись). Если маршрут Caddy уже существует, можно использовать `--no-caddy` и продолжить с существующей конфигурацией вручную.

**Очень важно:** в приложении `compose.yaml` публикуй веб-порт **только на loopback VPS**:

```yaml
services:
  web:
    build: ./web
    ports:
      - "127.0.0.1:8080:80"
```

Публичные Docker-порты (`"8080:80"`, `"5432:5432"`, `"0.0.0.0:..."`) **отклоняются** при деплое. Не публикуй порт PostgreSQL. Сам Caddy слушает публичные 80/443.

Для базы и других постоянных данных используй **именованные Docker volumes**, не относительные bind mounts (`./db:/...`) внутри релизов. Используется стабильное имя Compose-проекта, поэтому именованные volumes переживают обновления. Команды `docker compose down -v` не выполняются.

## 3. Настроить GitHub Secrets

Авторизация GitHub CLI требует твоего участия:

```bash
sudo gh auth login --web
sudo vpsctl github configure viki
```

В репозитории `OWNER/Viki` будут созданы **Actions repository secrets**:

- `DEPLOY_HOST` — публичный IPv4 VPS;
- `DEPLOY_PORT` — ранее заданный SSH-порт;
- `DEPLOY_SSH_KEY` — отдельный приватный ключ проекта, который затем удаляется из временной директории VPS;
- `DEPLOY_KNOWN_HOSTS` — проверенный публичный SSH host key **именно этого VPS**, без небезопасного `StrictHostKeyChecking=no`.

`gh` требует права на управление секретами указанного репозитория. После настройки можешь выполнить `sudo gh auth logout`, чтобы не хранить сессию GitHub CLI на VPS. **Никогда не коммить приватные ключи, `.env` или токены.** Если `github configure` прервался, ключ останется доступен **только root** в `/root/.local/share/vps-kit/pending-keys/`; повтори команду после устранения ошибки.

### GHCR для приватных образов

На VPS **один раз** выполни:

```bash
sudo vpsctl registry login YOUR_GITHUB_LOGIN
```

На запрос пароля введи отдельный **GitHub PAT (classic)** с минимальным `read:packages` и доступом к пакетам этого проекта. Права доступа к GHCR-пакетам и SSO организаций нужно настроить отдельно при необходимости. Токен сохраняется у системного пользователя `deploy` в конфигурации Docker **без встроенного менеджера секретов**; защити VPS и периодически меняй токен. Если образ публичный, анонимное скачивание может быть доступно без этого шага, но закрытые образы потребуют авторизации.

Проверь `sudo vpsctl doctor`. Если с российского VPS `ghcr.io` недоступен, **этот GHCR-вариант работать не будет**; понадобится альтернативная доставка Docker-образов через SSH или доступный реестр. В версии 1 альтернативный транспорт не реализован.

## 4. Включить workflow в приложении

```bash
vpsctl workflow viki > deploy.yml
```

Помести сгенерированный файл в **репозиторий приложения**, путь `.github/workflows/deploy.yml`, сделай commit/push в `main`. Он запускается на **`ubuntu-latest` GitHub**, не на твоём VPS.

Workflow:

1. Checkout исходников и вход в GHCR через встроенный `GITHUB_TOKEN` (`packages:write`).
2. Находит сервисы с `build:` через `docker compose config`, создаёт временный override `.vpskit.images.yaml` с тегами `ghcr.io/owner/repo-service:sha-<commit>`.
3. Собирает и отправляет только эти образы в GHCR. Сервисы с готовыми `image:` (например, PostgreSQL) не пересобираются.
4. Передаёт checkout + override в виде `tar.gz` через SSH с **ограниченным ключом** (без общего интерактивного shell).
5. VPS безопасно распаковывает релиз (исключает `.env` и symlink-и), читает **свой** `.env`, скачивает образы, проверяет порты и запускает `docker compose up -d --no-build --wait`.
6. Если healthcheck не проходит, пытается вернуть предыдущий релиз. Релизы сохраняются в `/opt/apps/<slug>/releases`, активный отмечен `current`.

**Важно про Compose:**

- Базовый файл может называться `compose.yaml` или `docker-compose.yml`, укажи через `--compose`.
- Если Compose требует `${DB_PASSWORD:?}`, добавь в репозиторий `.env.ci` только с **не секретными заглушками** для CI (пример `examples/.env.ci`). Production `.env` остаётся на сервере.
- Если в `build:` нужны приватные секреты, эту версию workflow необходимо адаптировать к GitHub Actions secrets / Docker BuildKit secrets. **Не передавай production-пароли через build args.**
- Для монорепозиториев / нескольких Compose-файлов, специальных profiles и нестандартных build contexts может потребоваться адаптация workflow.
- Образам GHCR может потребоваться связь с репозиторием и настроенный доступ для GitHub Actions в настройках Packages.
- Развертывание запускается после push в выбранную ветку или вручную (workflow_dispatch); для критичных проектов включи GitHub Environment approvals/branch protection.
- Для контейнеров без healthcheck `--wait` проверит состояние `running`, но **не готовность самого HTTP API**: добавляй Compose healthcheck.
- Первый запуск не отменяет необходимость заполнить `.env` и настроить Caddy/порты.

## 5. Следующие проекты

```bash
sudo vpsctl project add consigliere --repo OWNER/Consigliere --port 8081 --compose compose.yaml
sudo vpsctl github configure consigliere
vpsctl workflow consigliere > deploy.yml

sudo vpsctl project add words --repo OWNER/learn-words --port 8082 --compose compose.yaml
```

Каждому проекту соответствует отдельный набор Secrets, SSH-ключ, Caddy host, каталог `.env` и префикс Docker Compose. Проверка:

```bash
vpsctl status
sudo vpsctl doctor
sudo docker compose ls
```

## Уже работающий VPS и старые runners

Installer можно запустить на существующем VPS, но **он не мигрирует старые runners и Compose-проекты автоматически**. Убедись, что новый `slug` не конфликтует с существующим именем проекта и что домен уже не объявлен в Caddyfile. Если Viki уже работает, сперва подготовь новый workflow и секреты, затем отдельно выключи старый self-hosted runner и только потом запускай новый деплой. При переносе используй те же именованные Docker volumes и предварительно сделай резервную копию БД. Не запускай два workflow одновременно для одного приложения.

## Риски и гарантии

- Пользователь `deploy` состоит в группе Docker: **это практически root-доступ**. Ограниченный SSH-ключ предотвращает обычные команды через SSH, но **не делает недоверенный репозиторий безопасным**: злонамеренный Compose-код способен управлять VPS через Docker. Храни deploy secrets только в доверенных репозиториях, защищай ветки и включай approvals по необходимости.
- При деплое не удаляются Docker volumes и production `.env`, но **автоматизированного резервного копирования данных нет**. Настрой отдельные бэкапы.
- Старые релизы не удаляются автоматически; периодически контролируй диск.
- В текущей реализации сайт Caddy добавляется при `project add`. Пока приложение не поднято, запрос может отвечать 502; это нормально.
- Пакет тестировался статическими и офлайн-тестами; **на реальном VPS/выпущенном TLS-сертификате ещё не проверен**.

## Разработка

```bash
bash build.sh
python3 tests/test_offline.py
```

`build.sh` собирает **самодостаточный** `install.sh` из прозрачных исходных файлов `src/` без сетевых скачиваний и скрытых payload. Для публичного GitHub добавь обычную лицензию и тег версии. Не используй `curl | sudo bash` без проверки содержимого загружаемого скрипта.
