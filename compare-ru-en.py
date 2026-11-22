#!/usr/bin/env python3
"""Сравнение RU- и EN-серий: та же методика, разные языки вопросов.

Считает по каждой серии (модель × think): полноту эталонных «основных»,
долю галлюцинаций (вердикты судьи), стабильность (Jaccard между
прогонами), средний балл судьи, среднюю длину ответа, долю «пищевых»
позиций. Пишет compare-ru-en.html и печатает сводку в консоль.

Запуск: python3 compare-ru-en.py
"""

import itertools
import json
import statistics
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from build_mush_report import (norm_name, match_strict, ethalon_targets,
                               page)  # noqa: E402

LEVELS = {"off": range(1, 6), "low": range(6, 11),
          "medium": range(11, 16), "xhigh": range(16, 21)}
# серийные объёмы 1:1 между RU и EN (решение владельца «в en столько же
# прогонов, сколько в ru»); API-модели: уровень = уровень их RU-серии
MODEL_LEVELS = {
    "gemma4-26b": LEVELS,
    "gemma4-31b-it-qat": LEVELS,
    "huihui-ai-pi-100k": LEVELS,
    "medgemma-27b": {"off": LEVELS["off"]},
    "orcarouter-Qwen3.8-27B-Uncensored-latest": LEVELS,
    "qwen3.8-latest": LEVELS,
    "deepseek-flash": {"off": LEVELS["off"]},
    "mimo-v2.6-pro": {"off": LEVELS["off"]},
    "kimi-k3.1-pro": {"low": range(1, 6)},
    "qwen4-max-preview": {"low": range(1, 6)},
    "qwen4-max": {"low": range(1, 6)},
    "gemini-4-neon": {"low": range(1, 3)},
    "gpt-6.2-sol": {"low": range(1, 3)},
}
MODELS = list(MODEL_LEVELS)
FOOD_RU = {"белки", "углеводы", "полисахариды", "вода", "липиды", "жиры",
           "аминокислоты", "клетчатка", "пищевые волокна", "минералы",
           "макроэлементы", "микроэлементы", "золота", "пищеварение"}
FOOD_EN = {"proteins", "protein", "carbohydrates", "carbohydrate",
           "polysaccharides", "water", "lipids", "fats", "amino acids",
           "fiber", "dietary fiber", "minerals", "macronutrients",
           "micronutrients", "ash"}


def is_food(name: str, food: set[str]) -> bool:
    n = name.lower().strip()
    return any(n.startswith(f) for f in food)


def series_metrics(models_root: str, ethalon_file: str, food: set[str]):
    eth = json.loads((SCRIPT_DIR / ethalon_file).read_text(
        encoding="utf-8"))["mushrooms"]
    out = {}
    for slug in MODELS:
        for lv, rng in MODEL_LEVELS[slug].items():
            covs, lens, scores, hals, food_sh = [], [], [], [], []
            by_q: dict[str, list[set]] = {}
            for r in rng:
                try:
                    sess = json.loads((SCRIPT_DIR / models_root / slug /
                                       "composition" / f"run-{r:02d}" /
                                       "session.json").read_text(
                                           encoding="utf-8"))
                except FileNotFoundError:
                    continue
                try:
                    vd = json.loads((SCRIPT_DIR / models_root / slug /
                                     "composition" / f"run-{r:02d}" /
                                     "verdicts.json").read_text(
                                         encoding="utf-8"))["verdicts"]
                except FileNotFoundError:
                    vd = {}
                for t in sess["turns"]:
                    items = t.get("composition") or []
                    names = {norm_name(x.get("вещество", ""))
                             for x in items} - {""}
                    lens.append(len(items))
                    by_q.setdefault(t["id"], []).append(names)
                    if names:
                        food_sh.append(
                            sum(is_food(x.get("вещество", ""), food)
                                for x in items) / len(items))
                    targets = set()
                    for e in eth.get(t["id"], {}).get("основные") or []:
                        targets |= {norm_name(x) for x in ethalon_targets(e)}
                    if targets:
                        hit = sum(any(match_strict(nm, tg) for nm in names)
                                  for tg in targets)
                        covs.append(hit / len(targets))
                    v = vd.get(t["id"])
                    if v:
                        scores.append(v.get("балл"))
                        subs = v.get("вещества") or []
                        if subs:
                            hals.append(
                                sum(x.get("статус") == "галлюцинация"
                                    for x in subs) / len(subs))
            stabs = []
            for sets in by_q.values():
                if len(sets) < 2:
                    continue
                vals = [len(a & b) / len(a | b)
                        for a, b in itertools.combinations(sets, 2)
                        if a | b]  # обе пусты → пара не информативна
                if vals:
                    stabs.append(statistics.mean(vals))
            out[(slug, lv)] = {
                "cov": statistics.mean(covs) if covs else 0.0,
                "hal": statistics.mean(hals) if hals else 0.0,
                "stab": statistics.mean(stabs) if stabs else 0.0,
                "score": statistics.mean(scores) if scores else 0.0,
                "len": statistics.mean(lens) if lens else 0.0,
                "food": statistics.mean(food_sh) if food_sh else 0.0,
            }
    return out


def main() -> int:
    ru = series_metrics("models", "ethalon.json", FOOD_RU)
    en = series_metrics("models-en", "ethalon-en.json", FOOD_EN)

    def fmt(v, pct=True, digits=1):
        if v is None:
            return "—"
        return f"{v:.{digits}f}" + ("%" if pct else "")

    rows = []
    for slug in MODELS:
        for lv in MODEL_LEVELS[slug]:
            r, e = ru.get((slug, lv)), en.get((slug, lv))
            if not r and not e:
                continue
            r = r or {}
            e = e or {}
            rows.append(
                f'<tr><td>{escape(slug.replace("-Qwen3.8-27B-Uncensored", ""))}'
                f'</td><td>{lv}</td>'
                f'<td>{fmt(100*r.get("cov", 0))}</td><td>{fmt(100*e.get("cov", 0))}</td>'
                f'<td>{fmt(100*r.get("hal", 0))}</td><td>{fmt(100*e.get("hal", 0))}</td>'
                f'<td>{fmt(100*r.get("stab", 0))}</td><td>{fmt(100*e.get("stab", 0))}</td>'
                f'<td>{fmt(r.get("score", 0), pct=False)}</td><td>{fmt(e.get("score", 0), pct=False)}</td>'
                f'<td>{fmt(r.get("len", 0), pct=False)}</td><td>{fmt(e.get("len", 0), pct=False)}</td>'
                f'<td>{fmt(100*r.get("food", 0))}</td><td>{fmt(100*e.get("food", 0))}</td></tr>')

    body = f"""
<h2>Модель × уровень: RU-серия vs EN-серия</h2>
<div class="scroll"><table class="sx">
<tr><th>Модель</th><th>think</th>
<th>осн RU</th><th>осн EN</th>
<th>галл RU</th><th>галл EN</th>
<th>стаб RU</th><th>стаб EN</th>
<th>балл RU</th><th>балл EN</th>
<th>длин RU</th><th>длин EN</th>
<th>пищев RU</th><th>пищев EN</th></tr>
{"".join(rows)}
</table></div>
<div class="legend" style="margin-top:10px"><span class="hint">
осн — полнота эталонных «основных» (match по именам); галл — доля вердиктов
«галлюцинация» (судья v2, строгий: нет в эталоне = галлюцинация);
стаб — средний попарный Jaccard наборов между прогонами; балл — средний
балл судьи 0–10; длин — среднее число веществ в ответе; пищев — доля
позиций пищевого состава (RU- и EN-словари). RU = models/ (промпты v4.3,
русские названия), EN = models-en/ (questions-composition-en.json,
ethalon-en.json, те же модели и уровни)</span></div>
"""
    chips = ('<div class="chip">Серии: <b>RU (v4.3)</b> и <b>EN (v4.3-en)</b>'
             '</div><div class="chip">Эталоны: ethalon.json / ethalon-en.json'
             ' (зеркала)</div>')
    page_html = page(
        "RU vs EN: отвечает ли модель иначе на английском?",
        "Сравнение двух серий одной методики: вопросы и названия грибов "
        "на русском и английском. Одинаковые модели, уровни размышлений "
        "и судья; эталоны — языковые зеркала (одни и те же факты).",
        chips, body,
        f"построено compare-ru-en.py · "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    (SCRIPT_DIR / "compare-ru-en.html").write_text(page_html,
                                                   encoding="utf-8")
    print(f"отчёт: {SCRIPT_DIR / 'compare-ru-en.html'}")

    print(f"\n{'модель':22s} {'ур':6s} | осн RU→EN | галл RU→EN | стаб RU→EN | балл RU→EN")
    for slug in MODELS:
        for lv in MODEL_LEVELS[slug]:
            r, e = ru.get((slug, lv)), en.get((slug, lv))
            if not r and not e:
                continue
            r = r or {}
            e = e or {}
            print(f"{slug[:22]:22s} {lv:6s} |   {r.get('cov',0):4.0%}→{e.get('cov',0):4.0%} "
                  f"|  {r.get('hal',0):4.0%}→{e.get('hal',0):4.0%} "
                  f"| {r.get('stab',0):4.0%}→{e.get('stab',0):4.0%} "
                  f"|  {r.get('score',0):3.1f}→{e.get('score',0):3.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
