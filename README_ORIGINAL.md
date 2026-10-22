# NeedZeroXtraTrash

NeedZeroXtraTrash — компактное Windows-приложение для мониторинга температур и
управления тремя группами корпусных вентиляторов через **NZXT RGB & Fan
Controller / Smart Device V2**. Интерфейс сохраняет ретро-стиль Windows 95/98,
а управление охлаждением выполняется отдельным headless-контроллером и не
зависит от окна или системного трея.

Текущая версия: **v4.6**.

Навигация по исходникам, процессам и маршрутам данных:
[PROJECT_MAP.md](PROJECT_MAP.md).

> Сборка `v4.6` подписана локальным сертификатом разработчика. Это локальная
> подпись, а не публично доверенная коммерческая подпись. Неподписанные DEV-сборки
> не устанавливают привилегированный автозапуск. Для постоянного управления
> охлаждением необходим подписанный RELEASE-артефакт, проверка в чистой Windows
> VM и разрешённая пользователем аппаратная приёмка.

## Возможности

- температуры CPU, GPU, GPU Hotspot и состояние обязательных датчиков;
- стабильные группы **Front Intake**, **Top Exhaust** и **Rear Exhaust**;
- отдельная кривая каждой группы и источники CPU, GPU или MAX(CPU, GPU);
- профили Silent, Balanced и Performance;
- десятисекундный тест всех вентиляторов на 100%;
- аварийный режим по GPU Hotspot;
- проверка применённой мощности по duty/RPM и явные fan/sensor fault;
- повторное обнаружение датчиков и NZXT-контроллера;
- отдельные процессы controller, tray и UI;
- журнал событий, визуализация корпуса, температур, RPM и потоков воздуха.

## Поддерживаемая система

- Windows 10/11 x64;
- NZXT RGB & Fan Controller или совместимый Smart Device V2;
- Microsoft Edge WebView2 Runtime для окна приложения;
- аппаратная защита от перегрева в BIOS/UEFI должна оставаться включённой.

Контроллер использует USB HID через liquidctl. Не устанавливайте случайные
драйверы Zadig/WinUSB для поддерживаемого устройства без отдельной причины.

## Архитектура и жизненный цикл

Программа разделена на три независимых процесса:

1. **Controller** — единственный владелец USB-записей. В подписанной установке
   запускается задачей BootTrigger под LocalSystem до входа пользователя.
2. **Tray** — лёгкий клиент текущего пользователя, запускаемый после logon.
3. **UI** — отдельное завершаемое окно WebView2. Закрытие окна не останавливает
   controller и не оставляет управление охлаждением на ответственности UI.

Controller и клиенты общаются только через loopback IPC с HMAC-аутентификацией,
ограничением размера сообщений и узким списком методов. Пути к DLL/EXE через
IPC не принимаются.

## Первый запуск

### Подписанный RELEASE

При первом открытии подписанного RELEASE приложение проверяет наличие
защищённого controller. Если его нет, до загрузки WebView появляется запрос на
установку. После согласия Windows показывает UAC, копирует проверенный EXE в
Program Files и создаёт две задачи:

- machine controller: BootTrigger, LocalSystem, watchdog/restart;
- tray текущего SID: LogonTrigger, LeastPrivilege.

Повторная установка той же версии идемпотентна. При обновлении старый controller
сначала выполняет подтверждённую безопасную остановку, и только затем
заменяются бинарный файл, token и задачи.

### Неподписанный DEV

DEV-сборка не устанавливает повышенный системный автозапуск. Параметр **Start
with Windows** создаёт запись только для текущего пользователя. После входа в
Windows приложение запрашивает UAC, запускает controller текущей сессии и остаётся
в трее. Это проверочный режим; не используйте его как замену подписанной
постоянной установке.

## Интерфейс

### OVERVIEW

Главный экран показывает CPU, GPU, GPU Hotspot, компактную кривую, текущий
профиль, журнал и корпус ПК. На корпусе отображаются CPU/GPU и RPM групп.

### CURVES

- левая кнопка и перетаскивание изменяют точку;
- правая кнопка по свободному полю добавляет точку;
- правая кнопка по точке удаляет её;
- удалить последние две точки нельзя;
- мощность автоматически остаётся неубывающей;
- кнопка **100%** включает все вентиляторы на 10 секунд и возвращает прежнее
  управление.

### SETTINGS

- сопоставление CPU, GPU и GPU Hotspot с датчиками;
- назначение `fan1`, `fan2`, `fan3` группам Front/Top/Rear;
- минимальный duty и интервал опроса;
- аварийные пороги и duty;
- установка или удаление безопасного автозапуска для RELEASE.

Каждая физическая группа должна использовать отдельный канал. Серверная
валидация исправляет повреждённые или дублирующиеся назначения до публикации.

## Поведение безопасности

- потеря или устаревание обязательного CPU/GPU повышает duty минимум до 50%;
- при `MAX` обязательны оба датчика, а не только доступный максимум;
- emergency защёлкивается и не снимается из-за `None`, stale или exception;
- сброс emergency требует три свежих значения минимум на 5 °C ниже порога;
- `duty_commanded` и `duty_reported` не смешиваются;
- повторяющийся duty mismatch, ошибка USB, неизвестная телеметрия или нулевой
  RPM после spin-up становятся явным fan fault;
- при fan fault остальные доступные каналы поднимаются до emergency duty;
- USB I/O выполняется в отдельном process с жёстким deadline: просроченный
  writer завершается до создания нового;
- штатная остановка устанавливает `max(50%, last confirmed duty)`; при
  emergency, sensor/fan fault или неизвестном состоянии используется 100%;
- неподтверждённая финальная запись считается ошибкой, а не успехом.

После первого разрешённого аппаратного теста обязательно проверьте соответствие
`fan1..fan3`, фактические RPM на 30/50/100% и USB reconnect. До такой проверки
программа не должна считаться подтверждённой для конкретного компьютера.

## Файлы и диагностика

Защищённые machine-файлы подписанной установки:

```text
%ProgramData%\NeedZeroXtraTrash\config.json
%ProgramData%\NeedZeroXtraTrash\ipc.token
%ProgramData%\NeedZeroXtraTrash\install.json
%ProgramData%\NeedZeroXtraTrash\controller.log
```

Controller log ограничен 2 MiB и тремя резервными файлами. Повреждённый JSON не
перезаписывается молча: исходник переносится в `config.json.broken-<timestamp>`,
а warning передаётся в UI. Если резервную копию создать нельзя, controller
завершается с ошибкой и сохраняет исходный файл.

При удалении автозапуска UI сначала экспортирует проверенную конфигурацию в:

```text
%APPDATA%\NeedZeroXtraTrash\config.json
```

Если export/hash-проверка не прошла, machine config сохраняется, а удаление
возвращает частичную ошибку вместо потери настроек.

## Устранение неполадок

### CONTROLLER ERROR / NO CONTROLLER

- подтвердите запрос UAC при первом запуске DEV;
- закройте NZXT CAM и другие приложения, удерживающие USB;
- проверьте USB-кабель и питание controller;
- проверьте `%ProgramData%\NeedZeroXtraTrash\controller.log`;
- при нескольких поддерживаемых NZXT-устройствах требуется стабильный
  serial/path/port: программа не выбирает первый элемент списка молча.

### Температура `—`

Источник ещё недоступен, устарел либо ручной identifier исчез. Supervisor
повторно подключает LHM/NVML с bounded backoff. HWiNFO больше не используется;
старые настройки этого источника переводятся на auto. Memory Junction/VRAM не
используется как GPU Hotspot. Пока обязательный датчик отсутствует, действует
безопасный floor.

### Автозапуск не устанавливается

- он доступен только frozen RELEASE с валидной Authenticode-подписью;
- DEV и запуск из исходников намеренно отклоняются;
- установщик не доверяет cwd, `%TEMP%`, `USERNAME`, `USERDOMAIN` или DLL paths
  из окружения;
- задача считается установленной только после полной проверки XML и
  аутентифицированной готовности controller.

## Разработка и тесты

Сборочный runtime закреплён на Windows x64 CPython 3.12; точный patch указан в
`build_release.py`. Зависимости фиксируются с SHA-256 в
`requirements-bootstrap.lock` и `requirements.lock`, LHM — в
`deps/librehardwaremonitor.lock.json`.

Базовые проверки без оборудования:

```powershell
$env:PYTHONPATH = 'src'
python tests\test_core.py
python tests\test_build_release.py
python -m pytest tests -q
node tests\ui_behavior_smoke.js
python fetch_deps.py --verify-only
python tools\idle_benchmark.py --duration 60 --output qa\headless-idle-v4.6.json
```

Headless benchmark использует только deterministic mock backends и никогда не
отправляет команды физическому контроллеру.

## Воспроизводимая сборка

Укажите абсолютный путь к закреплённому CPython и выберите режим явно. Если в
`build\lock-bootstrap` и `build\lock-wheels` есть полный проверенный набор,
сборка работает с `--no-index`; `--require-hashes` применяется в любом случае:

```bat
set NEEDZERO_PYTHON=C:\Path\To\Python312\python.exe
build.bat DEV
```

DEV создаёт файл с явной маркировкой:

```text
dist\NeedZeroXtraTrash v4.6.exe
```

Для RELEASE необходим реальный сертификат подписи кода:

```bat
set NEEDZERO_PYTHON=C:\Path\To\Python312\python.exe
set NEEDZERO_SIGN_SHA1=40_HEX_CERTIFICATE_THUMBPRINT
build.bat RELEASE
```

Для доверенной только на рабочем компьютере локальной подписи без Windows SDK
дополнительно задайте `NEEDZERO_LOCAL_SIGNING=1`. Этот режим использует Windows
PowerShell и не ставит timestamp, поэтому не предназначен для публичного релиза.

RELEASE без сертификата завершается до публикации артефакта. Сборщик проверяет
hash-locks, allowlist LHM, FileVersion/ProductVersion, фактический payload
PyInstaller и Authenticode. EXE и manifest публикуются одной транзакцией; номер
версии изменяется только явным редактированием `version.txt`.

## Структура проекта

```text
src/needzero/
├── controller_service.py   headless service lifecycle и IPC
├── controlloop.py          fail-safe state machine
├── config.py               schema, migration, durable JSON
├── autostart.py            защищённая установка/обновление/удаление
├── windows_security.py     Windows identity, paths, owner/DACL, signature
├── fans/                   liquidctl и deadline process owner
├── sensors/                supervisor и deadline read cache
├── app.py                  отдельные tray/UI clients
└── webui/                  HTML/CSS/JS и используемые assets
```

Основные компоненты: liquidctl, LibreHardwareMonitor, pywebview, pythonnet,
pystray и Pillow.

Шрифт Nunito распространяется по SIL OFL 1.1. Включённый Calculator разрешён
автором для личного использования; для коммерческого распространения требуется
отдельная лицензия. Тексты лицензий находятся рядом со шрифтами в
`src/needzero/webui/assets/fonts/` и включаются в сборочный payload.

Лицензия: [GPL-3.0-or-later](LICENSE).
