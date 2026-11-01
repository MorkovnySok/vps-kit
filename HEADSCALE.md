# Личная VPN для телефона и ПК

`vpsctl vpn setup` запускает на VPS Headscale **0.29.7** с собственным ретранслятором DERP и HTTPS через существующий Caddy. На телефон и ПК устанавливаются официальные клиенты Tailscale с адресом твоего сервера. Аккаунт в облачном Tailscale для этой сети не нужен.

Headscale управляет регистрацией устройств. Трафик между телефоном и ПК по возможности идёт напрямую; при невозможности прямого соединения используется DERP на твоём VPS. Публичные DERP-карты Tailscale отключены, доступ к своему DERP разрешён только зарегистрированным устройствам. Единственный VPS становится единственным ретранслятором: его недоступность может нарушить связь.

## Установка на VPS

Нужны Ubuntu/Debian VPS с systemd, публичным IPv4 и установленным vps-kit. Если `vpsctl vpn` ещё нет, обнови инструменты тем же установщиком:

```bash
curl -fsSLo install.sh https://raw.githubusercontent.com/MorkovnySok/vps-kit/master/install.sh
less install.sh
sudo bash install.sh --ip YOUR_PUBLIC_IPV4
```

При повторной установке можно не указывать `--ip` и `--ssh-port`: используются сохранённые значения. Установщик обновляет инструменты, сохраняя существующие приложения и конфигурацию VPN.

1. Создай DNS A-запись, например `vpn.example.com`, на IPv4 VPS. При наличии AAAA-записи её адрес тоже должен вести на этот сервер; иначе убери неверную AAAA-запись.
2. Открой **TCP 80/443 и UDP 3478** в firewall VPS и в панели провайдера. Инструмент не меняет правила firewall. UDP 3478 публикуется Docker; учитывай правила Docker при ограничении доступа. SSH-порт сохрани открытым для администрирования.
3. Если используешь Cloudflare, выбери для VPN-поддомена **DNS only**: Cloudflare Proxy/Tunnel несовместимы с требуемым протоколом Tailscale.
4. Запусти мастер:

```bash
sudo vpsctl vpn setup
```

Мастер спрашивает публичный IPv4, домен, имя владельца устройств и локальный порт Headscale. По умолчанию предлагаются `owner` и `18080`. Без своего домена можно использовать предложенный адрес `vpn.IP-С-ДЕФИСАМИ.sslip.io`, например `vpn.203-0-113-10.sslip.io` (IP здесь только пример). Это внешний DNS-сервис; для собственного DNS используй свой домен.

До подтверждения мастер не создаёт файлы и не запускает контейнеры. Он доступен также из главного меню `sudo vpsctl`.

Для запуска без вопросов:

```bash
sudo vpsctl vpn setup --domain vpn.example.com

# Переопределение IP из install.sh, имени владельца и локального порта:
sudo vpsctl vpn setup --domain vpn.example.com \
  --public-ip YOUR_PUBLIC_IPV4 --user owner --port 18080
```

Настройка проверяет конфликты с приложениями vps-kit, существующими Caddy-сайтами и занятыми портами. Перед запуском выполняется `headscale configtest`; Compose ожидает healthcheck, затем создаётся пользователь и перезагружается Caddy. Проверка `https://vpn.example.com/health` подтверждает доступность HTTPS с самого VPS. Доступ с мобильной сети нужно проверить отдельно.

TCP-порт Headscale доступен только на `127.0.0.1:18080`. Метрики и административный gRPC не публикуются. Caddy обслуживает существующие приложения и VPN на общем TCP 443. Для UDP STUN используется отдельный порт 3478.

## Подключить ПК

Установи [Tailscale](https://tailscale.com/download), затем выполни на ПК:

```powershell
tailscale login --login-server https://vpn.example.com
```

Откроется страница регистрации с **Auth ID**. Подтверди устройство **на VPS**:

```bash
sudo vpsctl vpn register AUTH_ID
```

На Windows включи в меню Tailscale **Preferences → Run unattended**, чтобы VPN работала до входа пользователя в систему. Доступ к выключенному или спящему ПК требует отдельной настройки пробуждения.

## Подключить телефон

Установи официальное приложение **Tailscale**.

- **Android:** настройки → `Accounts` → меню `⋮` → `Use an alternate server`.
- **iPhone:** `Log in…` → меню справа сверху → `Use custom coordination server`.

Укажи `https://vpn.example.com`, затем подтверди телефон на VPS командой `sudo vpsctl vpn register AUTH_ID` с Auth ID со страницы регистрации телефона.

Альтернатива — создать ключ на VPS:

```bash
sudo vpsctl vpn key
```

Это одноразовый ключ на **1 час**; для каждого устройства создавай новый. На Android после выбора своего сервера открой `Accounts` → `⋮` → `Use an auth key`. На ПК можно использовать `tailscale login --login-server https://vpn.example.com --auth-key KEY`, учитывая, что секрет попадёт в историю shell. Для ручного подключения удобнее подтверждение по Auth ID.

Пользователь `owner` — локальная учётная запись Headscale для устройств, а не пароль для входа в веб-панель. Веб-панель управления не устанавливается. Эта конфигурация предназначена для личной сети: зарегистрированные устройства могут обращаться друг к другу; для разделения нескольких пользователей настрой ACL отдельно.

## Проверить связь и подключиться к ПК

На VPS:

```bash
sudo vpsctl vpn status       # Контейнер, устройства, их VPN-IP и проверка HTTPS
sudo vpsctl vpn connect      # Повторить инструкции подключения
sudo vpsctl vpn logs         # Последние 100 строк журнала
```

На ПК:

```powershell
tailscale status
tailscale netcheck
tailscale ping ИМЯ_ИЛИ_VPN_IP_ТЕЛЕФОНА
```

Внутренние имена имеют вид `имя.tailnet.internal`; обычный интернет продолжает использовать локальные DNS-настройки устройства. VPN предоставляет доступ к ПК; для управления экраном включи RDP (если поддерживается редакцией ОС) или другой сервер удалённого рабочего стола и установи соответствующий клиент на телефон. Подключайся к VPN-IP ПК из `vpn status`. Разреши нужный сервис в firewall ПК для VPN-сети. Порты RDP/SSH/домашних приложений на публичном IP VPS открывать не требуется.

Проверь доступ с телефона через мобильный интернет, отключив Wi-Fi. `tailscale ping` показывает прямой путь или ретрансляцию. Проверка health сама по себе не подтверждает NAT traversal, DERP или удалённый рабочий стол.

## Повторный запуск и восстановление

```bash
sudo vpsctl vpn setup
```

При наличии метаданных повторный запуск использует прежние параметры, сохраняет пользовательские изменения конфигурации, базу и ключи. Попытка передать другой домен/IP/порт/владельца отклоняется. Установщик vps-kit не обновляет запущенный Headscale автоматически; обновление версии и миграции выполняются отдельно по документации Headscale с резервной копией.

При ошибке запуска файлы и данные сохраняются для повторной попытки. Созданный в этой попытке Caddy-сайт удаляется, а уже существовавший сайт остаётся. Контейнер и данные автоматически не удаляются. После исправления причины повтори `vpn setup` без параметров.

Если Headscale запустился, но HTTPS пока недоступен, команда возвращает код **1** и сохраняет сервер и Caddy-сайт. Проверь DNS, TCP 80/443, выпуск сертификата (`sudo journalctl -u caddy -n 100 --no-pager`), затем повтори `sudo vpsctl vpn status`. При отсутствии метаданных или повреждении файлов восстанови их из резервной копии: инструмент не пытается перезаписать неизвестную установку.

| Путь | Содержимое |
| --- | --- |
| `/opt/vps-kit/headscale/settings.json` | Параметры установки |
| `/opt/vps-kit/headscale/compose.yaml` | Compose-проект `vps-kit-headscale` |
| `/opt/vps-kit/headscale/config/config.yaml` | Конфигурация Headscale (JSON, совместимый с YAML) |
| `/opt/vps-kit/headscale/data/` | SQLite, идентификационные ключи сервера и DERP |
| `/etc/caddy/vps-kit/vps-kit-headscale.caddy` | HTTPS-маршрут VPN |

Для согласованной резервной копии останови только Headscale, скопируй весь каталог `/opt/vps-kit/headscale` и Caddy-сайт, затем запусти сервис снова:

```bash
sudo docker compose -p vps-kit-headscale -f /opt/vps-kit/headscale/compose.yaml stop
# Здесь выполни резервное копирование указанных каталогов своим инструментом.
sudo docker compose -p vps-kit-headscale -f /opt/vps-kit/headscale/compose.yaml up -d --wait
```

Справка: `vpsctl help vpn setup`. Исходники: `src/vpskit_vpn.py`; модуль включается в единый `install.sh` через `build.sh`.

Основа конфигурации: [Headscale v0.29.7](https://github.com/juanfont/headscale/blob/v0.29.7/config-example.yaml), [Docker](https://headscale.net/stable/setup/install/container/), [DERP](https://headscale.net/stable/ref/derp/), [Caddy reverse proxy](https://headscale.net/stable/ref/integration/reverse-proxy/), [регистрация устройств](https://headscale.net/stable/usage/getting-started/).
