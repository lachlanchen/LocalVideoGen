[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Tiếng Việt](README.vi.md) · [中文 (简体)](README.zh-Hans.md) · [中文（繁體）](README.zh-Hant.md) · [Deutsch](README.de.md) · [Русский](README.ru.md)

[![LazyingArt banner](https://github.com/lachlanchen/lachlanchen/raw/main/figs/banner.png)](https://github.com/lachlanchen/lachlanchen/blob/main/figs/banner.png)

# LocalVideoGen

*Локальная генерация видео MiniMax H3 максимального качества для рабочей станции с двумя RTX 4090 — нативные изображение, звук и референсы при аккуратном владении ресурсами.*

[![Website](https://img.shields.io/badge/Website-lazying.art-0EA5E9?style=flat-square)](https://lazying.art)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](../LICENSE)
[![ComfyUI pinned](https://img.shields.io/badge/ComfyUI-pinned-5C35D5?style=flat-square)](../config/runtime-versions.txt)
[![Workflows](https://img.shields.io/badge/H3_workflows-17-16A34A?style=flat-square)](../workflows/)
[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-lachlanchen-EA4AAA?style=flat-square&logo=githubsponsors)](https://github.com/sponsors/lachlanchen)

LocalVideoGen — воспроизводимый операционный слой поверх внешней закреплённой установки ComfyUI и официального выровненного пакета MiniMax H3. Он предоставляет доступное только через loopback веб-приложение H3 Studio, загрузку моделей с обязательной проверкой контрольных сумм, процессы T2V/I2V/R2V, совместную нативную генерацию видео и звука, постоянную историю заданий и консервативное управление жизненным циклом для двух RTX 4090 по 24 GiB и 128 GiB оперативной памяти.

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=kofi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

## Интерфейс H3 Studio

Светлая тема ясно объединяет настройку референсов, параметры качества и состояние рендера в одном локальном рабочем пространстве.

![Светлая тема H3 Studio с локальными настройками референсов и рендера MiniMax H3](../docs/images/h3-studio-light.png)

## Возможности

- Профиль максимального качества: сокращённый BF16 Ref2VA/FL2VA DiT, выровненный NVFP4-AWQ Qwen3-VL conditioner, FP16 video VAE, FP32 audio VAE и 25 полных шагов модели.
- Поэтапное размещение на двух GPU: GPU 0 выполняет DiT и denoising; GPU 1 — Qwen conditioning и video/audio VAE. Наличие PCIe peer-to-peer не предполагается.
- Локальные T2V, I2V и многоопорный R2V, включая профиль максимального сохранения идентичности и нативный синхронный звук.
- Профили качества, резервный режим одной GPU и низкоразрешённый предпросмотр INT8 Turbo.
- Локальный H3 выдаёт до 768 пикселей по короткой стороне при 24 fps. Отдельный этап регенерации MiniMax в 2K доступен только через API и не заявляется локальной функцией.

## Архитектура

```mermaid
flowchart LR
    B[Браузер] -->|loopback :8190| S[H3 Studio]
    S --> V[Проверка загрузок и графа]
    V --> J[(Закрытый реестр заданий)]
    V -->|loopback :8188| C[Закреплённый ComfyUI]
    C --> G0[GPU 0: DiT + denoising]
    C --> G1[GPU 1: Qwen + video/audio VAE]
    C <--> R[RAM хоста: DynamicVRAM + асинхронный offload]
    M[Пакет моделей с проверкой SHA-256] --> C
```

Веб-приложение никогда не запускает второй процесс ComfyUI. Сервисы привязаны только к `127.0.0.1`; загружаемые файлы нормализуются в ограниченные медиа, браузер видит непрозрачные идентификаторы, а выходные пути разрешены явным списком.

## Текущее содержимое

| Путь | Назначение |
| --- | --- |
| [`webapp/`](../webapp/) | Адаптивный H3 Studio, локальный API, нормализация загрузок, реестр заданий и тесты |
| [`workflows/dual_gpu/`](../workflows/dual_gpu/) | Поэтапные процессы BF16/INT8 T2V, I2V и R2V |
| [`workflows/quality/`](../workflows/quality/) | 25-шаговые процессы качества для одной GPU |
| [`workflows/preview/`](../workflows/preview/) | Малые процессы предпросмотра INT8 Turbo |
| [`scripts/`](../scripts/) | Загрузка, проверка, жизненный цикл, ресурсы, smoke-тест и инструменты процессов |
| [`config/model-manifest.sha256`](../config/model-manifest.sha256) | Точный список разрешённых девяти файлов и суммы SHA-256 |
| [`config/runtime-versions.txt`](../config/runtime-versions.txt) | Закреплённые версии и коммиты внешней среды |

Крупные `ComfyUI/`, `workflow_templates/`, веса, результаты, загрузки, базы данных и квитанции среды являются локальными установками либо закрытым/сгенерированным состоянием и намеренно не коммитятся.

## Быстрый запуск

Этот репозиторий — публичный слой оркестрации, а не универсальный установщик. Перед командами установите внешние рабочие копии `ComfyUI/` и `workflow_templates/` на точных коммитах из [`config/runtime-versions.txt`](../config/runtime-versions.txt), создайте `.venv` с указанным стеком Python/PyTorch/CUDA и установите зависимости upstream ComfyUI. Исходные деревья намеренно не включены.

Полная очередь официальных выровненных моделей занимает **147 804 799 439 байт (137,65 GiB)**. Загрузки возобновляемы; сверх веса моделей нужно не менее 32 GiB свободного места.

```bash
git clone https://github.com/lachlanchen/LocalVideoGen.git
cd LocalVideoGen

# После установки закреплённой внешней среды:
./scripts/download_models.sh
./scripts/verify_models.sh
./scripts/status.sh

./scripts/start_comfyui.sh
./scripts/start_webapp.sh
```

Откройте <http://127.0.0.1:8190>. ComfyUI остаётся закрытым на <http://127.0.0.1:8188>.

Когда рендер не выполняется, останавливайте только проверенные процессы этого проекта:

```bash
./scripts/stop_webapp.sh
./scripts/stop_comfyui.sh
```

## Безопасность и управление ресурсами

- Для запуска требуется не менее 48 GiB доступной RAM, 20 000 MiB свободной VRAM на каждой запрошенной GPU и не более 75 % занятого swap.
- Частичные загрузки и изменение отпечатка размер/mtime блокируют запуск; все девять файлов проходят структурную проверку и SHA-256.
- Перед действиями проверяются PID, идентичность загрузки, командная строка, владелец listener, маркер сервиса и очередь.
- Очистка GPU по умолчанию работает как dry-run. Явная очистка использует точные pidfd и защищает деревья `LocalLLM` и `AgenticApp`; чужие процессы автоматически не завершаются.
- Swap — аварийный резерв, не замена порогу RAM. Проект предполагает один движок рендера и один лёгкий Studio.

## Проверка

Статические процессы и тесты веб-приложения запускаются без постановки рендера:

```bash
./scripts/prepare_workflows.py
./scripts/validate_workflows.py
.venv/bin/python -m pytest -q webapp/tests
./scripts/verify_models.sh
```

С проверенными моделями и свободной GPU 0 запустите малый нативный аудиовизуальный smoke-граф:

```bash
H3_CUDA_DEVICES=0 ./scripts/start_comfyui.sh
./scripts/smoke_test.sh
H3_SMOKE_MODEL=minimax_h3_fl2va_pruned_bf16.safetensors ./scripts/smoke_test.sh
./scripts/stop_comfyui.sh
```

Пятикадровый результат проверяет Qwen conditioning, совместный video/audio sampling, оба VAE, MP4 mux и декодирование потоков; это не тест визуального качества.

## Лицензия и территория модели

Оригинальные код и документация LocalVideoGen выпущены по [MIT License](../LICENSE). Она **не перелицензирует** ComfyUI, workflow templates, веса MiniMax H3, компоненты Qwen, FFmpeg, другие upstream-зависимости или сгенерированные материалы. Для каждого сохраняются собственные условия.

Jailbroken или abliterated conditioner не включён: manifest принимает только выровненный файл Comfy-Org `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` с записанной контрольной суммой. MiniMax H3 Community License исключает ЕС, Великобританию, Республику Корея и США из применимой территории и устанавливает обязанности при распространении. До загрузки или применения весов изучите [upstream model card](https://huggingface.co/MiniMaxAI/MiniMax-H3), [лицензию](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE), применимое право и лицензии зависимостей. Проект не предоставляет юридических консультаций.

## Цитирование

При использовании LocalVideoGen в исследовании процитируйте репозиторий. GitHub читает [CITATION.cff](../CITATION.cff) и показывает панель **Cite this repository** на странице репозитория.

```bibtex
@software{chen_localvideogen_2026,
  author = {Chen, Lachlan},
  title = {LocalVideoGen: Resource-Aware Local MiniMax H3 Video Generation},
  year = {2026},
  version = {0.1.0},
  url = {https://github.com/lachlanchen/LocalVideoGen}
}
```

## Статус и область применения

Версия **0.1.0** — исследовательский выпуск для данной рабочей станции, проверенный в Linux с двумя RTX 4090 и 128 GiB RAM. Приоритеты — воспроизводимость, целостность моделей, визуальное качество и безопасное сосуществование с другими длительными проектами, а не широкая поддержка оборудования или установка одним щелчком. Результаты остаются генеративными и требуют проверки перед публикацией.

Проект: [github.com/lachlanchen/LocalVideoGen](https://github.com/lachlanchen/LocalVideoGen) · Сайт: [lazying.art](https://lazying.art)
