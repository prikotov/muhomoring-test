#!/usr/bin/env python3
"""Сборка HTML-отчётов теста на знание состава мухоморов.

Режимы:
    --model-dir models/<модель>/composition --think off
        Сводный отчёт одного уровня размышлений по всем прогонам (5 шт):
        для каждого гриба — единая таблица «вещество × прогон»; клетка
        окрашена вердиктом судьи (реальное / спорно / галлюцинация),
        серая — вещество в этом прогоне не перечислено. Плюс стабильность
        (Jaccard состава между прогонами), пропуски эталонных веществ,
        баллы судьи. Пишет report-<think>.html в папку модели.

    --index
        Сводный отчёт по всем моделям и уровням: таблица «модель ×
        think» (балл судьи, галлюцинации, стабильность, JSON-разбор,
        время), ссылки на отчёты уровней. Пишет report.html в корень.

Стиль — из карты знаний PHP (сетка клеток с тултипами), содержимое —
recall-состав и вердикты судьи.
"""

import argparse
import difflib
import html
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

COLOR_OK = "#3aa25c"     # реальное вещество
COLOR_AMBER = "#e8b64c"  # спорно
COLOR_BAD = "#d75f5f"    # галлюцинация
COLOR_NA = "#a9b0ba"     # не перечислено / нет вердикта
STATUS_COLOR = {"реальное": COLOR_OK, "спорно": COLOR_AMBER,
                "галлюцинация": COLOR_BAD}


def esc(s) -> str:
    return html.escape(str(s), quote=True)


# «неизвестно» и синонимы, которыми модели заполняют долю, когда
# не знают значения (решение владельца: в клетках показывать внятную
# подпись «доля не указана», а не голое «неизвестно»)
UNKNOWN_AMT_RE = re.compile(
    r"^(неизвестно|unknown|не\s*установлено|не\s*указано|нет\s*данных|"
    r"н/?д|—|-|not\s*specified|n/a)$", re.IGNORECASE)


def is_unknown_amount(value: str | None) -> bool:
    return bool(UNKNOWN_AMT_RE.match((value or "").strip()))


def norm_name(name: str) -> str:
    """Нормализация названия соединения для сопоставления: нижний регистр,
    без содержимого скобок (включая вложенные и незакрытые — обрезанные
    ответы оставляют «хвост» после «(»), без пунктуации, сжатые пробелы;
    альфа/бета/гамма → α/β/γ (иначе «альфа-аманитин» не матчится с
    «α-аманитин»)."""
    s = name.lower()
    # `\t` из JSON-строки декодировался в таб-символ (модель писала \text
    # без двойного бэкслеша) — восстановить, иначе «$<таб>ext{…}$» остаётся
    # мусором; \n/\r в названии тоже артефакт декодирования
    s = re.sub(r"\t", r"\\t", s)
    s = re.sub(r"[\n\r]", " ", s)
    # LaTeX-обёртки, которыми модели украшают греческие префиксы:
    # «$\alpha$-аманитин», «$\text{alpha-amanitin}$» и даже «$\text{violin}$»
    s = re.sub(r"\\text\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\(?:alpha|beta|gamma)", lambda m: {"\\alpha": "α",
                "\\beta": "β", "\\gamma": "γ"}[m.group(0)], s)
    s = s.replace("$", " ").replace("\\", " ")
    s = s.replace("{", " ").replace("}", " ")
    while True:
        s2 = re.sub(r"\([^()]*\)", " ", s)
        if s2 == s:
            break
        s = s2
    if "(" in s:  # незакрытая скобка — всё после неё не название
        s = s.split("(", 1)[0]
    s = s.replace(")", " ")
    s = s.replace("альфа", "α").replace("бета", "β").replace("гамма", "γ")
    s = re.sub(r"\balpha\b|(?<=-)alpha|alpha(?=-)", "α", s)
    s = re.sub(r"\bbeta\b|(?<=-)beta|beta(?=-)", "β", s)
    s = re.sub(r"\bgamma\b|(?<=-)gamma|gamma(?=-)", "γ", s)
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"[«»\"'`.,;:!?№]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# обобщённые токены, которые не могут быть признаком совпадения
# («ванилиновая кислота» не матчится с «стизолобиновая кислота» из-за слова
# «кислота»)
GENERIC_TOKENS = {"кислота", "кислоты", "кислот", "пигмент", "пигменты",
                  "алкалоид", "алкалоиды", "токсин", "токсины", "белок",
                  "белки", "сахар", "сахара", "спирт", "спирты", "эфир",
                  "эфиры", "амин", "амины", "пептид", "пептиды", "металлы",
                  "минералы", "соединение", "соединения", "вещество",
                  "вещества", "полисахариды", "жиры", "липиды"}


# Наивная кириллица→латиница: научные названия транслитерируются почти
# 1:1 (аманитин→amanitin, мускарин→muskarin) — основа скриптового
# фолбэка в match_strict (модель ответила латиницей внутри русского
# ответа против кириллического эталона). Фаллоидин→falloidin не
# совпадёт с phalloidin — это честный предел наивной транслитерации.
_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "x", "ц": "c", "ч": "ch", "ш": "sh", "щ": "shh",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def _cyr_to_lat(s: str) -> str:
    return s.translate(_TRANSLIT)


# Вариант ф→ph: греческие по происхождению термины в научной латыни
# пишутся через ph (фаллоидин→phalloidin, фенол→phenol)
_TRANSLIT_PH = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "ph", "х": "x", "ц": "c", "ч": "ch", "ш": "sh", "щ": "shh",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def _cyr_to_lat_ph(s: str) -> str:
    return s.translate(_TRANSLIT_PH)


def _match_core(na: str, nb: str, sfx: str = r"[а-яё]") -> bool:
    if na == nb:
        return True
    ta, tb = na.split(), nb.split()
    la = max(ta, key=len) if ta else ""
    lb = max(tb, key=len) if tb else ""
    if len(ta) == 1 and len(tb) == 1 and \
            difflib.SequenceMatcher(None, na, nb).ratio() >= 0.92:
        return True
    if len(lb) >= 5 and lb not in GENERIC_TOKENS and \
            re.search(rf"\b{re.escape(lb)}{sfx}{{0,3}}\b", na):
        return True
    if len(la) >= 5 and la not in GENERIC_TOKENS and \
            re.search(rf"\b{re.escape(la)}{sfx}{{0,3}}\b", nb):
        return True
    return False


def match_strict(na: str, nb: str) -> bool:
    """Строгое сопоставление названий с эталоном. Фантазии-мимикры
    («иботениловая/иботиновая кислота» vs «иботеновая», «иботен») не
    матчатся: ratio-ветка только для однословных имён (морфология:
    «мусцимола»≈«мусцимол»), вхождение — по границе слова и не более 3
    букв наращивания («аманитин» ⊂ «α-аманитин» да, «иботен» ⊄
    «иботеновая» нет). Фолбэк: латиница ответа против кириллицы эталона
    (и наоборот) — наивная транслитерация обеих сторон, без семантики
    (решение владельца: «α-amanitin» = «α-аманитин» в эталоне)."""
    if _match_core(na, nb):
        return True
    if _match_core(_cyr_to_lat(na), _cyr_to_lat(nb), sfx=r"[а-яёa-z]"):
        return True
    return _match_core(_cyr_to_lat_ph(na), _cyr_to_lat_ph(nb),
                       sfx=r"[а-яёa-z]")


def ethalon_targets(entry: dict) -> list[str]:
    """Строки эталона, по которым матчится название модели: само поле
    «вещество» плюс перечисленные в скобках члены («органические кислоты
    (яблочная, аскорбиновая…)» → яблок./аскорб. тоже эталонные). Для
    записей «не обнаружены» скобки НЕ разворачиваются: перечисленное там —
    то, чего в грибе НЕТ (мускарин у цезарского не должен получать ✓)."""
    name = entry.get("вещество", "")
    if "не обнаружен" in (entry.get("содержание") or "").lower():
        return [name]
    m = re.match(r"^(.*?)\((.*)\)", name)
    if not m:
        return [name]
    head = m.group(1).strip(" ,")
    items = [x.strip(" .") for x in m.group(2).split(",") if x.strip(" .")]
    return ([head] if head else []) + items


def cluster_substances(names: list[str]) -> dict[str, list[str]]:
    """Группировка названий ТОЛЬКО по точному совпадению после нормализации:
    схожие, но разные вещества (иботеновая/ибогеновая, мусцимол/мусцимола)
    не должны сливаться — это разные вердикты судьи. Возвращает
    {каноническое_имя: [исходные формы]} — канон самая частая форма."""
    groups: dict[str, list[str]] = {}
    for n in names:
        groups.setdefault(norm_name(n), []).append(n)
    return groups


def match_ethalon(name: str, ethalon_names: list[str]) -> str | None:
    """Какому эталонному соединению соответствует название модели
    (или None). Строгое сопоставление — как в метке «эталон»."""
    nn = norm_name(name)
    for e in ethalon_names:
        if match_strict(nn, norm_name(e)):
            return e
    return None


def load_runs(model_dir: Path, think: str | None) -> list[dict]:
    """Прогоны уровня: [{'dir', 'session', 'verdicts'}]."""
    runs = []
    for rd in sorted(model_dir.glob("run-*")):
        s_path = rd / "session.json"
        if not s_path.exists():
            continue
        session = json.loads(s_path.read_text(encoding="utf-8"))
        if think and session.get("think") != think:
            continue
        v_path = rd / "verdicts.json"
        verdicts = json.loads(v_path.read_text(encoding="utf-8")) if v_path.exists() else {}
        runs.append({"dir": rd, "session": session,
                     "verdicts": verdicts.get("verdicts", {})})
    if not runs:
        raise SystemExit(f"в {model_dir} нет прогонов с think={think}")
    return runs


def verdict_for(verdict: dict | None, substance_name: str) -> dict | None:
    """Вердикт судьи для конкретного соединения ответа (по названию).
    Судья v2 повторяет имя модели как есть — достаточно строгого
    матчинга. Прежний нечёткий проход (0.55) убран: он склеивал
    мимикров («Хитин» ловил вердикт «Холина» → зелёный вместо
    красного); потеря вердикта на переформулированном имени честнее
    неверной склейки — клетка остаётся без окраски."""
    if not verdict:
        return None
    nn = norm_name(substance_name)
    entries = verdict.get("вещества", [])
    for s in entries:  # точное совпадение после нормализации
        if norm_name(s.get("название", "")) == nn:
            return s
    for s in entries:  # формы записи и префиксы («аманитин»⊂«α-аманитин»)
        if match_strict(nn, norm_name(s.get("название", ""))):
            return s
    return None


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a | b) else 0.0


def stability_score(sets: list[set]) -> float:
    """Средний попарный Jaccard наборов веществ между прогонами (0–1)."""
    if len(sets) < 2:
        return 0.0
    vals = [jaccard(a, b) for a, b in combinations(sets, 2)]
    return sum(vals) / len(vals)


def run_label(session: dict, run_dir: Path) -> str:
    tag = session.get("model", "?")
    no = session.get("run")
    no = f", run-{no:02d}" if isinstance(no, int) else f", {run_dir.name}"
    think = session.get("think")
    think_s = f" · think: {think}" if think else ""
    return tag + no + think_s


def chip(label: str, value, extra: str = "") -> str:
    return (f'<div class="chip">{esc(label)}: <b>{esc(value)}</b>'
            f'{f"<br><small>{esc(extra)}</small>" if extra else ""}</div>')


def amt_html(value: str | None) -> str:
    """Доля вещества в клетке: «неизвестно» и синонимы → приглушённая
    подпись «доля не указана» (решение владельца — голое «неизвестно»
    не воспринимается)."""
    if is_unknown_amount(value):
        return '<b>доля не указана</b>'
    return f'<b>{esc(value or "—")}</b>'


def mushroom_block(qid: str, topic: str, ethalon_entry: dict,
                   runs_all: list[dict]) -> str:
    """Единая таблица «вещество × прогон» для одного гриба. Прогоны без
    этой клетки (добор контрольной группы) не рисуют пустых колонок —
    таблица только по прогонам, где гриб спрашивался."""
    runs = [r for r in runs_all
            if any(t["id"] == qid for t in r["session"]["turns"])]
    n_runs = len(runs)
    # клетки: [(run_idx, turn)]
    cells = []
    for ri, r in enumerate(runs):
        turn = next((t for t in r["session"]["turns"] if t["id"] == qid), None)
        cells.append((ri, turn))
    # имена всех веществ по прогонам
    per_run_items: list[list[dict]] = []
    for _ri, turn in cells:
        comp = (turn or {}).get("composition") or []
        per_run_items.append(comp)
    # объединение веществ: группы ТОЛЬКО по точному совпадению нормализованных
    # названий; канон группы — самая частая исходная форма
    groups: dict[str, dict] = {}
    for ri in range(len(cells)):
        for it in per_run_items[ri]:
            key = norm_name(it.get("вещество", ""))
            g = groups.setdefault(key, {"raws": {}, "cells": {}})
            g["raws"][it.get("вещество", "")] = \
                g["raws"].get(it.get("вещество", ""), 0) + 1
            if ri not in g["cells"]:
                g["cells"][ri] = it
    for g in groups.values():
        g["canonical"] = max(g["raws"], key=g["raws"].get)
    order = sorted(groups.values(),
                   key=lambda g: (-len(g["cells"]), g["canonical"].lower()))
    # дегенеративные ответы (сотни уникальных фантазий): таблицу режем до
    # осмысленных строк, метрики считаются по всем данным
    truncated = None
    if len(order) > 60:
        truncated = sum(len(g["cells"]) for g in order[50:])
        order = order[:50]

    rows = []
    for g in order:
        c = g["canonical"]
        tds = []
        for ri in range(len(cells)):
            item = g["cells"].get(ri)
            if item is None:
                tds.append('<td class="cell na"></td>')
                continue
            turn = cells[ri][1]
            verdict = verdict_for(
                (turn and runs[ri]["verdicts"].get(turn["id"])) or None,
                item.get("вещество", ""))
            status = (verdict or {}).get("статус")
            color = STATUS_COLOR.get(status, COLOR_NA)
            tip_parts = [f"<b>{esc(item.get('вещество', ''))}</b>",
                         esc(f"Содержание: {item.get('содержание', '—')}"),
                         esc(f"Влияние: {item.get('влияние', '—')}")]
            if verdict:
                tip_parts.append(
                    esc(f"Судья: {verdict.get('статус', '—')} · "
                        f"концентрация {verdict.get('концентрация', '—')} · "
                        f"влияние {verdict.get('влияние', '—')}"))
            else:
                tip_parts.append("Вердикта судьи нет")
            cls = "" if status in STATUS_COLOR else "na"
            tds.append(
                f'<td class="cell {cls}" style="background:{color}">'
                f'<div class="tip">{"".join(f"{p}<br>" for p in tip_parts)}</div>'
                f'<div class="inner">{amt_html(item.get("содержание"))}'
                f'<span>{esc((item.get("влияние") or "")[:90])}</span></div></td>')
        freq = f"{len(g['cells'])}/{n_runs}"
        rows.append(
            f'<tr><td class="name">{esc(c)}</td>{"".join(tds)}'
            f'<td class="freq">{freq}</td></tr>')
    if truncated:
        rows.append(
            f'<tr><td class="name">… ещё {truncated} наименований '
            f'(по одному разу — дегенеративный перебор)</td>'
            f'{"<td class=\"cell na\"></td>" * n_runs}'
            f'<td class="freq">—</td></tr>')

    # пропуски эталонных важных соединений
    eth_main = ethalon_entry.get("основные", [])
    miss_rows = []
    for e in eth_main:
        targets = ethalon_targets(e)
        marks = []
        for ri, turn in cells:
            comp = per_run_items[ri]
            found = any(match_ethalon(it.get("вещество", ""), targets)
                        for it in comp)
            marks.append('<td class="mark ok">✓</td>' if found
                         else '<td class="mark no">—</td>')
        miss_rows.append(f'<tr><td class="name">{esc(e["вещество"])}</td>'
                         f'{"".join(marks)}</tr>')

    # метрики гриба
    sets = [{norm_name(it.get("вещество", "")) for it in items
             if it.get("вещество")} for items in per_run_items]
    stab = stability_score(sets)
    # стабильность по веществам, признанным судьёй реальными: отличает
    # «знает ядро, добавляя мусор» от «не знает ядро вовсе»
    real_sets = []
    for ri in range(len(cells)):
        turn = cells[ri][1]
        vd = (turn and runs[ri]["verdicts"].get(turn["id"])) or None
        names = set()
        for it in per_run_items[ri]:
            s = verdict_for(vd, it.get("вещество", ""))
            if s and s.get("статус") == "реальное":
                names.add(norm_name(it.get("вещество", "")))
        real_sets.append(names)
    stab_real = stability_score(real_sets)
    listed = sum(len(items) for items in per_run_items)
    hal = part = real = 0
    scores = []
    for ri, turn in cells:
        vd = runs[ri]["verdicts"].get(turn["id"]) if turn else None
        if vd:
            scores.append(vd.get("балл"))
        for s in (vd or {}).get("вещества", []):
            st = s.get("статус")
            real += st == "реальное"
            part += st == "спорно"
            hal += st == "галлюцинация"
    judged = [s for s in scores if s is not None]
    score_s = (f"{statistics.mean(judged):.1f} / 10" if judged else "—")
    hal_pct = (f"{100 * hal / max(1, real + part + hal):.0f}%"
               if (real + part + hal) else "—")
    stab_pct = f"{100 * stab:.0f}%"
    stab_real_pct = f"{100 * stab_real:.0f}%"

    header_runs = "".join(
        f'<th class="runhd">run-{r["session"].get("run", ri + 1):02d}</th>'
        for ri, r in enumerate(runs))
    table = f"""
<div class="scroll"><table class="comp">
<tr><th class="namehd">Соединение</th>{header_runs}<th>частота</th></tr>
{"".join(rows)}
</table></div>"""
    miss_table = f"""
<div class="scroll"><table class="comp miss">
<tr><th class="namehd">Эталонные соединения (основные)</th>{header_runs}</tr>
{"".join(miss_rows)}
</table></div>"""
    return f"""
<div class="mush">
<h3>{with_latin(topic, qid)}</h3>
<div class="mini-chips">
  <div class="chip">Балл судьи: <b>{esc(score_s)}</b></div>
  <div class="chip">Стабильность состава: <b>{esc(stab_pct)}</b> <small>(Jaccard между прогонами)</small></div>
  <div class="chip">Стабильность реальных: <b>{esc(stab_real_pct)}</b> <small>(только признанные судьёй)</small></div>
  <div class="chip">Перечислено соединений: <b>{listed}</b></div>
  <div class="chip">Галлюцинации: <b>{esc(hal_pct)}</b> ({hal} из {real + part + hal} вердиктов)</div>
  <div class="chip">реальных <b>{real}</b> · спорных <b>{part}</b></div>
</div>
<div class="legend">
  <span><span class="sw" style="background:{COLOR_OK}"></span>реальное вещество</span>
  <span><span class="sw" style="background:{COLOR_AMBER}"></span>спорно</span>
  <span><span class="sw" style="background:{COLOR_BAD}"></span>галлюцинация</span>
  <span><span class="sw" style="background:{COLOR_NA}"></span>не перечислено в прогоне</span>
  <span class="hint">наведите — содержание, влияние и вердикт судьи</span>
</div>
{table}
<h4>Пропуски эталонных основных веществ</h4>
<div class="legend"><span class="hint">✓ — вещество названо в прогоне, — — нет</span></div>
{miss_table}
</div>"""


def appendix(runs: list[dict]) -> str:
    parts = []
    for r in runs:
        s = r["session"]
        for t in s.get("turns", []):
            raw = (t.get("answer_raw") or "").strip()
            parts.append(
                f'<details class="raw"><summary>{esc(t["topic"])} · '
                f'run-{s.get("run", "?"):02d} · {t.get("duration_s", 0)} c · '
                f'{t.get("n_substances", 0)} веществ</summary>'
                f'<pre>{esc(raw or "(пусто)")}</pre></details>')
    return "\n".join(parts)


def methodology_section(session: dict | None = None,
                        runs: list[dict] | None = None,
                        qdata: dict | None = None,
                        level: bool = True,
                        rel: str = "") -> str:
    """Раздел «Методика»: транспорт и параметры запроса к модели,
    system-промпт, пример клетки и ответа, судья и его промпт."""
    runs = runs or []
    s = session if session is not None else (runs[0]["session"] if runs else {})
    qdata = qdata or {}
    sysp = s.get("system_prompt") or qdata.get("system_prompt") or ""
    think = s.get("think", "off")
    model = s.get("model", "<модель>")
    base = s.get("base_url") or "OLLAMA_BASE_URL (из .env.local)"
    keep = s.get("keep_alive", "10m")
    first = next((t for t in s.get("turns", []) if t.get("prompt")), None)
    questions = qdata.get("questions") or []
    uprompt = ((first or {}).get("prompt")
               or (questions[0]["prompt"] if questions else "<вопрос клетки>"))
    qid = (first or {}).get("id",
                            questions[0]["id"] if questions else "?")
    qtopic = next((q["topic"] for q in questions if q["id"] == qid), qid)
    payload = json.dumps({
        "model": model,
        "stream": False,
        "keep_alive": keep,
        "think": False if think == "off" else think,
        "messages": [
            {"role": "system", "content": "<SYSTEM-ПРОМПТ — целиком ниже>"},
            {"role": "user", "content": uprompt},
        ]}, ensure_ascii=False, indent=1)
    raw = ((first or {}).get("answer_raw") or "").strip()
    if len(raw) > 700:
        raw = raw[:700] + "\n… (обрезано)"
    verdict_example = ""
    if runs:
        vd = runs[0]["verdicts"].get((first or {}).get("id") or "")
        if vd:
            verdict_example = json.dumps(vd, ensure_ascii=False, indent=1)
            if len(verdict_example) > 1000:
                verdict_example = verdict_example[:1000] + "\n… (обрезано)"
    try:
        from judge_mush import (JUDGE_SYSTEM, JUDGE_FORMAT, JUDGE_CAP,
                                JUDGE_MODEL, JUDGE_THINKING)
        judge_label = f"{JUDGE_MODEL}</code>, thinking <code>{JUDGE_THINKING}"
        judge_full = JUDGE_SYSTEM + "\n\n" + JUDGE_FORMAT
        cap_n = JUDGE_CAP
    except Exception:
        judge_label = "glm-5.3-flash</code>, thinking <code>low"
        judge_full = "(judge_mush.py недоступен)"
        cap_n = 100
    # ссылка на JSON-файл эталона языка отчёта (решение владельца)
    _lang = str((qdata or {}).get("lang", "ru")).lower()
    _eth_json = "ethalon-en.json" if _lang == "en" else "ethalon.json"
    eth_link = (f'<a class="report-link" target="_blank" rel="noopener" '
                f'href="{rel}{_eth_json}">{_eth_json}</a>')
    think_line = (f"уровень размышлений этого отчёта: <b>{esc(str(think))}</b>"
                  if level else
                  "уровни размышлений: off / low / medium / xhigh — по отчёту "
                  "на каждый уровень")
    fmt = s.get("format") or ""
    fmt_line = (f' Запрос содержит поле <code>format</code> — структурированный '
                f'вывод ollama: <code>{esc(fmt)}</code>. Декодирование ограничено '
                f'грамматикой по схеме — текст вне JSON и больше 100 элементов '
                f'невозможны на уровне декодера.' if fmt else "")
    return f"""
<h2>Методика</h2>
<div class="method">
<h3>Запуск модели</h3>
<p>Одна клетка = один независимый запрос к ollama
<code>POST {esc(base)}/api/chat</code>, stream=false. Истории между клетками
нет — клетки изолированы, каждый вопрос несёт system-рамку теста. Параметры
генерации — дефолтные ollama (temperature и прочее не переопределяются),
seed не фиксируется; уровень размышлений передаётся полем <code>think</code>
({think_line}).{fmt_line}</p>
<pre class="prompt-sample">{esc(payload)}</pre>
<ul>
<li>внутри прогона <code>keep_alive</code> = {esc(str(keep))} — веса держатся
загруженными;</li>
<li>модель выгружается перед первым и после каждого прогона
(<code>keep_alive: 0</code>) — изоляция прогонов, KV-кэш между сериями не
переживает;</li>
<li>серия: 5 прогонов на уровень размышлений; уровни не смешиваются;</li>
<li>ответ должен быть строго JSON; при усечённом/дегенеративном ответе парсер
достаёт целые фрагменты-объекты (пометка «ремонт», json_ok=false).</li>
</ul>
<h3>System-промпт (одинаков во всех запросах)</h3>
<pre class="prompt-sample">{esc(sysp)}</pre>
<h3>Пример user-сообщения — {esc(qtopic)}</h3>
<pre class="prompt-sample">{esc(uprompt)}</pre>
{(f'<details class="raw"><summary>Пример ответа модели на эту клетку</summary>'
  f'<pre>{esc(raw)}</pre></details>') if raw else ''}
<h3>Судья</h3>
<p>Каждый ответ сверяется судьёй — <code>{judge_label}</code>,
транспорт <code>pi</code> (провайдер zai). Судья получает эталонную справку
состава гриба — запись этого гриба из {eth_link}, целиком файл не
передаётся — и разобранный состав ответа; по каждому веществу
выносит вердикт «реальное / спорно / галлюцинация», оценивает концентрацию
(в норме / завышена / занижена) и влияние на человека, перечисляет пропущенные
эталонно важные вещества и ставит балл 0–10. Ответы длиннее {cap_n} веществ
(дегенеративный перебор) режутся до первых {cap_n} с пометкой. Вердикт строго
JSON: невалидная схема — одна повторная попытка, иначе клетка исключается из
метрик. Переиспользование вердиктов — по хэшам входов эталона: изменение
входа конкретного гриба переоценивает только его клетки.</p>
{(f'<details class="raw"><summary>Пример вердикта судьи</summary>'
  f'<pre>{esc(verdict_example)}</pre></details>') if verdict_example else ''}
<details class="raw"><summary>Полный промпт судьи (system + критерии)</summary>
<pre>{esc(judge_full)}</pre></details>
</div>"""


def build_level(model_dir: Path, think: str, ethalon: dict,
                questions_file: str | None = None,
                backlink_name: str = "report.html") -> None:
    runs = load_runs(model_dir, think)
    sessions = [r["session"] for r in runs]
    model = sessions[0].get("model", model_dir.name)
    n_runs = len(runs)
    qids = [t["id"] for t in sessions[0].get("turns", [])]

    # общие чипы уровня
    all_scores, all_h, all_n, json_ok_n, json_all = [], 0, 0, 0, 0
    dur = sum(s.get("totals", {}).get("duration_s") or 0 for s in sessions)
    tin = sum(s.get("totals", {}).get("prompt_tokens") or 0 for s in sessions)
    tout = sum(s.get("totals", {}).get("output_tokens") or 0 for s in sessions)
    by_q: dict[str, list[set]] = {}
    by_qr: dict[str, list[set]] = {}
    for r in runs:
        for t in r["session"].get("turns", []):
            json_all += 1
            json_ok_n += bool(t.get("json_ok"))
            vd = r["verdicts"].get(t["id"])
            if vd:
                all_scores.append(vd.get("балл"))
                for s in vd.get("вещества", []):
                    all_n += 1
                    all_h += s.get("статус") == "галлюцинация"
            names = {norm_name(it.get("вещество", ""))
                     for it in (t.get("composition") or [])} - {""}
            by_q.setdefault(t["id"], []).append(names)
            real_names = set()
            for it in (t.get("composition") or []):
                sv = verdict_for(r["verdicts"].get(t["id"]),
                                 it.get("вещество", ""))
                if sv and sv.get("статус") == "реальное":
                    real_names.add(norm_name(it.get("вещество", "")))
            by_qr.setdefault(t["id"], []).append(real_names)
    # средний попарный Jaccard по клеткам одного гриба между прогонами
    pairs = [stability_score(qs) for qs in by_q.values() if len(qs) >= 2]
    mean_stab = statistics.mean(pairs) if pairs else 0.0
    pairs_real = [stability_score(qs) for qs in by_qr.values() if len(qs) >= 2]
    mean_stab_real = statistics.mean(pairs_real) if pairs_real else 0.0

    score_s = f"{statistics.mean(all_scores):.1f} / 10" if all_scores else "—"
    hal_s = f"{all_h} / {all_n}" if all_n else "—"
    json_s = f"{100 * json_ok_n / max(1, json_all):.0f}%"

    qdata = json.loads(
        (Path(questions_file) if questions_file
         else SCRIPT_DIR / "questions-composition.json").read_text(
            encoding="utf-8"))
    blocks = []
    for q in qdata["questions"]:
        qid, topic = q["id"], q["topic"]
        eth = ethalon["mushrooms"].get(qid, {})
        blocks.append(mushroom_block(qid, topic, eth, runs))

    title = (f"Состав мухоморов — {model}, think {think}: "
             f"{n_runs} прогонов")
    protocol = ("клетка = вопрос «перечисли основные и характерные соединения "
                "гриба» (recall-генерация, промпты v4; один прогон = все 8 "
                "клеток); архивы серий v1–v3 с другими промптами в отчётах не "
                "участвуют. Цвет клетки = вердикт судьи (glm-5.3-flash low "
                "против эталона), серая = вещество не перечислено в этом "
                "прогоне; стабильность = средний Jaccard наборов веществ между "
                "прогонами")
    chips = "".join([
        chip("Модель", model, f"think {think} · прогонов: {n_runs} "
             f"(в таблице гриба — где его клетка спрашивалась)"),
        chip("Балл судьи (средний)", score_s, "по оценённым клеткам"),
        chip("Галлюцинации", hal_s, "доля вердиктов «галлюцинация»"),
        chip("Стабильность состава", f"{100 * mean_stab:.0f}%",
             "средний попарный Jaccard веществ между прогонами"),
        chip("Стабильность реальных", f"{100 * mean_stab_real:.0f}%",
             "Jaccard по веществам, признанным судьёй"),
        chip("JSON-разбор", json_s, f"({json_ok_n} из {json_all} клеток)"),
        chip("Время", f"{dur:.0f} c", f"токены: {tin} вх / {tout} вых"),
        chip("Эталон", f"v{ethalon.get('version')}",
             "судья видит справку целиком"),
    ])
    rel_prefix = "../" * len(
        model_dir.resolve().relative_to(SCRIPT_DIR).parts)
    body = (methodology_section(sessions[0], runs, qdata, rel=rel_prefix)
            + "".join(blocks)
            + "<h2>Ответы моделей (raw)</h2>" + appendix(runs))
    footer = (f"построено build_mush_report.py · "
              f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
              f"данные: run-*/session.json, run-*/verdicts.json")
    out = model_dir / f"report-{think}.html"
    out.write_text(page(title, protocol, chips, body, footer,
                        backlink=f"../../../{backlink_name}"),
                   encoding="utf-8")
    print(f"отчёт: {out}")


def page(title: str, protocol: str, chips: str, body: str, footer: str,
         backlink: str = "") -> str:
    css = """
    :root { --fg:#1c2130; --mut:#6b7280; --line:#e3e6ea; }
    * { box-sizing: border-box; }
    body { font: 15px/1.5 system-ui, sans-serif; color: var(--fg);
           background: #f6f7f9; margin: 0; padding: 28px 18px 60px; }
    main { max-width: 1280px; margin: 0 auto; }
    h1 { font-size: 24px; margin: 0 0 4px; }
    h2 { font-size: 19px; margin: 34px 0 10px; }
    h3 { font-size: 17px; margin: 26px 0 6px; }
    h4 { font-size: 14px; margin: 16px 0 6px; color: var(--mut); }
    .proto { color: var(--mut); margin: 0 0 14px; }
    .chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 6px; }
    .chip { background: #fff; border: 1px solid var(--line); border-radius: 9px;
            padding: 7px 12px; font-size: 14px; }
    .chip b { font-size: 15px; }
    .chip small { color: var(--mut); }
    .mini-chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 8px; }
    .mini-chips .chip { padding: 4px 10px; font-size: 12.5px; }
    .mini-chips .chip b { font-size: 13px; }
    .legend { display: flex; gap: 14px; align-items: center; margin: 8px 0;
              font-size: 13px; flex-wrap: wrap; }
    .legend .hint { color: var(--mut); font-size: 12px; }
    .sw { display: inline-block; width: 13px; height: 13px; border-radius: 4px;
          margin-right: 5px; vertical-align: -2px; }
    table.comp { border-collapse: separate; border-spacing: 3px; width: 100%;
                 background: transparent; margin: 6px 0 10px; }
    table.comp th { font-size: 12px; color: var(--mut); font-weight: 600;
                    padding: 2px 6px; text-align: left; }
    table.comp th.runhd { text-align: center; }
    td.cell { height: 58px; min-width: 130px; border-radius: 7px; padding: 6px 8px;
              position: relative; color: rgba(20,24,32,.85); vertical-align: middle; }
    td.cell.na { background: #a9b0ba; }
    td.cell .inner { display: flex; flex-direction: column; gap: 2px;
                     overflow: hidden; }
    td.cell .inner b { font-size: 12.5px; white-space: nowrap;
                       overflow: hidden; text-overflow: ellipsis; }
    td.cell .inner span { font-size: 11px; opacity: .8; line-height: 1.3;
                          display: -webkit-box; -webkit-line-clamp: 2;
                          -webkit-box-orient: vertical; overflow: hidden; }
    /* подсказку позиционирует скрипт (position: fixed) относительно окна:
       внутри .scroll и у верхних строк она обрезалась */
    td.cell .tip { display: none; position: fixed; width: 330px; z-index: 30;
                   background: #202634; color: #eef1f6; padding: 9px 11px;
                   border-radius: 9px; font-size: 12px; line-height: 1.45;
                   text-align: left; white-space: normal;
                   box-shadow: 0 6px 18px rgba(0,0,0,.35); pointer-events: none; }
    td.cell:hover .tip { display: block; }
    td.cell:hover { outline: 2px solid #1c2130; }
    td.name { font-size: 13px; padding: 2px 8px 2px 2px; max-width: 190px; }
    td.freq { text-align: center; font-weight: 700; font-size: 12.5px;
              color: var(--mut); }
    td.mark { text-align: center; font-size: 14px; background: #fff;
              border-radius: 6px; padding: 3px; }
    td.mark.ok { color: #22834a; }
    td.mark.no { color: #c9ccd2; }
    table.miss td.cell, table.miss td.mark { height: 30px; }
    code.id { font-size: 12px; background: #eef1f4; border-radius: 5px;
              padding: 1px 6px; color: var(--mut); }
    .mush { background: #fff; border: 1px solid var(--line); border-radius: 12px;
            padding: 14px 16px 10px; margin: 14px 0 22px; }
    details.raw { background: #fff; border: 1px solid var(--line);
                  border-radius: 8px; padding: 8px 12px; margin: 6px 0; }
    details.raw summary { cursor: pointer; color: var(--mut); font-size: 13px;
                          user-select: none; }
    details.raw pre { background: #f4f6f8; border-radius: 6px; padding: 8px;
                      font: 12px/1.45 ui-monospace, Menlo, monospace;
                      white-space: pre-wrap; word-break: break-word; max-height: 420px;
                      overflow: auto; }
    /* border-collapse: separate + без overflow:hidden — иначе position:
       sticky на шапке не работает в Chromium */
    table.sx { border-collapse: separate; border-spacing: 0;
               background: #fff; width: 100%;
               border: 1px solid var(--line); border-radius: 12px; }
    table.sx th:first-child { border-top-left-radius: 12px; }
    table.sx th:last-child { border-top-right-radius: 12px; }
    table.sx th, table.sx td { border-bottom: 1px solid var(--line);
                 padding: 8px 12px; text-align: left; font-size: 14px; }
    table.sx th { position: sticky; top: 0; background: #fff; z-index: 2; }
    table.sx th { background: #f0f2f5; font-size: 13px; color: var(--mut); }
    a.report-link { color: #315f9e; font-weight: 600; text-decoration: none; }
    a.report-link:hover { text-decoration: underline; }
    .ok-t { color: #22834a; font-weight: 600; }
    .bad-t { color: #b03a3a; font-weight: 600; }
    .warn-t { color: #8a6d1a; font-weight: 600; }
    footer { color: var(--mut); font-size: 12.5px; margin-top: 40px; }
    .backlink { display: inline-block; background: #edf1f7; color: #315f9e;
                font-size: 13.5px; font-weight: 600; padding: 6px 14px;
                border-radius: 8px; text-decoration: none; margin-bottom: 14px; }
    .backlink:hover { background: #dfe7f3; }
    .scroll { overflow-x: auto; }
    .latin { font-weight: 400; font-style: italic; color: var(--mut);
             font-size: 0.82em; }
    td.mx { text-align: center; font-weight: 700;
            font-size: 10.5px; color: rgba(20,24,32,.82); padding: 3px 1px;
            border-radius: 4px; }
    /* матрица всегда ровно в карточку: фиксированный лейаут делит ширину
       поровну между сериями при любом их числе (21+ колонок) */
    table.mx-tab { table-layout: fixed; width: 100%; min-width: 900px; }
    table.mx-tab th.namehd, table.mx-tab td.name { width: 168px;
        word-break: break-word; }
    table.mx-tab th.mx-model { text-align: center; font-size: 10px;
                               line-height: 1.2; padding: 3px 2px;
                               overflow: hidden;
                               border-left: 2px solid var(--line); }
    table.mx-tab td.mx.grp { border-left: 2px solid var(--line); }
    table.mx-tab th.mx-model:first-child { border-left: none; }
    table.mx-tab th.mx-think { padding: 2px 3px; font-size: 9.5px;
                               text-align: center; white-space: nowrap;
                               font-weight: 400; color: var(--mut);
                               border-left: 1px solid #cfd4db; }
    td.mx.na { background: #e3e6ea; }
    tr.mushhdr th { text-align: left; background: #eef1f4; font-size: 13.5px;
                    padding: 7px 10px; color: var(--fg); }
    tr.subhdr th { text-align: left; background: #f7f8fa; font-size: 12px;
                   padding: 4px 10px; color: var(--mut); font-weight: 600; }
    td.mx-miss { background: #e3e6ea; color: #8a8f98; font-weight: 400; }
    td.fant-rest { font-size: 12px; font-weight: 400; color: #5a6069;
                   line-height: 1.6; padding: 8px 10px; }
    td.fant-total { text-align: center; font-size: 12px; font-weight: 700;
                    color: #5a6069; background: #f1f3f6;
                    border-left: 1px solid #e3e6ea; }
    td.fant-total.name-c, td.name.fant-total { text-align: left;
                    font-weight: 600; font-size: 12px; font-style: italic; }
    .eth { background: #fff; border: 1px solid var(--line);
           border-radius: 12px; padding: 14px 18px 6px; margin: 18px 0; }
    .eth .intro { font-size: 13.5px; color: var(--mut); margin: 0 0 12px; }
    .eth h3 { font-size: 15.5px; margin: 22px 0 4px; }
    .eth .note { color: var(--mut); font-size: 13px; margin: 4px 0 8px; }
    table.eth-t td { font-size: 13px; vertical-align: top; }
    table.eth-t td.name-c { width: 20%; font-weight: 600; }
    table.eth-t td.eff i { color: var(--mut); }
    table.eth-t td.cont { width: 28%; }
    table.eth-t td.eff { color: var(--mut); }
    table.eth-t td, table.eth-t th { padding: 7px 10px; }
    tr.grouphd td { background: #f0f2f5; font-size: 12px; font-weight: 600;
                    color: var(--mut); padding: 4px 10px; }
    tr.classhd td { background: #fbfcfe; font-size: 11px; color: #6b7280;
                    padding: 3px 14px; font-style: italic; }
    .badge { display: inline-block; background: #e7f2e9; color: #22834a;
             font-size: 11px; font-weight: 600; padding: 1px 8px;
             border-radius: 8px; vertical-align: 2px; margin-left: 6px; }
    .conf { display: inline-block; font-size: 11px; padding: 1px 8px;
            border-radius: 8px; white-space: nowrap; }
    .conf-hi { background: #e7f2e9; color: #22834a; }
    .conf-mid { background: #eef1f4; color: #6b7280; }
    .conf-lo { background: #fdf3e0; color: #8a6d1a; }
    .conf-dis { background: #fbeaea; color: #b03a3a; }
    .notpres { font-size: 13px; background: #fdf6f6; border: 1px solid #f0d9d9;
               border-radius: 8px; padding: 6px 10px; color: #7a4a4a;
               margin: 4px 0 8px; }
    td.src-c { font-size: 11.5px; color: var(--mut); }
    .srclink { color: #315f9e; text-decoration: none;
               border-bottom: 1px dotted #9ab2d4; }
    .srclink:hover { color: #1c4587; border-bottom: 1px solid #315f9e; }
    .mh { display: block; font-size: 11px; font-weight: 700; color: var(--fg); }
    .mt { display: block; font-size: 10.5px; font-weight: 400; color: var(--mut); }
    td.mk { text-align: center; font-size: 11.5px; white-space: nowrap; }
    .mk-main { color: #22834a; font-weight: 700; }
    .mk-trace { color: #64778a; }
    .mk-disp { color: #8a6d1a; font-weight: 600; }
    .mk-none { color: #b6bbc2; }
    .method { background: #fff; border: 1px solid var(--line);
              border-radius: 12px; padding: 14px 18px; margin: 18px 0; }
    .method h3 { font-size: 14.5px; margin: 16px 0 6px; }
    .method h3:first-child { margin-top: 0; }
    .method p, .method li { font-size: 13.5px; }
    .method ul { margin: 6px 0; padding-left: 20px; }
    .method li { margin: 3px 0; }
    .method code { background: #eef1f4; border-radius: 4px; padding: 0 5px;
                   font-size: 12.5px; }
    .prompt-sample { background: #f4f6f8; border: 1px solid var(--line);
                     border-radius: 8px; padding: 10px 12px;
                     font: 12.5px/1.5 ui-monospace, Menlo, monospace;
                     white-space: pre-wrap; margin: 6px 0 10px;
                     word-break: break-word; }
    """
    return f"""<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{css}</style>
<main>
{f'<a class="backlink" href="{backlink}">← Сводный отчёт</a>' if backlink else ''}
<h1>{esc(title)}</h1>
<p class="proto">{esc(protocol)}</p>
{chips}
{body}
<footer>{esc(footer)}</footer>
</main>
{_TIP_JS}
</body>
</html>
"""


def score_cell_color(score: float | None) -> str:
    if score is None:
        return "na-t"
    if score >= 8:
        return "ok-t"
    if score >= 6:
        return "warn-t"
    return "bad-t"


# Латинские биномы клеток — для заголовков рядом с бытовым названием
# (решение владельца: «Мухомор красный (Amanita muscaria)»). Одинаков
# для RU- и EN-серий.
LATIN = {
    "AMUS": "Amanita muscaria",
    "APAN": "Amanita pantherina",
    "APHA": "Amanita phalloides",
    "ACIT": "Amanita citrina",
    "ARUB": "Amanita rubescens",
    "ACAE": "Amanita caesarea",
    "AVIO": "Amanita virosa",
    "AMEL": "Armillaria mellea",
}


def with_latin(topic: str, qid: str) -> str:
    lat = LATIN.get(qid)
    return (f'{esc(topic)} <span class="latin">({esc(lat)})</span>'
            if lat else esc(topic))


MODEL_SHORT = {
    "orcarouter/Qwen3.8-27B-Uncensored:latest": "orca",
    "huihui-ai:pi-100k": "huihui",
    "deepseek-flash": "deepseek",
    "google/gemini-4-neon": "gem-4-neon",
    "openai/gpt-6.2-sol": "sol 6.2",
    "mimo-v2.6-pro": "mimo 2.6",
    "kimi-k3.1-pro": "kimi 3.1",
    "k3.1-pro": "kimi 3.1",
    "medgemma:27b": "medg 27b",
    "qwen4-max": "qw4-max",
    "qwen4-max-preview": "qw4-max·p",
}


def model_short(model: str) -> str:
    """Короткая метка модели для шапки матрицы: без провайдера и шумных
    тегов (latest/ctx100k/pi-100k), с размером если он различает модели
    (gemma4 26b vs gemma4 31b). Явные исключения — MODEL_SHORT."""
    if model in MODEL_SHORT:
        return MODEL_SHORT[model]
    base, _, tag = model.partition(":")
    base = base.split("/")[-1]
    if tag in ("", "latest", "ctx100k", "pi-100k"):
        return base
    return f"{base} {tag.split('-')[0]}"


def cell_status_color(st: dict) -> str:
    """Устарело: раньше красил клетки матрицы вердиктами судьи. С решением
    владельца подсветка матрицы — по числу повторов (см. substance_matrix),
    причастность к эталону — положением строки."""
    r = st.get("реальное", 0)
    h = st.get("галлюцинация", 0)
    if r and not h:
        return COLOR_OK
    if h and not r:
        return COLOR_BAD
    if r or h:
        return COLOR_AMBER
    return "#c9ccd2"


def substance_matrix(series: list[dict], ethalon: dict,
                     qdata: dict) -> str:
    """Кросс-модельная матрица «соединение × серия (модель × think)»,
    построенная ОТ ЭТАЛОНА: первый блок каждого гриба — эталонный
    (реальный) состав и найдено ли он моделями; ниже — фантазии моделей
    (нет в эталоне) по частоте упоминаний. Клетка = «названо прогонов /
    всего прогонов серии», цвет = стабильность повторов (зелёный ≥80%,
    янтарный частично, красный однократно, серый 0/n); «фантазии» —
    градации серого по тем же повторам (решение владельца); принадлежность
    эталону — положением строки (эталонный блок или фантазии)."""
    from collections import Counter
    # по серии: acc[qid][норм-имя] = {runs, st, raws}
    # runs — МНОЖЕСТВО номеров прогонов, где имя встретилось: клетка
    # показывает «в скольких прогонах названо», а не число упоминаний
    # (иначе «Аминокислоты (Треонин)», «(Серин)»… схлопываются нормализацией
    # в один ключ и дают k=83 при n=5)
    per_series = []
    for s in series:
        acc: dict[str, dict[str, dict]] = {}
        n_by_q: dict[str, int] = {}
        for r in s["runs"]:
            run_no = r["session"].get("run")
            for t in r["session"]["turns"]:
                qid = t["id"]
                n_by_q[qid] = n_by_q.get(qid, 0) + 1
                vd = r["verdicts"].get(qid)
                for it in t.get("composition") or []:
                    nm = norm_name(it.get("вещество", ""))
                    if not nm:
                        continue
                    a = (acc.setdefault(qid, {})
                         .setdefault(nm, {"runs": set(), "st": Counter(),
                                          "raws": Counter()}))
                    a["runs"].add(run_no)
                    a["raws"][it.get("вещество", "")] += 1
                    sv = verdict_for(vd, it.get("вещество", ""))
                    if sv:
                        a["st"][sv.get("статус")] += 1
        for qid in acc:
            for a in acc[qid].values():
                a["k"] = len(a["runs"])
                a["disp"] = a["raws"].most_common(1)[0][0]
        per_series.append((s, acc, n_by_q))

    n_cols = len(series)
    # индексы колонок, с которых начинается новая группа моделей —
    # для вертикальных разделителей групп во всех строках таблицы
    group_start = set()
    for _i, s in enumerate(series):
        if _i == 0 or model_short(str(s["model"])) != \
                model_short(str(series[_i - 1]["model"])):
            group_start.add(_i)
    # в шапке уровни сокращены (med) и идут по возрастанию «усилия»
    think_short = {"medium": "med"}

    # подсветка фантазий (решение владельца 22.11): внутри каждого гриба
    # колонки шкалируются от минимума уникальных фантазий (зелёный) до
    # максимума (красный) — видно, как качество распределено по моделям;
    # теми же цветами красится строка «уникальных фантазий» и шапка гриба
    def _heat_style(c: float, lo: float, hi: float) -> str:
        if hi <= lo:
            return ""
        t = (c - lo) / (hi - lo)
        ca, cb = ((0x3a, 0xa2, 0x5c), (0xe8, 0xb6, 0x4c)) if t <= .5 \
            else ((0xe8, 0xb6, 0x4c), (0xd7, 0x5f, 0x5f))
        u = t * 2 if t <= .5 else t * 2 - 1
        rgb = tuple(round(ca[i] + (cb[i] - ca[i]) * u) for i in range(3))
        return f"background:#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

    def subhdr(text: str) -> str:
        return (f'<tr class="subhdr"><th colspan="{n_cols + 1}">'
                f'{esc(text)}</th></tr>')

    body_rows = []
    for q in qdata["questions"]:
        qid, topic = q["id"], q["topic"]
        eth = ethalon["mushrooms"].get(qid) or {}
        union: set[str] = set()
        for _s, acc, _n in per_series:
            union |= set(acc.get(qid, {}))
        if not union and not eth:
            continue
        body_rows.append(
            f'<tr class="mushhdr"><th colspan="{n_cols + 1}">'
            f'{with_latin(topic, qid)}</th></tr>')

        def cell_for(k: int, n: int, tip: str, gray: bool = False,
                     first_of_group: bool = False) -> str:
            # Подсветка = стабильность повторов (решение владельца), а не
            # вердикт судьи: причастность к эталону показывает ПОЛОЖЕНИЕ
            # строки — эталонный блок («правильный ответ») или фантазии.
            # В «фантазиях» цвета не нужны — градации серого по повторам
            # (решение владельца): цвет там читался как оценка, а она уже
            # дана положением строки.
            if k == 0:
                return (f'<td class="mx mx-miss" title="не названо">0/{n}</td>')
            r = k / n if n else 0.0
            if gray:
                if r >= 0.8:
                    color, tcol = "#7d838c", "#fff"
                elif r >= 0.4:
                    color, tcol = "#a9aeb6", "#fff"
                else:
                    color, tcol = "#d4d8dd", "#2b2f33"
            else:
                color = (COLOR_OK if r >= 0.8
                         else COLOR_AMBER if r >= 0.4 else COLOR_BAD)
                tcol = ""
            style = f"background:{color}" + (f";color:{tcol}" if tcol else "")
            cls = "mx" + (" grp" if first_of_group else "")
            return (f'<td class="{cls}" style="{style}" '
                    f'title="{esc(tip)}">{k}/{n}</td>')

        matched: set[str] = set()
        real_rows: list[str] = []
        # --- блок 1: эталонный (реальный) состав ---
        groups = (("эталон · основные", "основные"),
                  ("эталон · следовые", "следовые"),
                  ("эталон · спорные", "спорные"))
        for sub, key in groups:
            entries = eth.get(key) or []
            if not entries:
                continue
            real_rows.append(subhdr(sub))
            for e in entries:
                targets = [norm_name(t) for t in ethalon_targets(e)]
                tds = []
                k_total = 0
                for si, (s, acc, n_by_q) in enumerate(per_series):
                    qacc = acc.get(qid, {})
                    n = n_by_q.get(qid, 0)
                    runs_hit: set = set()
                    st = Counter()
                    for nm, a in qacc.items():
                        if any(match_strict(nm, t) for t in targets):
                            matched.add(nm)
                            # объединяем множества прогонов: если модель
                            # назвала и «α-аманитин», и «аманитины» в одном
                            # прогоне, это один прогон, а не два упоминания
                            runs_hit |= a["runs"]
                            st += a["st"]
                    k = len(runs_hit)
                    k_total += k
                    tip = (f"{e['вещество']}: найдено в {k}/{n} прогонах · "
                           f"судья: реальных {st.get('реальное', 0)}, "
                           f"спорных {st.get('спорно', 0)}, галлюцинаций "
                           f"{st.get('галлюцинация', 0)}")
                    tds.append(cell_for(k, n, tip,
                                        first_of_group=si in group_start))
                real_rows.append(
                    f'<tr><td class="name">{esc(e["вещество"])}</td>'
                    f'{"".join(tds)}</tr>')

        # число уникальных фантазий по колонкам — им шкалируются и строка
        # «уникальных фантазий», и шапка гриба (решение владельца 22.11)
        fant_counts = [sum(1 for nm in (acc.get(qid) or {}) if nm not in matched)
                       for _s, acc, _n in per_series]
        lo, hi = min(fant_counts), max(fant_counts)
        # двухъярусная шапка (решение владельца): верх — модель на всю её
        # группу уровней (colspan, цвет по среднему её уровней), низ —
        # уровни размышлений (цветом своей колонки); повторяется под
        # заголовком КАЖДОГО гриба с подсветкой по фантазиям этого гриба
        models_row, thinks_row = [], []
        i = 0
        while i < n_cols:
            full = str(series[i]["model"])
            m = model_short(full)
            span = 1
            while i + span < n_cols and \
                    model_short(str(series[i + span]["model"])) == m:
                span += 1
            stl = _heat_style(sum(fant_counts[i:i + span]) / span, lo, hi)
            stl = f' style="{stl}"' if stl else ''
            models_row.append(f'<th class="mx-model" colspan="{span}"{stl} '
                              f'title="{esc(full)}">{esc(m)}</th>')
            i += span
        for si, s in enumerate(series):
            lv = think_short.get(s["think"], s["think"])
            stl = _heat_style(fant_counts[si], lo, hi)
            stl = f' style="{stl}"' if stl else ''
            thinks_row.append(f'<th class="mx-think"{stl}>{esc(lv)}</th>')
        body_rows.append('<tr><th class="namehd" rowspan="2">Соединения</th>'
                         + "".join(models_row) + '</tr><tr>'
                         + "".join(thinks_row) + '</tr>')
        body_rows.extend(real_rows)

        # --- блок 2: фантазии (нет в эталоне) по частоте ---
        # строку сюда определяет строгий матчик (нет совпадения с эталоном);
        # подсветка клеток — по повторам, не по вердиктам судьи
        fant = []
        for nm in union - matched:
            k_total = 0
            st_total: Counter = Counter()
            raws: Counter = Counter()
            for _s, acc, _n in per_series:
                a = acc.get(qid, {}).get(nm)
                if a:
                    k_total += a["k"]
                    st_total += a["st"]
                    raws += a["raws"]
            disp = raws.most_common(1)[0][0]
            mentions = sum(raws.values())
            fant.append((-k_total, disp.lower(), nm, disp, k_total,
                         st_total, mentions))
        fant.sort()
        if fant:
            body_rows.append(subhdr("фантазии (нет в эталоне) · по частоте"))
            # первая строка блока: сколько уникальных фантазий в колонке
            # (решение владельца) — масштаб фантазийания каждой серии
            total_tds = []
            for si, (s, acc, n_by_q) in enumerate(per_series):
                stl = _heat_style(fant_counts[si], lo, hi)
                stl = f' style="{stl}"' if stl else ''
                total_tds.append(
                    f'<td class="fant-total"{stl} '
                    f'title="уникальных фантазий (вне эталона) в этой серии">'
                    f'{fant_counts[si]}</td>')
            body_rows.append(
                f'<tr><td class="fant-total name-c">уникальных фантазий</td>'
                f'{"".join(total_tds)}</tr>')
            shown = fant[:15]
            for _neg, _d, nm, disp, k_total, st_total, _m in shown:
                tds = []
                for si, (s, acc, n_by_q) in enumerate(per_series):
                    a = acc.get(qid, {}).get(nm)
                    n = n_by_q.get(qid, 0)
                    if not a:
                        tds.append('<td class="mx na"></td>')
                        continue
                    tip = (f"{disp}: названо в {a['k']}/{n} прогонах · "
                           f"упоминаний: {sum(a['raws'].values())} · "
                           f"судья: реальных {a['st'].get('реальное', 0)}, "
                           f"спорных {a['st'].get('спорно', 0)}, "
                           f"галлюцинаций {a['st'].get('галлюцинация', 0)}")
                    tds.append(cell_for(a["k"], n, tip, gray=True,
                                        first_of_group=si in group_start))
                body_rows.append(
                    f'<tr><td class="name">{esc(disp)}</td>'
                    f'{"".join(tds)}</tr>')
            if len(fant) > 15:
                rest = fant[15:]
                listing = ", ".join(
                    f"{esc(d)} ({m})"
                    for _n1, _n2, _nm, d, _k, _st, m
                    in sorted(rest, key=lambda x: x[3].lower()))
                body_rows.append(
                    f'<tr><td class="fant-rest" colspan="{n_cols + 1}">'
                    f'… ещё {len(rest)} фантазий: {listing}</td></tr>')

    legend = f"""
<div class="legend">
  <span><span class="sw" style="background:{COLOR_OK}"></span>повторяется почти в каждом прогоне (≥80%)</span>
  <span><span class="sw" style="background:{COLOR_AMBER}"></span>повторяется частично</span>
  <span><span class="sw" style="background:{COLOR_BAD}"></span>названо однократно (нестабильно)</span>
  <span><span class="sw" style="background:#e3e6ea"></span>не названо (0/n)</span>
  <span><span class="sw" style="background:#7d838c"></span>фантазии: стабильные</span>
  <span><span class="sw" style="background:#a9aeb6"></span>средние</span>
  <span><span class="sw" style="background:#d4d8dd"></span>редкие</span>
  <span><span class="sw" style="background:linear-gradient(90deg,#3aa25c,#e8b64c,#d75f5f)"></span>строка «уникальных фантазий» и шапка: зелёный — меньше выдумок, красный — больше (шкала внутри каждого гриба)</span>
  <span class="hint">эталонный блок — цвет по стабильности повторов;
  «фантазии» — градации серого (темнее = повторяется чаще); принадлежность
  эталону определяется положением строки: первый блок каждого гриба — эталон
  («правильный ответ»), ниже — фантазии; в подсказке клетки — разбивка
  вердиктов судьи</span>
</div>"""
    return (legend
            # ширины колонок через colgroup: fixed-layout берёт их из
            # первой строки, а ею теперь является заголовок гриба (colspan
            # на всё) — без colgroup колонка имён схлопывается в буквы
            + f'<div class="scroll"><table class="comp mx-tab">'
            + f'<colgroup><col style="width:168px">'
            + '<col>' * n_cols + '</colgroup>'
            + "".join(body_rows) + "</table></div>")


CONF_CLASS = {"высокая": "conf-hi", "средняя": "conf-mid",
               "низкая": "conf-lo", "спорная": "conf-dis"}


SCHOLAR = "https://scholar.google.com/scholar?q="
SOURCE_LINKS = [
    ("Michelot & Melendez-Howell 2003",
     "https://davidmoore.org.uk/21st_Century_Guidebook_to_Fungi_PLATINUM/"
     "REPRINT_collection/Michelot_etal_A.muscaria_chemistry_biology_"
     "toxicology.pdf"),
    ("Michelot 2003",
     "https://davidmoore.org.uk/21st_Century_Guidebook_to_Fungi_PLATINUM/"
     "REPRINT_collection/Michelot_etal_A.muscaria_chemistry_biology_"
     "toxicology.pdf"),
    ("MDPI 2021", "https://www.mdpi.com/2673-8392/1/3/69"),
    ("Wieland 1983/1986", SCHOLAR + "Wieland+toxic+peptides+from+Amanita+mushrooms+1983"),
    ("Musso 1979", SCHOLAR + "Musso+pigments+of+fly+agaric+Amanita+muscaria"),
    ("Dörfer 2019", SCHOLAR + "Dorfer+Gressler+Hoffmeister+diversity+bioactivity+Armillaria+sesquiterpene+aryl+ester"),
    ("Pfütze 2024", SCHOLAR + "Pfutze+depicting+chemical+diversity+bioactive+melleolides+Armillaria+ostoyae"),
    ("Barceloux 2008", SCHOLAR + "Barceloux+Medical+Toxicology+of+Natural+Substances"),
    ("Savickaitė 2025", SCHOLAR + "Savickaite+Amanita+muscaria+alkaloids"),
    ("Voynova", SCHOLAR + "Voynova+Toxicological+and+Pharmacological+Profile+of+Amanita+muscaria+Pharmacia"),
    ("en-wiki Amatoxin", "https://en.wikipedia.org/wiki/Amatoxin"),
    ("en-wiki Amanita phalloides", "https://en.wikipedia.org/wiki/Amanita_phalloides"),
    ("en-wiki Amanita virosa", "https://en.wikipedia.org/wiki/Amanita_virosa"),
    ("en-wiki Amanita muscaria", "https://en.wikipedia.org/wiki/Amanita_muscaria"),
    ("en-wiki Amanita pantherina", "https://en.wikipedia.org/wiki/Amanita_pantherina"),
    ("en-wiki Amanita citrina", "https://en.wikipedia.org/wiki/Amanita_citrina"),
    ("en-wiki Amanita rubescens", "https://en.wikipedia.org/wiki/Amanita_rubescens"),
    ("en-wiki Amanita caesarea", "https://en.wikipedia.org/wiki/Amanita_caesarea"),
    ("en-wiki Armillaria mellea", "https://en.wikipedia.org/wiki/Armillaria_mellea"),
]


def src_links_html(text: str) -> str:
    """Источники в тексте → кликабельные ссылки: прямые URL для того, что
    проверялось напрямую (MDPI, PDF Michelot, вики-статьи), поисковые
    Google Scholar — для остальных (без выдуманных DOI)."""
    t = esc(text)
    for needle, url in SOURCE_LINKS:
        n = esc(needle)
        if n in t:
            t = t.replace(n, f'<a class="srclink" href="{esc(url)}" '
                             f'target="_blank" rel="noopener">{n}</a>')
    return t


CONF_EN = {"высокая": ("high", "conf-hi"), "средняя": ("medium", "conf-mid"),
           "низкая": ("low", "conf-lo"), "спорная": ("disputed", "conf-dis")}
CONF_CLASS = {"высокая": "conf-hi", "средняя": "conf-mid", "низкая": "conf-lo",
              "спорная": "conf-dis", "high": "conf-hi", "medium": "conf-mid",
              "low": "conf-lo", "disputed": "conf-dis"}
GROUP_EN = {"основные": "main", "следовые": "trace", "спорные": "disputed"}
TITLES_EN = {
    "h2": "Reference compositions and sources",
    "intro": ("The reference is the ground-truth composition the judge "
              "(glm-5.3-flash, thinking low) compares every model answer "
              "against. Assembled from peer-reviewed literature (Michelot & "
              "Melendez-Howell 2003, Wieland 1983/1986, Carboué & Lopez 2021) "
              "and wiki articles traced to primary sources; weakly supported "
              "items are marked disputed and penalised more softly. Groups: "
              "<b>main</b> — counted as omissions, <b>trace</b> — completeness "
              "credit, <b>disputed</b> — presence contested by literature."),
    "sources": "Reference sources",
    "src_col": "Source",
    "taken": "What it contributed",
    "not_present": "Confirmed absent",
    "t_substance": "Compound",
    "t_content": "Content",
    "t_effect": "Brief description and effect on humans",
    "t_source": "Source",
    "t_conf": "Confidence",
    "control": "control group",
}
TITLES_RU = {
    "h2": "Эталонные составы и источники",
    "intro": ("Эталон — опорная справка состава, с которой судья "
              "(glm-5.3-flash, thinking low) сверяет каждый ответ модели. "
              "Собран из рецензируемой литературы (обзоры Michelot & "
              "Melendez-Howell 2003, Wieland 1983/1986, Carboué & Lopez 2021) "
              "и вики-статей с трассировкой к первоисточникам; слабо "
              "подтверждённое помечено «спорная» и штрафуется судьёй мягче. "
              "Группы: <b>основные</b> — входят в «пропущено важное» (штраф за "
              "пропуск), <b>следовые</b> — вклад в полноту, <b>спорные</b> — "
              "присутствие оспаривается литературой."),
    "sources": "Источники эталона",
    "src_col": "Источник",
    "taken": "Что взято",
    "not_present": "Не содержит (подтверждено)",
    "t_substance": "Соединение",
    "t_content": "Содержание",
    "t_effect": "Краткое описание и влияние на человека",
    "t_source": "Источник",
    "t_conf": "Уверенность",
    "control": "контрольная группа",
}


PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/#query="
# имя соединения (подстрока, без регистра) → статья Wikipedia / PubChem.
# Все вики-цели проверены HEAD-запросом (200); у отсутствующих статей —
# поисковая ссылка PubChem (NIH).
COMPOUND_LINKS_RU = [
    ("арсенохолин", "https://scholar.google.com/scholar?q=Kuehnelt+arsenocholine+Amanita+muscaria"),
    ("гетероауксин", "https://ru.wikipedia.org/wiki/Гетероауксин"),
    ("изоксазолы", "https://ru.wikipedia.org/wiki/Изоксазолы"),
    ("мускаридин", "https://ru.wikipedia.org/wiki/Мускаридин"),
    ("эрготионеин", "https://en.wikipedia.org/wiki/Ergothioneine"),
    ("гидроксипирролидинон", PUBCHEM + "hydroxypyrrolidinone"),
    ("герцинин", PUBCHEM + "hercynine"),
    ("мтгк", PUBCHEM + "MTHCC"),
    ("гемолитический белок", "https://en.wikipedia.org/wiki/Amanita_rubescens"),
    ("α-аманитин", "https://ru.wikipedia.org/wiki/Аманитин"),
    ("β-аманитин", "https://ru.wikipedia.org/wiki/Аманитин"),
    ("аманитин", "https://ru.wikipedia.org/wiki/Аманитин"),
    ("иботеновая кислота", "https://ru.wikipedia.org/wiki/Иботеновая_кислота"),
    ("мусцимол", "https://ru.wikipedia.org/wiki/Мусцимол"),
    ("мусказон", "https://ru.wikipedia.org/wiki/Мусказон"),
    ("мускарин", "https://ru.wikipedia.org/wiki/Мускарин"),
    ("ацетилхолин", "https://ru.wikipedia.org/wiki/Ацетилхолин"),
    ("холин", "https://ru.wikipedia.org/wiki/Холин"),
    ("дмт", "https://ru.wikipedia.org/wiki/Диметилтриптамин"),
    ("триптамин", "https://ru.wikipedia.org/wiki/Триптамин"),
    ("триптофан", "https://ru.wikipedia.org/wiki/Триптофан"),
    ("серотонин", "https://ru.wikipedia.org/wiki/Серотонин"),
    ("фаллоин", "https://ru.wikipedia.org/wiki/Фаллотоксины"),
    ("фаллацин", "https://ru.wikipedia.org/wiki/Фаллотоксины"),
    ("фаллисацин", "https://ru.wikipedia.org/wiki/Фаллотоксины"),
    ("мускафлавин", PUBCHEM + "muscaflavin"),
    ("мускаурины", PUBCHEM + "muscaurin"),
    ("мускапурпурин", PUBCHEM + "muscapurpurin"),
    ("глюканы", "https://en.wikipedia.org/wiki/Beta-glucan"),
    ("гликоген", "https://ru.wikipedia.org/wiki/Гликоген"),
    ("трегалоза", "https://ru.wikipedia.org/wiki/Трегалоза"),
    ("маннит", "https://ru.wikipedia.org/wiki/Маннит"),
    ("хитин", "https://ru.wikipedia.org/wiki/Хитин"),
    ("яблочная кислота", "https://ru.wikipedia.org/wiki/Яблочная_кислота"),
    ("лимонная кислота", "https://ru.wikipedia.org/wiki/Лимонная_кислота"),
    ("аскорбиновая кислота", "https://ru.wikipedia.org/wiki/Аскорбиновая_кислота"),
    ("фумаровая кислота", "https://ru.wikipedia.org/wiki/Фумаровая_кислота"),
    ("шикимовая кислота", "https://ru.wikipedia.org/wiki/Шикимовая_кислота"),
    ("янтарная кислота", "https://ru.wikipedia.org/wiki/Янтарная_кислота"),
    ("кетоглутаровая", PUBCHEM + "ketoglutaric acid"),
    ("мускарон", PUBCHEM + "muscarone"),
    ("стизолобовая", "https://pubchem.ncbi.nlm.nih.gov/#query=stizolobic acid"),
    ("стизолобиновая", "https://pubchem.ncbi.nlm.nih.gov/#query=stizolobinic acid"),
    ("фаллацидин", "https://en.wikipedia.org/wiki/Phallotoxin"),
    ("амавадин", "https://en.wikipedia.org/wiki/Amavadine"),
    ("1,3-диолеин", PUBCHEM + "1,3-diolein"),
    ("буфотенин", "https://ru.wikipedia.org/wiki/Буфотенин"),
    ("тропановые алкалоиды", "https://ru.wikipedia.org/wiki/Тропановые_алкалоиды"),
    ("аматоксины", "https://ru.wikipedia.org/wiki/Аматоксины"),
    ("фаллотоксины", "https://ru.wikipedia.org/wiki/Фаллотоксины"),
    ("фаллоидин", "https://ru.wikipedia.org/wiki/Фаллоидин"),
    ("виротоксины", "https://en.wikipedia.org/wiki/Amatoxin"),
    ("фаллолизин", PUBCHEM + "phallolysin"),
    ("антаманид", "https://ru.wikipedia.org/wiki/Антаманид"),
    ("серотонин", "https://ru.wikipedia.org/wiki/Серотонин"),
    ("триптамин", "https://ru.wikipedia.org/wiki/Триптамин"),
    ("5-meo-дмт", "https://ru.wikipedia.org/wiki/Диметилтриптамин"),
    ("тритерпены", "https://ru.wikipedia.org/wiki/Тритерпены"),
    ("меллеолиды", PUBCHEM + "melleolide"),
    ("термолабильные", "https://en.wikipedia.org/wiki/Armillaria_mellea"),
    ("полисахариды", "https://ru.wikipedia.org/wiki/Полисахариды"),
    ("глюканы", "https://en.wikipedia.org/wiki/Beta-glucan"),
    ("гликоген", "https://ru.wikipedia.org/wiki/Гликоген"),
    ("трегалоза", "https://ru.wikipedia.org/wiki/Трегалоза"),
    ("маннит", "https://ru.wikipedia.org/wiki/Маннит"),
    ("хитин", "https://ru.wikipedia.org/wiki/Хитин"),
    ("гиспидин", "https://en.wikipedia.org/wiki/Hispidin"),
    ("люциферин", "https://ru.wikipedia.org/wiki/Люциферин"),
    ("фотогенные", "https://ru.wikipedia.org/wiki/Люциферин"),
    ("тяжёлые металлы", "https://ru.wikipedia.org/wiki/Тяжёлые_металлы"),
    ("органические кислоты", "https://ru.wikipedia.org/wiki/Органические_кислоты"),
    ("эргостерол", "https://ru.wikipedia.org/wiki/Эргостерол"),
    ("лектины", "https://ru.wikipedia.org/wiki/Лектины"),
]
COMPOUND_LINKS_EN = [
    ("arsenocholine", "https://scholar.google.com/scholar?q=Kuehnelt+arsenocholine+Amanita+muscaria"),
    ("ergothioneine", "https://en.wikipedia.org/wiki/Ergothioneine"),
    ("hydroxypyrrolidinone", PUBCHEM + "hydroxypyrrolidinone"),
    ("hercynine", PUBCHEM + "hercynine"),
    ("mthcc", PUBCHEM + "MTHCC"),
    ("heteroauxin", "https://en.wikipedia.org/wiki/Indole-3-acetic_acid"),
    ("muscaridine", PUBCHEM + "muscaridine"),
    ("rubescenslysin", "https://en.wikipedia.org/wiki/Amanita_rubescens"),
    ("hemolytic protein", "https://en.wikipedia.org/wiki/Amanita_rubescens"),
    ("α-amanitin", "https://en.wikipedia.org/wiki/Amatoxin"),
    ("β-amanitin", "https://en.wikipedia.org/wiki/Amatoxin"),
    ("amanitin", "https://en.wikipedia.org/wiki/Amatoxin"),
    ("ibotenic acid", "https://en.wikipedia.org/wiki/Ibotenic_acid"),
    ("muscimol", "https://en.wikipedia.org/wiki/Muscimol"),
    ("muscazone", "https://en.wikipedia.org/wiki/Muscazone"),
    ("muscarine", "https://en.wikipedia.org/wiki/Muscarine"),
    ("choline", "https://en.wikipedia.org/wiki/Choline"),
    ("acetylcholine", "https://en.wikipedia.org/wiki/Acetylcholine"),
    ("malic acid", "https://en.wikipedia.org/wiki/Malic_acid"),
    ("citric acid", "https://en.wikipedia.org/wiki/Citric_acid"),
    ("fumaric acid", "https://en.wikipedia.org/wiki/Fumaric_acid"),
    ("shikimic acid", "https://en.wikipedia.org/wiki/Shikimic_acid"),
    ("succinic acid", "https://en.wikipedia.org/wiki/Succinic_acid"),
    ("ketoglutaric", PUBCHEM + "ketoglutaric acid"),
    ("muscaflavin", PUBCHEM + "muscaflavin"),
    ("muscaurin", PUBCHEM + "muscaurin"),
    ("muscaaurin", PUBCHEM + "muscaurin"),
    ("muscapurpurin", PUBCHEM + "muscapurpurin"),
    ("phalloin", "https://en.wikipedia.org/wiki/Phallotoxin"),
    ("phallacin", "https://en.wikipedia.org/wiki/Phallotoxin"),
    ("phallacidin", "https://en.wikipedia.org/wiki/Phallotoxin"),
    ("phallisacin", "https://en.wikipedia.org/wiki/Phallotoxin"),
    ("muscarone", PUBCHEM + "muscarone"),
    ("stizolob", "https://en.wikipedia.org/wiki/Stizolobic_acid"),
    ("amavadine", "https://en.wikipedia.org/wiki/Amavadine"),
    ("1,3-diolein", PUBCHEM + "1,3-diolein"),
    ("bufotenin", "https://en.wikipedia.org/wiki/Bufotenin"),
    ("tropane", "https://en.wikipedia.org/wiki/Tropane_alkaloid"),
    ("amatoxin", "https://en.wikipedia.org/wiki/Amatoxin"),
    ("phallotoxin", "https://en.wikipedia.org/wiki/Phallotoxin"),
    ("phalloidin", "https://en.wikipedia.org/wiki/Phallotoxin"),
    ("phalloin", "https://en.wikipedia.org/wiki/Phallotoxin"),
    ("virotoxin", "https://en.wikipedia.org/wiki/Amatoxin"),
    ("phallolysin", PUBCHEM + "phallolysin"),
    ("antamanide", "https://en.wikipedia.org/wiki/Antamanide"),
    ("serotonin", "https://en.wikipedia.org/wiki/Serotonin"),
    ("tryptamine", "https://en.wikipedia.org/wiki/Tryptamine"),
    ("tryptophan", "https://en.wikipedia.org/wiki/Tryptophan"),
    ("5-MeO-DMT", "https://en.wikipedia.org/wiki/5-MeO-DMT"),
    ("triterpene", "https://en.wikipedia.org/wiki/Triterpene"),
    ("melleolide", PUBCHEM + "melleolide"),
    ("polysaccharide", "https://en.wikipedia.org/wiki/Polysaccharide"),
    ("glucan", "https://en.wikipedia.org/wiki/Beta-glucan"),
    ("glycogen", "https://en.wikipedia.org/wiki/Glycogen"),
    ("trehalose", "https://en.wikipedia.org/wiki/Trehalose"),
    ("mannitol", "https://en.wikipedia.org/wiki/Mannitol"),
    ("chitin", "https://en.wikipedia.org/wiki/Chitin"),
    ("hispidin", "https://en.wikipedia.org/wiki/Hispidin"),
    ("luciferin", "https://en.wikipedia.org/wiki/Luciferin"),
    ("heavy metal", "https://en.wikipedia.org/wiki/Heavy_metals"),
    ("organic acid", "https://en.wikipedia.org/wiki/Organic_acid"),
    ("malic", "https://en.wikipedia.org/wiki/Malic_acid"),
    ("ascorbic", "https://en.wikipedia.org/wiki/Ascorbic_acid"),
    ("citric", "https://en.wikipedia.org/wiki/Citric_acid"),
    ("ergosterol", "https://en.wikipedia.org/wiki/Ergosterol"),
    ("lectin", "https://en.wikipedia.org/wiki/Lectin"),
]


def compound_link(name: str, en: bool) -> str:
    """Имя соединения → ссылка на проверенную статью Wikipedia, либо
    поисковая ссылка PubChem (NIH) для соединений без статьи."""
    registry = COMPOUND_LINKS_EN if en else COMPOUND_LINKS_RU
    low = name.lower()
    for needle, url in sorted(registry, key=lambda x: -len(x[0])):
        if needle.lower() in low:
            return url
    return ""


def ethalon_section(ethalon: dict) -> str:
    """Раздел «Эталонные составы и источники» (RU) / «Reference compositions
    and sources» (EN, ethalon-en.json): справка по каждому грибу с
    источником у каждого соединения, список «не содержит» и источники
    эталона. Язык — по ethalon["lang"]."""
    en = ethalon.get("lang") == "en"
    T = TITLES_EN if en else TITLES_RU
    conf_label = (lambda c: CONF_EN.get(c, (c, "conf-mid"))[0]) if en \
        else (lambda c: c)

    parts = [f'<h2>{esc(T["h2"])}</h2>', '<div class="eth">',
             f'<p class="intro">{T["intro"]}</p>']

    srcs = ethalon.get("sources") or []
    if srcs:
        parts.append(f'<h3>{esc(T["sources"])}</h3><table class="sx"><tr>'
                     f'<th style="width:44%">{esc(T["src_col"])}</th>'
                     f'<th>{esc(T["taken"])}</th></tr>')
        for s in srcs:
            links = s.get("links") or []
            links_html = (" " + " · ".join(
                f'<a class="srclink" href="{esc(l["url"])}" target="_blank" '
                f'rel="noopener">{esc(l["label"])}</a>' for l in links)
                if links else "")
            ref_html = (f'<a class="srclink" href="{esc(s["url"])}" '
                        f'target="_blank" rel="noopener">{esc(s["ref"])}</a>'
                        if s.get("url") else src_links_html(s["ref"]))
            parts.append(f'<tr><td>{ref_html}{links_html}</td>'
                         f'<td>{src_links_html(s["взято"])}</td></tr>')
        parts.append('</table>')

    for qid, e in (ethalon.get("mushrooms") or {}).items():
        title = e.get("гриб", qid)
        badge = (f' <span class="badge">{esc(T["control"])}</span>'
                 if "контрольная группа" in title or "control group" in title
                 else '')
        parts.append(f'<h3>{esc(title)}{badge}</h3>')
        if e.get("примечание"):
            parts.append(f'<p class="note">{esc(e["примечание"])}</p>')
        parts.append(f'<table class="sx eth-t"><tr>'
                     f'<th>{esc(T["t_substance"])}</th>'
                     f'<th>{esc(T["t_content"])}</th>'
                     f'<th>{esc(T["t_effect"])}</th>'
                     f'<th>{esc(T["t_source"])}</th>'
                     f'<th>{esc(T["t_conf"])}</th></tr>')
        for sub in ("основные", "следовые", "спорные"):
            entries = e.get(sub) or []
            if not entries:
                continue
            sub_label = GROUP_EN.get(sub, sub) if en else sub
            parts.append(f'<tr class="grouphd"><td colspan="5">'
                         f'{esc(sub_label)}</td></tr>')
            # группировка по химическим классам (порядок первого появления)
            by_class: dict[str, list] = {}
            for it in entries:
                by_class.setdefault(it.get("класс") or "—", []).append(it)
            for cls_name, items in by_class.items():
                parts.append(f'<tr class="classhd"><td colspan="5">'
                             f'{esc(cls_name)}</td></tr>')
                for it in items:
                    conf = it.get("уверенность", "средняя")
                    cls = CONF_CLASS.get(conf, "conf-mid")
                    nm_raw = it.get("вещество", "—")
                    # пояснения в скобках — в колонку «Краткое описание и
                    # влияние», имя соединения остаётся чистым (проще матчинг)
                    pm = re.match(r"^(.*?)\((.*)\)\s*$", nm_raw)
                    clean, desc = (pm.group(1).strip(" ,"), pm.group(2).strip()) \
                        if pm else (nm_raw, "")
                    curl_url = compound_link(clean, en)
                    name_html = (f'<a class="srclink" href="{esc(curl_url)}" '
                                 f'target="_blank" rel="noopener">'
                                 f'{esc(clean)}</a>'
                                 if curl_url else esc(clean))
                    eff_txt = it.get("влияние", "—")
                    eff_html = (f'<i>{esc(desc)}.</i> {esc(eff_txt)}'
                                if desc and eff_txt != "—" else esc(eff_txt))
                    parts.append(
                        f'<tr><td class="name-c">{name_html}</td>'
                        f'<td class="cont">{esc(it.get("содержание", "—"))}</td>'
                        f'<td class="eff">{eff_html}</td>'
                        f'<td class="src-c">{src_links_html(it.get("источник", "—"))}</td>'
                        f'<td><span class="conf {cls}">'
                        f'{esc(conf_label(conf))}</span></td></tr>')
        parts.append('</table>')

    parts.append('</div>')
    return chr(10).join(parts)


_TIP_JS = "<script>\n(function () {\n  let live = null;\n  document.addEventListener('mouseover', (e) => {\n    const td = e.target.closest && e.target.closest('td.cell');\n    if (!td || td === live) return;\n    if (live) hide(live);\n    const tip = td.querySelector('.tip');\n    if (!tip) return;\n    live = td;\n    tip.style.display = 'block';\n    const r = td.getBoundingClientRect();\n    const w = tip.offsetWidth, h = tip.offsetHeight;\n    let x = r.left + r.width / 2 - w / 2;\n    x = Math.max(8, Math.min(x, window.innerWidth - w - 8));\n    let y = r.top - h - 8;                 // над клеткой\n    if (y < 8) y = r.bottom + 8;           // не влезло — под клеткой\n    tip.style.left = x + 'px';\n    tip.style.top = y + 'px';\n  });\n  document.addEventListener('mouseout', (e) => {\n    const td = e.target.closest && e.target.closest('td.cell');\n    if (td) hide(td);\n  });\n  function hide(td) {\n    const tip = td.querySelector('.tip');\n    if (tip) tip.style.display = 'none';\n    live = null;\n  }\n  window.addEventListener('scroll', () => {\n    if (live) hide(live);\n  }, true);\n})();\n</script>"


_SORT_JS = "<script>\n(function () {\n  const table = document.querySelector('table.sx');\n  if (!table) return;\n  const body = table.tBodies[0];   // только данные, шапка в thead\n  const ths = [...table.tHead.rows[0].cells];\n  let cur = -1, dir = -1;\n  ths.forEach((th, i) => {\n    if (!th.textContent.trim()) return;\n    th.style.cursor = 'pointer';\n    th.title = 'сортировать по колонке';\n    th.addEventListener('click', () => {\n      dir = (cur === i) ? -dir : -1;\n      cur = i;\n      const rows = [...body.rows];\n      rows.sort((a, b) => {\n        const va = a.cells[i].dataset.sort ?? a.cells[i].textContent.trim();\n        const vb = b.cells[i].dataset.sort ?? b.cells[i].textContent.trim();\n        const na = parseFloat(va), nb = parseFloat(vb);\n        const num = va !== '' && vb !== '' && !isNaN(na) && !isNaN(nb);\n        return (num ? na - nb\n                     : String(va).localeCompare(String(vb), 'ru')) * dir;\n      });\n      rows.forEach(r => body.appendChild(r));\n      ths.forEach(x => { if (x !== th) x.textContent =\n        x.textContent.replace(/ [▼▲]$/, ''); });\n      th.textContent = th.textContent.replace(/ [▼▲]$/, '')\n        + (dir === -1 ? ' ▼' : ' ▲');\n    });\n  });\n  if (ths[3]) ths[3].click();  // по умолчанию: лучшая серия сверху\n})();\n</script>"


def build_index(root: Path, ethalon: dict, models_root: str = "models",
                questions_file: str | None = None,
                report_name: str = "report.html",
                alt_report: str | None = None) -> None:
    """Сводный отчёт: модель × think + матрица соединений + эталон (RU/EN)."""
    # эталонные разделы — отдельные страницы RU и EN (для англоязычного
    # эксперимента: спрашивать модели на английском и сравнивать ответы)
    for lang, fname, json_name in (("ru", "ethalon-ru.html", "ethalon.json"),
                                   ("en", "ethalon-en.html", "ethalon-en.json")):
        jp = SCRIPT_DIR / json_name
        if not jp.exists():
            continue
        eth_l = json.loads(jp.read_text(encoding="utf-8"))
        if lang == "ru":
            t = "Эталонные составы и источники"
            pr = ("Эталон — опорная справка состава (ethalon.json, v7), по "
                  "которой судья glm-5.3-flash (thinking low) сверяет ответы "
                  "моделей; собран из рецензируемой литературы и вики-статей "
                  "с трассировкой к первоисточникам. English version: "
                  "ethalon-en.html.")
            chips = (f'<div class="chip">Язык: <b>RU</b></div>'
                     f'<div class="chip">English version: '
                     f'<a class="report-link" target="_blank" rel="noopener" '
                     f'href="ethalon-en.html">ethalon-en.html</a></div>')
        else:
            t = "Reference mushroom compositions and sources"
            pr = ("English version of the reference composition "
                  "(ethalon-en.json, v7) used by the judge; prepared for an "
                  "English-language experiment — models asked about "
                  "composition in English, answers compared with the "
                  "Russian-language run. Structure and facts mirror the "
                  "Russian reference. Русская версия: ethalon-ru.html.")
            chips = (f'<div class="chip">Language: <b>EN</b></div>'
                     f'<div class="chip">Русская версия: '
                     f'<a class="report-link" target="_blank" rel="noopener" '
                     f'href="ethalon-ru.html">ethalon-ru.html</a></div>')
        (root / fname).write_text(
            page(t, pr, chips, ethalon_section(eth_l),
                 "built by build_mush_report.py"), encoding="utf-8")
        print(f"отчёт: {root / fname}")
    rows = []
    series = []
    model_dirs = sorted({d.parent for d in
                         root.glob(f"{models_root}/*/composition/run-*")
                         if (d / "session.json").exists()})
    for md in model_dirs:
        think_order = {"off": 0, "low": 1, "medium": 2, "xhigh": 3}
        thinks = sorted({json.loads((rd / "session.json").read_text(
            encoding="utf-8")).get("think", "?")
            for rd in md.glob("run-*") if (rd / "session.json").exists()},
            key=lambda t: think_order.get(t, 9))
        for think in thinks:
            try:
                runs = load_runs(md, think)
            except SystemExit:
                continue
            sessions = [r["session"] for r in runs]
            scores, h, n, json_ok, all_t = [], 0, 0, 0, 0
            dur = tokens_in = tokens_out = 0
            by_q: dict[str, list[set]] = {}
            by_qr: dict[str, list[set]] = {}
            for r in runs:
                s = r["session"]
                dur += s.get("totals", {}).get("duration_s") or 0
                tokens_in += s.get("totals", {}).get("prompt_tokens") or 0
                tokens_out += s.get("totals", {}).get("output_tokens") or 0
                for t in s.get("turns", []):
                    all_t += 1
                    json_ok += bool(t.get("json_ok"))
                    vd = r["verdicts"].get(t["id"])
                    if vd:
                        scores.append(vd.get("балл"))
                        for sv in vd.get("вещества", []):
                            n += 1
                            h += sv.get("статус") == "галлюцинация"
                    names = {norm_name(it.get("вещество", ""))
                             for it in (t.get("composition") or [])} - {""}
                    by_q.setdefault(t["id"], []).append(names)
                    real_names = set()
                    for it in (t.get("composition") or []):
                        sv = verdict_for(vd, it.get("вещество", ""))
                        if sv and sv.get("статус") == "реальное":
                            real_names.add(norm_name(it.get("вещество", "")))
                    by_qr.setdefault(t["id"], []).append(real_names)
            stab_pairs = [stability_score(qs) for qs in by_q.values()
                          if len(qs) >= 2]
            stab = statistics.mean(stab_pairs) if stab_pairs else 0.0
            stab_real_pairs = [stability_score(qs) for qs in by_qr.values()
                               if len(qs) >= 2]
            stab_real = statistics.mean(stab_real_pairs) if stab_real_pairs else 0.0
            score = statistics.mean(scores) if scores else None
            # путь от корня проекта (report.html лежит в корне)
            href = f"{md.relative_to(root).as_posix()}/report-{think}.html"
            model = sessions[0].get("model", md.name)
            provider = sessions[0].get("provider", "ollama")
            rows.append({
                "model": model, "provider": provider,
                "think": think, "runs": len(runs),
                "score": score, "h": h, "n": n, "stab": stab,
                "stab_real": stab_real,
                "json_ok": 100 * json_ok / max(1, all_t),
                "dur": dur, "tin": tokens_in, "tout": tokens_out,
                "href": href,
            })
            series.append({"model": model, "provider": provider,
                           "think": think, "runs": runs})
    order_think = {"off": 0, "low": 1, "medium": 2, "xhigh": 3}
    # порядок в шапке матрицы и таблице серий (решение владельца):
    # сначала локальные модели по алфавиту, затем облачные по алфавиту
    series_key = lambda s: (0 if s["provider"] == "ollama" else 1,
                            model_short(str(s["model"])).lower(),
                            order_think.get(s["think"], 9))
    series.sort(key=series_key)
    # «Модель × уровень»: по умолчанию лучшая серия сверху (решение
    # владельца); матрица выше остаётся локальные→облачные по алфавиту
    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or -1)))

    trs = []
    for r in rows:
        score_s = f"{r['score']:.1f}" if r["score"] is not None else "—"
        cls = score_cell_color(r["score"])
        trs.append(
            f'<tr><td data-sort="{esc(r["model"])}">{esc(r["model"])}</td>'
            f'<td data-sort="{esc(r["think"])}">{esc(r["think"])}</td>'
            f'<td data-sort="{r["runs"]}">{r["runs"]}</td>'
            f'<td class="{cls}" data-sort="{r["score"] if r["score"] is not None else -1}">{score_s}</td>'
            f'<td data-sort="{r["h"] / max(1, r["n"]):.4f}">{r["h"]} из {r["n"]}</td>'
            f'<td data-sort="{100 * r["stab"]:.1f}">{100 * r["stab"]:.0f}%</td>'
            f'<td data-sort="{100 * r["stab_real"]:.1f}">{100 * r["stab_real"]:.0f}%</td>'
            f'<td data-sort="{r["json_ok"]:.1f}">{r["json_ok"]:.0f}%</td>'
            f'<td data-sort="{r["dur"]:.0f}">{r["dur"]:.0f} c</td>'
            f'<td data-sort="{r["tin"]}">{r["tin"]}</td>'
            f'<td data-sort="{r["tout"]}">{r["tout"]}</td>'
            f'<td><a class="report-link" target="_blank" rel="noopener" '
            f'href="{esc(r["href"])}">отчёт</a></td></tr>')

    qdata = json.loads(
        (Path(questions_file) if questions_file
         else SCRIPT_DIR / "questions-composition.json").read_text(
            encoding="utf-8"))
    lang = str(qdata.get("lang", "ru")).upper()
    alt_chip = ""
    if alt_report:
        # метка противоположной серии: RU-отчёт ведёт на EN и наоборот
        alt_label = "EN-серия" if lang == "RU" else "RU-серия"
        alt_chip = (f'<div class="chip">{alt_label}: '
                    f'<a class="report-link" target="_blank" rel="noopener" '
                    f'href="{esc(alt_report)}">{esc(alt_report)}</a></div>')
    chips = "".join([
        chip("Моделей", len({r["model"] for r in rows})),
        chip("Серий (модель × think)", len(rows)),
        chip("Эталон", f"v{ethalon.get('version')}"),
        chip("Судья", "glm-5.3-flash (low, pi/zai)"),
        chip("Язык прогонов", lang),
    ]) + alt_chip
    title = ("Состав мухоморов — сводный отчёт теста"
             if lang == "RU" else
             "Mushroom composition test — English-language series")
    protocol = ("клетка = вопрос «перечисли основные и характерные соединения "
                "гриба» (recall-генерация, промпты v4); 5 прогонов на каждый "
                "уровень размышлений; балл и вердикты — судья glm-5.3-flash "
                "(thinking low) против эталонного состава. Матрица ниже "
                "построена от эталона: сначала реальные соединения и находят "
                "ли их модели, затем фантазии по частоте. Стабильность = "
                "средний попарный Jaccard наборов соединений между прогонами")
    # одна ссылка — эталон языка отчёта (решение владельца «ссылки
    # разнести»): report.html → ethalon-ru.html, report-en.html →
    # ethalon-en.html, без ссылки на инородную версию
    # подпись всегда на русском (решение владельца), ссылка — на эталон
    # языка отчёта
    own = "ethalon-ru.html" if lang == "RU" else "ethalon-en.html"
    cap = "Эталонные составы и источники"
    eth_links = ('<div class="chips">'
                 f'<div class="chip">{cap}: '
                 f'<a class="report-link" target="_blank" rel="noopener" '
                 f'href="{own}">{own}</a></div>'
                 '<div class="chip">Судья сверяет каждый ответ модели с '
                 'эталоном; все соединения в эталоне — ссылки на Wikipedia / '
                 'PubChem, каждая запись — с указанием источника</div>'
                 '</div>')
    body = f"""
<h2>Модель × уровень размышлений</h2>
<table class="sx">
<thead><tr><th>Модель</th><th>think</th><th>Прогонов</th><th>Балл судьи</th>
<th>Галлюцинации</th><th>Стабильность</th><th>Стаб. реальных</th><th>JSON</th>
<th>Время</th><th>Токены вх</th><th>Токены вых</th><th></th></tr></thead>
<tbody>
{"".join(trs)}
</tbody>
</table>
{_SORT_JS}
<div class="legend" style="margin-top:10px"><span class="hint">колонки
сортируются кликом по заголовку; по умолчанию — лучшая серия сверху ·
балл ≥ 8 — зелёный, 6–8 — янтарный, &lt; 6 — красный; «—» — вердиктов
судьи ещё нет (судья не был запущен)</span></div>
<h2>Соединения: кто знает, кто фантазирует</h2>
{substance_matrix(series, ethalon, qdata)}
{eth_links}
{methodology_section(runs=series[0]["runs"], qdata=qdata, level=False)}
"""
    footer = (f"построено build_mush_report.py · "
              f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    (root / report_name).write_text(
        page(title, protocol, chips, body, footer), encoding="utf-8")
    print(f"отчёт: {root / report_name}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model-dir", default=None,
                   help="папка composition/ модели — сводный отчёт одного уровня")
    p.add_argument("--think", default=None,
                   help="уровень размышлений для --model-dir")
    p.add_argument("--index", action="store_true",
                   help="сводный отчёт по всем моделям (report.html в корне)")
    p.add_argument("--ethalon", default=str(SCRIPT_DIR / "ethalon.json"))
    p.add_argument("--questions", default=str(SCRIPT_DIR / "questions-composition.json"),
                   help="файл вопросника (для EN-серий: questions-composition-en.json)")
    p.add_argument("--models-root", default="models",
                   help="корень дерева моделей для --index (EN: models-en)")
    p.add_argument("--report-name", default="report.html",
                   help="имя сводного отчёта (EN: report-en.html)")
    p.add_argument("--alt-report", default=None,
                   help="ссылка-чип на серию другого языка (напр. report-en.html)")
    args = p.parse_args()

    ethalon = json.loads(Path(args.ethalon).read_text(encoding="utf-8"))
    root = SCRIPT_DIR
    if args.index:
        build_index(root, ethalon, models_root=args.models_root,
                    questions_file=args.questions,
                    report_name=args.report_name,
                    alt_report=args.alt_report)
        return 0
    if args.model_dir:
        if not args.think:
            p.error("--model-dir требует --think (уровни не смешиваются)")
        build_level(Path(args.model_dir), args.think, ethalon,
                    questions_file=args.questions,
                    backlink_name=args.report_name)
        return 0
    p.error("укажите --model-dir + --think или --index")


if __name__ == "__main__":
    sys.exit(main())
