# EcoFlow exporter

Минимальный MQTT → Prometheus экспортер для Raspberry Pi 5 с 64-битной Linux.
Основан на [vbash/ecoflow_exporter](https://github.com/vbash/ecoflow_exporter),
исходный коммит `9a32d1359919ddfac270002ce9f6dae8abab9068`. Лицензия исходного проекта GPL-3.0 сохранена в LICENSE.

## Запуск

Сначала разместите содержимое этого каталога в ветке `master` репозитория
`eduardpawlow/ecoflow-exporter`. Если используете `main`, замените `#master`
на `#main` в compose.yaml. Удалённый build context должен быть доступен серверу.

На Raspberry Pi нужны Docker Engine и Docker Compose v2.
Скопируйте compose.yaml и .env.example в каталог сервиса:

```bash
cp .env.example .env
chmod 600 .env
nano .env
docker compose up -d --build
docker compose logs --tail=100 -f ecoflow-exporter
curl -fsS http://127.0.0.1:19094/metrics
```

В `.env` заполните DEVICE_SN, ECOFLOW_USERNAME и ECOFLOW_PASSWORD.
Пароль можно заключить в одинарные кавычки, чтобы `$` не интерполировался.
`.env` исключён из Git и Docker context.

Compose собирает **удалённую ветку**, поэтому локальные правки не попадут в сборку
до отправки в GitHub. Для сборки до публикации временно замените `build.context`
на `.` и запускайте Compose из этого каталога.

Метрики доступны только на самом хосте: `127.0.0.1:19094/metrics`.
Prometheus в отдельном контейнере не сможет обратиться к ним через собственный localhost.
Метрики устройства появляются после получения MQTT-сообщений.

## Состав и изменения

Один Python-файл, Dockerfile, закреплённые зависимости, Compose и пример окружения.
Включён JSON дашборда Smart Home Panel; дополнительные сервисы Grafana/Prometheus, изображения и CI не включены.
Все runtime-зависимости, включая транзитивные, закреплены через `==`.
`paho-mqtt==1.6.1` совместим с исходными MQTT callbacks.
Базовый Python 3.12 Alpine закреплён digest многоархитектурного образа с ARM64.
Сборочные инструменты setuptools, wheel и packaging также закреплены в Dockerfile.

В коде убраны неиспользуемый импорт и закомментированные фрагменты,
добавлен таймаут HTTP 30 секунд, явный client_id и обработка отсутствующего params
и некорректного имени метрики. Схема метрик и логика исходного экспортера сохранены.
`ecoflow_online` означает наличие сообщений за интервал обработки (по умолчанию 10 секунд),
а не независимую проверку доступности устройства.

Опциональные переменные: DEVICE_NAME (по умолчанию DEVICE_SN), LOG_LEVEL (INFO),
COLLECTING_INTERVAL (10 секунд), EXPORTER_PORT (9090).

Проверки подготовки: установка закреплённых зависимостей, создание MQTT-клиента,
обработка тестового payload и выдача метрик HTTP. Реальное подключение к EcoFlow
и сборка контейнера ARM64 требуют проверки на устройстве.

## Дашборд Grafana: EcoFlow Smart Home Panel

Файл [GrafanaEcoflowSmartHomePanel.json](GrafanaEcoflowSmartHomePanel.json) основан
на дашборде исходного репозитория. Он содержит 22 панели для Smart Home Panel,
10 электрических цепей и двух подключённых DELTA Pro:

- заряд резервных батарей, температура, оценка времени работы и зарядки;
- состояние электросети, системы и каждой DELTA Pro;
- суточное потребление от сети и резервного питания;
- суммарная мощность, мощность по цепям и доли потребления;
- входная/выходная мощность DELTA Pro и история отключений сети.

Дашборд ожидает метрики Smart Home Panel (`ecoflow_backup_*`, `ecoflow_grid_*`,
`ecoflow_energy_infos_*`, `ecoflow_info_list_ch_watt_*`, `ecoflow_state_bean_*`).
Для других моделей набор метрик может отличаться: отсутствующие данные дают пустые панели.
Значения и единицы измерения в запросах сохранены из исходного дашборда.
JSON экспортирован из Grafana 9.4.3 (schemaVersion 38); совместимость с установленной
версией Grafana нужно проверить при импорте.

### Подключение

1. Настройте сбор `/metrics` в существующем Prometheus. Если Prometheus работает
   непосредственно на том же хосте или использует host networking, подходит такой job:

   ```yaml
   scrape_configs:
     - job_name: ecoflow
       scrape_interval: 10s
       static_configs:
         - targets: ["127.0.0.1:19094"]
   ```

   Добавьте job в существующий `scrape_configs`. Для Prometheus в обычной Docker-сети
   localhost указывает на его собственный контейнер; потребуется отдельно настроить
   доступ к экспортеру через общую сеть или адрес хоста.
2. Добавьте Prometheus как источник данных в Grafana.
3. Импортируйте `GrafanaEcoflowSmartHomePanel.json` через функцию импорта дашборда
   и выберите этот источник данных для входного параметра `DS_PROMETHEUS`.
4. В поле `Device ID` укажите значение метки `device`: обычно это `DEVICE_SN`,
   а при заданном `DEVICE_NAME` — именно `DEVICE_NAME`. Задайте названия `Circuit 1`–`Circuit 10`.

Исходный серийный номер и названия домашних цепей заменены нейтральными значениями.
Запросы сохранены: большинство из них **не фильтруются по Device ID**.
Этот вариант рассчитан на один Smart Home Panel в источнике данных;
для нескольких устройств нужно добавить фильтр `{device="$deviceId"}` к соответствующим метрикам.
