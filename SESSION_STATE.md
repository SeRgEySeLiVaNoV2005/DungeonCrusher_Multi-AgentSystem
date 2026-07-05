# Session State — 2026-07-03

## Что за проект

Мультиагентная система автоматизации игры **Dungeon Crusher: Soul Hunters** (Крушители подземелий). Игра запускается на ПК через VK Play. Система имитирует действия пользователя (не внедряется в игру).

## Где код

- **Локально:** `C:\Users\dog24\Desktop\Агенты_для_крушителей_подземелий\`
- **GitHub:** `git@github.com:SeRgEySeLiVaNoV2005/DungeonCrusher_Multi-AgentSystem.git`
- **Ветка:** `framework-base` (запушена, 21 коммит)

## Структура проекта (актуальная)

```
Агенты_для_крушителей_подземелий/
├── AgentLimitationsAndCapabilities.md
├── README.md
├── ThePurposeOfTheWholeProject.md
├── SESSION_STATE.md                     # ← этот файл
├── AutomaticLevelingHeroes_Agent.md     # Спецификация агента прокачки
└── BaseAgent/
    ├── agents/                          # Игровые агенты
    │   ├── __init__.py
    │   ├── combat/
    │   │   ├── __init__.py
    │   │   └── combat_agent.py          # Боевой агент (отключен — нет шаблонов)
    │   └── automatic_leveling_heroes/   # NEW
    │       ├── __init__.py
    │       └── automatic_leveling_heroes_agent.py  # Автопрокачка героев
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
    │   ├── input/         # emulator.py (теперь + ScrollAction)
    │   ├── vision/        # template_matcher.py, ocr.py
    │   ├── communication/ # message_bus.py (теперь + AGENT_STATUS)
    │   ├── game_state/    # state.py
    │   ├── ui/            # command_overlay.py
    │   └── launcher/      # launcher.py (CLI)
    ├── config/settings.yaml
    ├── tests/             # 113 тестов (16 core + 22 tooltip + 19 sm + 22 combat + 34 leveling)
    └── resources/templates/  # 18 PNG-шаблонов UI-кнопок
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
- **97607b2** `perf(cmd): reuse tracker frame, 2× downscale, remove bring_to_front`
- **a092fc1** `fix(cmd): remove 2× downscale that broke template matching`

### Этап 6: AutomaticLevelingHeroesAgent (2026-07-04 — ТЕКУЩИЙ)
- **ScrollAction** — скролл колёсиком мыши (pynput)
- **AGENT_STATUS** / **SYSTEM_TRIGGER_LEVELING** / **SYSTEM_STOP_LEVELING** — новые типы сообщений
- **Multi-scale template matching** — 3 шкалы (1.0, 0.94, 1.06) + early exit на 1.0×
- **ParentAgent координаты** — конвертация window-relative → абсолютные экранные
- **CombatAgent** публикует AGENT_STATUS
- **AutomaticLevelingHeroesAgent** — второй автономный агент
  - 5 состояний: IDLE → NAVIGATING → SCANNING → LEVELING → DONE
  - Win32 GetLastInputInfo для idle detection
  - Двухпроходная стратегия: pass 1 (наём `nanyat`) → pass 2 (прокачка `prokachka`)
  - **Двунаправленный скролл**: вниз до `End.png` → разворот вверх → снова вниз
  - Команда `levelup` — ручной запуск, команда `stop` — остановка в IDLE
  - 18 шаблонов загружено, 113 тестов (34 leveling)
  - Подписка на AGENT_STATUS для отслеживания занятости других агентов
  - Скролл списка героев + поиск четырёх типов кнопок:
    - 🔴 `prokachka.png` — красная (прокачка доступна)
    - 🟣 `HiringHero.png` — фиолетовая (наём героя)
    - ⚫ `prokachka_gray.png` — серая (недоступна)
    - 🏁 `End.png` — конец списка (разворот скролла)
  - Включен в лаунчере по умолчанию

## Коммиты (последние)

```
faa3cc4 perf(agents): optimize LevelingAgent — multi-scale early-exit, two-pass strategy
aa41c59 feat(agents): add hire button support to LevelingAgent
<see git log> feat(agents): add AutomaticLevelingHeroesAgent — autonomous hero leveling
ec04d5c docs: add session resume point to SESSION_STATE
d1c75ed docs: update SESSION_STATE — 14 commits, downscale bug documented
a092fc1 fix(cmd): remove 2× downscale that broke template matching
628667b docs: update SESSION_STATE — commit 97607b2 pushed, 13 commits total
97607b2 perf(cmd): reuse tracker frame, 2× downscale, remove bring_to_front
```

## Статистика тестов

- **113 тестов**, все проходят
- 16 test_core, 19 test_state_machine, 22 test_combat_agent, 22 test_tooltip_reader, **34 test_automatic_leveling_heroes**
- Пробелы: WindowCapturer (0), InputEmulator (0), TemplateMatcher (0), MessageBus (0), ParentAgent (0)

## Известные проблемы

1. **Tesseract OCR не установлен** — системная зависимость, OCR не работает
2. **CombatAgent отключен** — нет боевых шаблонов (enemy_health_bar, battle_banner и др.)
3. **Ctrl+Shift+J занят** — хоткей оверлея не регистрируется, нужно кликать мышкой
4. **ParentAgent._on_user_command** вызывает приватный метод `_capture_via_mss()` — только как fallback, когда трекер пуст
5. **Нет лимита на `_pending_actions`** — может расти бесконечно при спаме

## Что дальше

1. **Установить Tesseract OCR** — системная зависимость (нужно разрешение)
2. **Создать шаблоны для прокачки** — prokachka.png (красная кнопка), prokachka_gray.png (серая кнопка)
3. **Создать боевые шаблоны** — enemy_health_bar, battle_banner, combat_ability_frame
4. **Включить CombatAgent** — раскомментировать в лаунчере
5. **NavigationAgent** — перемещение по карте
6. **ResourceAgent** — сбор золота/душ
7. **Тесты для инфраструктурных модулей** — хотя бы с моками

## Точка восстановления — конец сеанса 2026-07-05

**Последний коммит:** `883b85c` chore(agents): disable _SCROLL_DEBUG verbose logging — **~27 коммитов total**

### Этап 7: Отладка и оптимизация скролла (2026-07-05 — СЕГОДНЯ)

**Проблема:** скроллы происходили каждые ~14 секунд вместо ~400 мс.

**Корень:** `End.png` — шаблон 520×443 px (115 KB), в 17 раз больше остальных (~90×90). `matchTemplate` на скриншоте 1938×1098 занимал ~2.5 сек на каждый кадр. Плюс кнопки (180×74 px) добавляли ещё ~1.3 сек.

**Исправления (5 коммитов):**
1. `92ab8d8` — Диагностика: `perf_counter` тайминги, per-frame счётчики, ENTER/EXIT логи
2. `9ecd34b` — Frame-skip: End.png каждые 8 кадров, кнопки каждые 2 кадра → 14s→4s (×3.5)
3. `0aa1653` — Один guard-чек на цикл (`countdown == N-1`) → 4s→3.3s
4. `3db7def` — End-check раз в 3 цикла → 3.3s→3.0s
5. `fc0757a` — **2× downscale скриншота** (1938×1098 → 969×549) — matchTemplate ускорился в 4.8× (0.433s → 0.090s на шкалу)

**Результат:** скроллы каждые **~1.0–1.6 секунд** (было 14s) — **ускорение в 11.5×**.

**Добавлено в этом сеансе:**
- ✅ AutomaticLevelingHeroesAgent (5-state FSM, Win32 idle detection)
- ✅ ScrollAction в InputEmulator (скролл колёсиком)
- ✅ AGENT_STATUS в MessageBus (busy/idle трекинг)
- ✅ CombatAgent публикует AGENT_STATUS
- ✅ AutomaticLevelingConfig в config.py + settings.yaml
- ✅ 3 шаблона: prokachka.png, prokachka_gray.png, HiringHero.png
- ✅ Поддержка фиолетовой кнопки найма (HiringHero)
- ✅ 26 новых тестов, 105 total

**Что работает:**
- ✅ CommandOverlay — строка ввода поверх игры
- ✅ Шаблоны матчатся (17 шт.) — `match_confidence: 0.6`
- ✅ Web-сервер http://127.0.0.1:8765
- ✅ TooltipReaderAgent (CTRL+H)
- ✅ AutomaticLevelingHeroesAgent — полностью готов к работе
- ⚠️ Ctrl+Shift+J — занят, фокус на оверлей только мышкой
- ❌ CombatAgent отключен (нет боевых шаблонов)
- ❌ Tesseract OCR не установлен

**Инструкция для следующего сеанса:**
1. Прочитай этот файл (`SESSION_STATE.md`)
2. Проверь `git status` и `git log --oneline -5`
3. Лаунчер: `cd BaseAgent && python -m src.launcher`
4. Скролл оптимизирован (1.2s/цикл). Следующие задачи: End.png crop (80×40), stuck detection, боевые шаблоны

---

## Правила работы

1. Спрашивать разрешение перед установкой ЛЮБОЙ библиотеки
2. Системные зависимости — только с разрешения
3. Коммитить каждое значимое изменение
4. Conventional commits: feat:, fix:, refactor:, chore:, docs:, test:
5. **После каждой задачи обновлять этот файл**
