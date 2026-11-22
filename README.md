# Мухоморный тест: знают ли LLM химический состав грибов

Recall-тест локальных и облачных LLM на знание химического состава
8 грибов: красный, пантерный, лимонный и вонючий мухоморы, бледная
поганка, опёнок осенний, серо-розовый и цезарский (съедобные —
контрольная группа).

Модели перечисляют состав в JSON (вещество / доля / влияние на
человека), каждый ответ сверяется судьёй-LLM (glm-5.3-flash, thinking
low) с эталоном из 118 веществ. База веществ заранее составлена из
рецензируемых обзоров (Michelot & Melendez-Howell 2003, Wieland
1983/86, Carboué & Lopez 2021 MDPI, Dörfer 2019, Pfütze 2024), состав
сверен с монографией Вишневского 2024.

Две серии — русская и английская. 13 моделей, 270 прогонов,
2160 свекённых ответов. Главный вывод: без RAG состав грибов LLM
не знает — лучший (gemini-4-neon) покрывает 54–58% эталонных основных
соединений, половина названного — выдумки.

Публикация: [лидерборды prikotov.pro](https://prikotov.pro/leaderboards) —
[полный отчёт RU](https://prikotov.pro/raw/mushroom-20261122/report.html),
[EN](https://prikotov.pro/raw/mushroom-20261122/report-en.html),
[эталон и источники](https://prikotov.pro/raw/mushroom-20261122/ethalon-ru.html).

## Файлы

- `questions-composition.json` / `questions-composition-en.json` —
  вопросники (v4.3 / v4.3-en): system-промпт, 8 клеток, JSON-контракт
  (en — английскими ключами);
- `ethalon.json` / `ethalon-en.json` — эталонные составы (v13), язык
  RU и EN;
- `runner_mush.py` — раннер локальных моделей (ollama /api/chat,
  structured outputs, изоляция прогонов, keep_alive);
- `runner_deepseek.py` — раннер API-моделей через pi (deepseek, xiaomi
  mimo, kimi, qwen, openrouter);
- `judge_mush.py` — судья v2: строгий протокол, эталон — единственный
  источник истины, «нет в справке» = «галлюцинация», переиспользование
  вердиктов по хэшам входов;
- `build_mush_report.py` — генератор отчётов: уровни, сводная матрица
  от эталона, фантазии, контрольная группа, методика;
- `compare-ru-en.py` — сравнение RU vs EN серий;
- `models/`, `models-en/` — сырые данные: session.json (условия и
  ответы), verdicts.json (вердикты судьи), answers.csv, отчёты уровней;
- `archive/` — архив старых серий (v1–v3 промпты, свободное
  декодирование до format-схемы, старые версии судьи) — в отчёты не
  попадают;
- `blueprint.md` — методология целиком: уроки каждой версии промпта,
  судьи и транспорта.

## Запуск

```bash
python3 runner_mush.py --model gemma4:26b --runs 5 --think low
python3 judge_mush.py models/gemma4-26b/composition
python3 build_mush_report.py --model-dir models/gemma4-26b/composition --think low
python3 build_mush_report.py --index
```

OLLAMA_BASE_URL берётся из `.env.local` (не в репозитории).
