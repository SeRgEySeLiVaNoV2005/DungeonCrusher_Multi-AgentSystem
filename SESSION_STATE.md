# Session State — 2026-07-03

## Что за проект

Мультиагентная система автоматизации игры **Dungeon Crusher: Soul Hunters** (Крушители подземелий). Игра запускается на ПК через VK Play. Система имитирует действия пользователя (не внедряется в игру).

## Где код

- **Локально:** `C:\Users\dog24\Desktop\Агенты_для_крушителей_подземелий\`
- **GitHub:** `git@github.com:SeRgEySeLiVaNoV2005/DungeonCrusher_Multi-AgentSystem.git`
- **Ветка:** `framework-base` (запушена)

## Структура проекта (актуальная)

```
Агенты_для_крушителей_подземелий/
├── AgentLimitationsAndCapabilities.md
├── README.md
├── ThePurposeOfTheWholeProject.md
├── SESSION_STATE.md                     # ← этот файл
└── BaseAgent/
    ├── agents/                          # Игровые агенты (НОВОЕ)
    │   ├── __init__.py
    │   └── combat/
    │       ├── __init__.py
    │       └── combat_agent.py          # Первый боевой агент
    ├── base/              # BaseAgent (ABC) + ChildAgent
    │   ├── __init__.py
    │   ├── base_agent.py
    │   └── child_agent.py
    ├── parent/            # ParentAgent — координатор
    ├── watcher/           # WatcherAgent — отладка
    ├── tooltip_reader/    # TooltipReaderAgent (CTRL+H OCR)
    ├── src/
    │   ├── core/          # config, exceptions, logger, state_machine (НОВОЕ)
    │   ├── capture/       # window_capturer.py
    │   ├── input/         # emulator.py
    │   ├── vision/        # template_matcher.py, ocr.py
    │   ├── communication/ # message_bus.py
    │   ├── game_state/    # state.py
    │   └── launcher/      # launcher.py (CLI)
    ├── config/settings.yaml
    ├── tests/             # 79 тестов (16 core + 38 tooltip + 19 sm + 22 combat)
    └── resources/templates/
```

## Что сделано (хронология)

### Этап 1: Инфраструктура (8 коммитов)
- Screen capture (MSS + Win32), input emulation (pynput)
- Vision (OpenCV template matching + Tesseract OCR)
- MessageBus (pub/sub), GameState + StateTracker
- ParentAgent (главный цикл), ChildAgent (база), WatcherAgent
- CLI launcher, конфигурация, 16 тестов

### Этап 2: TooltipReader (2 коммита)
- CTRL+H хоткей, Win32 RegisterHotKey
- Буфер обмена (Win+Shift+S), 9 стратегий OCR (3×3)
- Веб-интерфейс ревью (localhost:8765)
- UIElementDB + PendingElementStore

### Этап 3: Боевой агент и интеграция (2 коммита — СЕГОДНЯ)
- **StateMachine** — легковесный FSM (guarded transitions, hooks, ANY-state)
- **CombatAgent** — первый автономный игровой агент:
  - 4 состояния: IDLE → SCANNING → COMBAT → CLEANUP
  - 2 сканера: template matching + colour heuristic (HSV red detection)
  - Ротация способностей с кулдауном
- **Интеграция:** UIElementDB → TemplateMatcher (рантайм-регистрация шаблонов)
- **Лаунчер:** создаёт TemplateMatcher, передаёт WebReviewServer и CombatAgent

## Коммиты (последние)

```
23eeeef feat(launcher): integrate TemplateMatcher, CombatAgent, and UI element pipeline
8e75bab feat(agents): add StateMachine and CombatAgent — first autonomous game agent
af42598 feat(tooltip-reader): upgrade OCR pipeline and hotkey system
d8b248a fix(config): add Russian and VK Play window search keywords
8b802ab fix(capture,vision): filter ghost windows and dedupe template loading
```

## Статистика тестов

- **79 тестов**, все проходят
- 16 test_core, 19 test_state_machine, 22 test_combat_agent, 22 test_tooltip_reader

## Что дальше

1. **Установить Tesseract OCR** — системная зависимость (нужно разрешение)
2. **Создать шаблоны UI-элементов** — запустить игру, использовать CTRL+H для захвата:
   - enemy_health_bar — красная полоска здоровья врага
   - battle_banner — баннер начала боя
   - combat_ability_frame — рамка способностей
   - victory_screen — экран победы
3. **Протестировать CombatAgent** на реальной игре
4. **Навигационный агент** (NavigationAgent) — перемещение по карте
5. **Агент сбора ресурсов** (ResourceAgent) — сбор золота/душ
6. **Обучение с учителем** (human-in-the-loop) — демонстрация действий

## Правила работы

1. Спрашивать разрешение перед установкой ЛЮБОЙ библиотеки
2. Системные зависимости — только с разрешения
3. Коммитить каждое значимое изменение
4. Conventional commits: feat:, fix:, refactor:, chore:, docs:, test:
5. **После каждой задачи обновлять этот файл**
