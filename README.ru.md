[English](README.md) | [Русский](README.ru.md)

# Filexa2Wan2GP Connector

Filexa2Wan2GP Connector подключает Wan2GP к локальной генерации Filexa, чтобы пользователи Telegram
могли запускать локальные image и short video tasks на своем компьютере.
Коннектор опрашивает Filexa, отправляет tasks через plugin API Wan2GP, сообщает progress и возвращает
поддерживаемые results через Filexa local connector API.

Бот: https://t.me/FilexaAIBot

Не связан с Wan2GP, не одобрен и не спонсируется проектом Wan2GP.

![Demo](docs/img/demo_pingpong.gif)

## Что это

Это plugin для Wan2GP, который позволяет Filexa отправлять локальные image и video tasks на ваш компьютер.
Вы настраиваете Wan2GP как обычно, вставляете API URL и token из Telegram bot во вкладку коннектора,
включаете polling и держите Wan2GP запущенным.

Публичный Wan2GP port не нужен: коннектор делает только исходящие HTTP/HTTPS запросы к Filexa.

## Что внутри

- `plugin.py` - реализация Wan2GP plugin.
- `plugin_info.json` - metadata для Wan2GP plugin manager.
- `API_CONTRACT.md` - bot-side API contract для повторного использования этого коннектора с другим bot/server.
- `README.md` - основное руководство по установке и использованию на английском.
- `README.ru.md` - русское руководство по установке и использованию.
- `LICENSE` - лицензия исходного кода.
- `NOTICE.md` - юридические уведомления и отказы от ответственности.
- `SECURITY.md` - политика сообщения об уязвимостях.

Prebuilt packages не нужны. Wan2GP загружает этот коннектор как Python plugin.

## Как установить для пользователя Wan2GP

Коннектор рассчитан на работу только с https://t.me/FilexaAIBot.

1. Установите Wan2GP из официального проекта:
   https://github.com/deepbeepmeep/Wan2GP
2. Запустите Wan2GP один раз, установите нужную model family и вручную проверьте одну local generation
   до подключения Filexa.
3. Скопируйте эту папку в Wan2GP plugins folder как:
   `Wan2GP\plugins\Filexa2wan2gp`
4. Перезапустите Wan2GP.
5. Настройте вкладку Wan2GP `Video Generator` с теми model/settings, которые должны использовать
   Filexa video jobs.
6. В Filexa bot откройте local generation settings, выберите WanGP и скопируйте API URL и token.
7. Вставьте API URL и token во вкладку коннектора, затем нажмите `Save / reconnect`
   (`Enable connector` включен по умолчанию).
8. Дополнительно: включите `Manual settings snapshots` и обновляйте image/video snapshot кнопкой, когда
   хотите заморозить WanGP settings вместо automatic refresh перед каждой task.

После включения держите Wan2GP запущенным. Коннектор делает только исходящие HTTP/HTTPS запросы к
Filexa. Публичный Wan2GP port не нужен. Bot-side HTTP contract для совместимых servers описан в
`API_CONTRACT.md`.

## Настройки Wan2GP

Коннектор намеренно сделан generic:

- Filexa отправляет `engine`, `client_type`, `profile`, `params`, prompt, references и result/status URLs.
- По умолчанию коннектор обновляет только подходящий WanGP settings snapshot прямо перед task:
  video tasks обновляют video-output snapshot, image tasks обновляют image-output snapshot.
  WanGP `image_mode` и model metadata используются, чтобы не сохранить video settings в image
  snapshot или наоборот.
- Открытие вкладки `Filexa2Wan2GP` также обновляет и сохраняет текущий image/video snapshot в
  правильный slot, когда manual snapshots выключены. Это помогает коннектору помнить последние
  настроенные пользователем image и video models между WanGP restarts.
- Successful task snapshots сохраняются отдельно как fallback state. Они не заменяют current
  image/video snapshot, если current saved snapshot не отсутствует, не invalid или не относится
  к wrong media kind.
- Saved snapshots удаляют task-specific reference file paths, например `image_start` и `image_refs`,
  чтобы последующая text-only task не переиспользовала удаленную temporary reference от более ранней
  edit/I2V task.
- `Manual settings snapshots` - advanced mode. Когда он включен, коннектор не обновляет snapshots
  автоматически; используйте `Update image snapshot` или `Update video snapshot` после ручной
  настройки WanGP.
- Filexa prompt переопределяет template prompt.
- Если Filexa отправляет `params.wangp_task`, этот object передается в Wan2GP как полный task payload
  после применения Filexa prompt и references.
- Если Filexa отправляет `params.reference_bindings`, он может сопоставить input references с Wan2GP
  setting keys. Values могут быть reference index, list of indexes или `"all"`.
- Для image edit и image-to-video tasks без explicit bindings первая reference помещается в
  `image_start`, все references помещаются в `image_refs`, а коннектор включает start/reference
  prompt mode WanGP, когда selected model definition его объявляет.

Так plugin остается достаточно прозрачным для будущих Wan2GP methods, а Filexa может решать, какие
methods показывать в bot UI.

## Как это работает

- Коннектор опрашивает Filexa каждые 10 секунд, пока включен.
- Ему нужны Filexa API URL и bearer token, созданные bot.
- `Disconnect` останавливает polling и локальную активность, но сохраняет token, пока вы
  не замените его или не удалите local connector config вручную.
- Если Filexa недоступна или token отклонен, коннектор сам переключается в disabled и прекращает
  polling. Исправьте URL/token/server, затем включите его вручную и нажмите `Save / reconnect`.
- API URL должен указывать на тот же Filexa origin для всех task URLs, которые возвращает Filexa.
- Он отклоняет tasks для других engines и принимает только `wangp`.
- Он проверяет prompts, local references и task URL shapes до запуска работы Wan2GP.
- Он хранит configuration в `filexa2wan2gp_config.json` внутри local plugin directory.
- Он скачивает I2I references во temporary directory и удаляет их после завершения task.
- Он запускает background generation через in-process/headless `shared.api` session WanGP. Gradio
  `api_session`, который передается в plugin UI callbacks, worker не использует, потому что свежие
  WanGP builds требуют live browser `session_hash` для WebUI-session submissions.
- Status panel всегда показывает loaded connector version, plugin file path, worker backend,
  last error и latest diagnostic events. Отдельного debug toggle нет.
- Вкладка также показывает compact live activity line, обновляемую каждые несколько секунд, и mini
  previews references, полученных для active task, чтобы пользователи видели, что WanGP занят, даже
  когда основная вкладка WanGP молчит.
- Он сообщает progress через Wan2GP callbacks, когда progress data доступна.
- Пока upload/reference chunk fallback cache активен после проблем с poor-network transfer, Status
  panel показывает `⚠️ Unstable network, chunk transfer method temporarily enabled.`
- Successful result reports включают actual WanGP `model_type`, отправленный в `shared.api`, чтобы
  совместимый bot мог показать его в captions.
- Кнопка `Cancel active task` просит и Wan2GP, и Filexa отменить active task, закрывает worker session
  и возвращает connector UI в idle/enabled state, если Wan2GP принимает stop.

## Доставка результатов

Image results возвращаются по той же delivery strategy, что и в Filexa SwarmUI connector:

- один короткий direct image upload с лимитом 40 MiB;
- optional JPEG conversion с 80 percent quality;
- fallback binary chunks по 50 KiB;
- fallback JSON/base64 chunks по 8 KiB;
- safe JSON/base64 chunks по 4 KiB с `Connection: close` и pauses between chunks;
- local-only completion, когда upload disabled, image слишком большой после compression или Wan2GP
  возвращает non-image artifact.

Video results используют только direct-upload:

- accepted video MIME types: MP4, WebM и QuickTime/MOV;
- direct video upload ограничен 50 MiB;
- chunked fallback намеренно не используется для video;
- если direct video upload fails или file слишком большой, generated file остается на компьютере
  пользователя, а Filexa получает local-only completion message.

Если Wan2GP возвращает file, который не является accepted image или accepted video, коннектор
оставляет его на компьютере пользователя и сообщает local-only completion.

## Если что-то не работает

### Коннектор не появляется в Wan2GP.

Проверьте, что files лежат в `Wan2GP\plugins\Filexa2wan2gp\plugin.py`, затем перезапустите Wan2GP.

### Коннектор говорит, что token invalid.

Сгенерируйте local connector token заново в Filexa bot, вставьте его во вкладку коннектора и снова
сохраните. Tokens хранятся локально на вашем компьютере; любой, кто может прочитать plugin config
file, может получить доступ к token.

### Wan2GP запустился, но Filexa ждет бесконечно.

Отмените task в Filexa через `/cancel`, затем нажмите `Cancel active task` во вкладке коннектора.
Если сам Wan2GP завис, перезапустите Wan2GP и снова включите коннектор.

### Коннектор переключился в DISABLED.

Это ожидаемо после rejected token, unavailable Filexa server или unreachable API URL. Сначала
исправьте bot URL/token/server, затем включите коннектор и нажмите `Save / reconnect`; polling не
возобновится автоматически.

### Result upload не проходит на слабой сети.

Оставьте JPEG conversion включенным. Коннектор постепенно перейдет от direct upload к chunked uploads
и запомнит успешный text-chunk mode на несколько часов.

## Юридическое уведомление

Этот репозиторий содержит только исходный код Filexa2Wan2GP Connector.

Коннектор распространяется по MIT License. Filexa bot/API service предоставляется на основании
отдельных Filexa Terms of Use и Privacy Policy:
https://teutonick.github.io/bot-legal-docs/privacy

Этот коннектор не является частью Wan2GP, не связан с проектом Wan2GP и не одобрен им. Wan2GP,
AI models, model weights, checkpoints, drivers и другие runtime components являются third-party
software и могут иметь собственные licenses и restrictions.

Пользователи самостоятельно отвечают за установку Wan2GP, выбор и лицензирование models, защиту
API tokens, эксплуатацию своего компьютера, проверку generated outputs, а также соблюдение
применимых законов и third-party terms.

## Уведомление о безопасности

О проблемах безопасности следует сообщать в приватном порядке согласно `SECURITY.md`.
