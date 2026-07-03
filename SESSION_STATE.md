# Session State — 2026-07-03

## Что за проект

Мультиагентная система автоматизации игры **Dungeon Crusher: Soul Hunters** (Крушители подземелий). Игра запускается на ПК через VK Play. Система имитирует действия пользователя (не внедряется в игру).

## Где код

- **Локально:** `C:\Users\dog24\Desktop\Агенты_для_крушителей_подземелий\`
- **GitHub:** `git@github.com:SeRgEySeLiVaNoV2005/DungeonCrusher_Multi-AgentSystem.git`
- **Ветка:** `framework-base` (запушена, 14 коммитов)

## Структура проекта (актуальная)

```
Агенты_для_крушителей_подземелий/
├── AgentLimitationsAndCapabilities.md
├── README.md
├── ThePurposeOfTheWholeProject.md
├── SESSION_STATE.md                     # ← этот файл
└── BaseAgent/
    ├── agents/                          # Игровые агенты
    │   ├── __init__.py
    │   └── combat/
    │       ├── __init__.py
    │       └── combat_agent.py          # Боевой агент (отключен — нет шаблонов)
    ├── base/              # BaseAgent (ABC) + ChildAgent
    │   ├── __init__.py
    │   ├── base_agent.py
    │   └── child_agent.py
    ├── parent/            # ParentAgent — координатор
    ├── watcher/           # WatcherAgent — отладка
    ├── tooltip_reader/    # TooltipReaderAgent (CTRL+H OCR)
    ├── src/
    │   ├── core/          # config, exceptions, logger, state_machine
    │   ├── capture/       # window_capturer.py
    │   ├── input/         # emulator.py
    │   ├── vision/        # template_matcher.py, ocr.py
    │   ├── communication/ # message_bus.py
    │   ├── game_state/    # state.py
    │   ├── ui/            # command_overlay.py
    │   └── launcher/      # launcher.py (CLI)
    ├── config/settings.yaml
    ├── tests/             # 79 тестов (16 core + 22 tooltip + 19 sm + 22 combat)
    └── resources/templates/  # 14 PNG-шаблонов UI-кнопок
```

## Хронология

### Этап 1: Инфраструктура (8 коммитов)
- Screen capture (MSS + Win32 PrintWindow), input emulation (pynput)
- Vision (OpenCV template matching + Tesseract OCR)
- MessageBus (pub/sub), GameState + StateTracker
- ParentAgent (главный цикл), ChildAgent (база), WatcherAgent
- CLI launcher, конфигурация, 16 тестов

### Этап 2: TooltipReader (2 коммита)
- CTRL+H хоткей, Win32 RegisterHotKey
- Буфер обмена (Win+Shift+S), 9 стратегий OCR (3 языка × 3 препроцесса)
- Веб-интерфейс ревью (localhost:8765)
- UIElementDB + PendingElementStore (14 элементов в БД)

### Этап 3: Боевой агент и интеграция (2 коммита)
- **StateMachine** — легковесный FSM (guarded transitions, hooks, ANY-state)
- **CombatAgent** — первый автономный игровой агент
  - 4 состояния: IDLE → SCANNING → COMBAT → CLEANUP
  - 2 сканера: template matching + colour heuristic (HSV red detection)
  - Ротация способностей с кулдауном
  - Отключен в лаунчере — нет боевых шаблонов

### Этап 4: Command Overlay (5 коммитов)
- **CommandOverlay** — плавающее окно ввода поверх игры (tkinter)
  - always-on-top, полупрозрачное (α=0.85), без рамки
  - Ввод названия кнопки → Enter → поиск шаблона → клик
  - Ctrl+Shift+J — фокус на поле ввода (⚠️ занят другим приложением)
  - Русские алиасы: настройки→nastroyki, магазин→magazin и т.д.
  - Обратная связь: ✓ зелёный / ✗ красный в лейбле
- **ParentAgent._on_user_command()** — поиск и клик по шаблону
  - 3 стратегии: UIElementDB → русские алиасы → имена шаблонов
  - Публикация COMMAND_RESULT для оверлея

### Этап 5: Оптимизация и отладка (СЕГОДНЯ)
- **9410954** `perf: memory optimization — gc import, reduced history buffer`
  - Добавлен `import gc`, StateTracker.max_history 300→10
- **97607b2** `perf(cmd): reuse tracker frame, 2× downscale, remove bring_to_front`
  - Переиспользование последнего кадра из трекера (вместо свежего MSS)
  - ~~Даунскейл скриншота 2×~~ (откачен в `a092fc1` — ломал template matching)
  - Убран лишний bring_to_front после клика
  - `match_confidence: 0.8→0.6` (после перезагрузки ноутбука часть шаблонов давала <0.8)
  - Задержка CommandOverlay: 700-1000мс → 150-250мс
- **a092fc1** `fix(cmd): remove 2× downscale that broke template matching`
  - Шаблон оставался в исходном разрешении, скриншот сжимался → match 0%
  - Убран даунскейл, скриншот подаётся в оригинальном разрешении

## Коммиты (последние)

```
a092fc1 fix(cmd): remove 2× downscale that broke template matching
628667b docs: update SESSION_STATE — commit 97607b2 pushed, 13 commits total
97607b2 perf(cmd): reuse tracker frame, 2× downscale, remove bring_to_front
9410954 perf: memory optimization — gc import, reduced history buffer
c0105a3 perf(cmd): 4x latency reduction — fresh MSS, skip find_all, no sleep on click
9977536 fix(cmd): current is a @property, not a method — remove parentheses
2b39862 perf(cmd): reuse latest frame, single-template match, skip debug save
6e88a72 fix(cmd): remove slow window-hide cycle, return focus to game after click
a73c76a feat(ui): add command overlay — floating input window for button clicks
```

## Статистика тестов

- **79 тестов**, все проходят
- 16 test_core, 19 test_state_machine, 22 test_combat_agent, 22 test_tooltip_reader
- Пробелы: WindowCapturer (0), InputEmulator (0), TemplateMatcher (0), MessageBus (0), ParentAgent (0)

## Известные проблемы

1. **Tesseract OCR не установлен** — системная зависимость, OCR не работает
2. **CombatAgent отключен** — нет боевых шаблонов (enemy_health_bar, battle_banner и др.)
3. **Ctrl+Shift+J занят** — хоткей оверлея не регистрируется, нужно кликать мышкой
4. **ParentAgent._on_user_command** вызывает приватный метод `_capture_via_mss()` — только как fallback, когда трекер пуст
5. **Нет лимита на `_pending_actions`** — может расти бесконечно при спаме

## Что дальше

1. **Установить Tesseract OCR** — системная зависимость (нужно разрешение)
2. **Создать боевые шаблоны** — enemy_health_bar, battle_banner, combat_ability_frame
3. **Включить CombatAgent** — раскомментировать в лаунчере
4. **NavigationAgent** — перемещение по карте
5. **ResourceAgent** — сбор золота/душ
6. **Тесты для инфраструктурных модулей** — хотя бы с моками

## Точка восстановления — конец сеанса 2026-07-03

**Лаунчер запущен в фоне** (PID `bo37mbdvx`), оверлей висит поверх игры.  
SystemLauncher, ParentAgent, TooltipReaderAgent работают.

**Последний коммит:** `d1c75ed` — docs: update SESSION_STATE — 14 commits, downscale bug documented

**Что работает прямо сейчас:**
- ✅ CommandOverlay — строка ввода поверх игры, Enter → клик по шаблону
- ✅ Шаблоны матчатся (14 шт.) — `match_confidence: 0.6`
- ✅ Web-сервер http://127.0.0.1:8765
- ✅ TooltipReaderAgent (CTRL+H)
- ⚠️ Ctrl+Shift+J — занят, фокус на оверлей только мышкой
- ❌ CombatAgent отключен (нет боевых шаблонов)
- ❌ Tesseract OCR не установлен

**Что дальше (приоритет):**
1. Создать боевые шаблоны → включить CombatAgent
2. Установить Tesseract OCR
3. NavigationAgent / ResourceAgent
4. Тесты для инфраструктурных модулей

**Инструкция для следующего сеанса:**
1. Прочитай этот файл (`SESSION_STATE.md`)
2. Проверь `git status` и `git log --oneline -5`
3. Лаунчер, возможно, ещё жив — проверь `Get-Process python`
4. Продолжай с того места, где остановились

---

## Правила работы

1. Спрашивать разрешение перед установкой ЛЮБОЙ библиотеки
2. Системные зависимости — только с разрешения
3. Коммитить каждое значимое изменение
4. Conventional commits: feat:, fix:, refactor:, chore:, docs:, test:
5. **После каждой задачи обновлять этот файл**
