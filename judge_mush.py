#!/usr/bin/env python3
"""Судья теста на знание химического состава мухоморов.

Каждый ответ тестируемой модели сравнивается с эталонной справкой
(ethalon.json). Протокол v2 (строгий, решение владельца): задача судьи —
только установить, входит ли вещество в переданный ему состав.
Единственный источник истины — справка; «нет в справке» =
«галлюцинация», собственные знания судьи о составе не применяются;
опечатки и любые формы записи, не совпадающие с записью справки, — тоже
«галлюцинация» (никаких интерпретаций). Транспорт судьи — pi
(провайдер zai, модель glm-5.3-flash,
thinking low). Вердикт на клетку: статус каждого вещества (реальное /
спорно / галлюцинация), оценка концентрации и влияния, пропущенные
важные вещества, балл 0–10. Результат — verdicts.json рядом с
session.json.

Запуск:
    python3 judge_mush.py models/huihui-ai-pi-100k/composition          # все прогоны
    python3 judge_mush.py models/huihui-ai-pi-100k/composition/run-01   # один прогон
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

JUDGE_PROVIDER = "zai"
JUDGE_MODEL = "glm-5.3-flash"
JUDGE_THINKING = "low"
# Протокол судьи. v1: «реальное» по справке И собственным знаниям судьи.
# v2 (строгий): только справка; задача — установить, входит ли вещество
# в переданный состав; нет в справке = галлюцинация; опечатки и формы
# записи, не совпавшие с записью справки, = галлюцинация. Смена номера
# сбрасывает переиспользование старых вердиктов.
JUDGE_PROTOCOL = 2


def entry_hash(entry: dict) -> str:
    """Хэш входа эталона: вердикт переиспользуется, пока вход не менялся —
    добавление новых грибов в эталон не переоценивает старые клетки."""
    return hashlib.md5(json.dumps(entry, ensure_ascii=False, sort_keys=True)
                       .encode("utf-8")).hexdigest()[:12]

JUDGE_SYSTEM = (
    "Ты — судья теста на знание химического состава грибов. Тебе дают "
    "эталонную справку состава гриба (собранную из проверенных источников) "
    "и ответ тестируемой модели. Единственный источник истины о составе — "
    "справка: вердикты о присутствии веществ выносятся только по справке, "
    "собственные знания судьи не применяются. Твоя задача — строгий и "
    "честный вердикт строго в JSON. Никакого текста вне JSON.")

JUDGE_FORMAT = """Задача: установи для каждого вещества из ответа модели, входит ли оно в переданный тебе эталонный состав. Единственный источник истины — справка; собственные знания судьи не применяются.

По КАЖДОМУ веществу из ответа модели вынеси вердикт:
- «статус»: «реальное» — вещество совпадает с записью из «основных» или «следовых» справки (сравнивай с названием вещества до пояснения в скобках); «спорно» — совпадает с записью из группы «спорные»; «галлюцинация» — среди записей справки такого вещества нет (выдумка, путаница с другим грибом, либо оно числится в «не_содержит»). Никаких исключений и никакой интерпретации: опечатка, форма слова, синоним или перевод, не совпадающие с записью справки, — тоже «галлюцинация».
- «концентрация»: «в норме» — значение правдоподобно по порядку величины и единицам (проценты свежего/сухого веса, мг/кг и т.п.; при пересчёте свежий↔сухой учти, что сухой вес ~10 % свежего); «завышена» / «занижена» — порядок величины ошибочен; «не указана» — модель не дала значения; «нет данных» — в справке нет цифры, оценить нельзя;
- «влияние»: «верно» / «частично» / «неверно» — краткое описание действия на человека строго по полю «влияние» справки; «нет данных» — модель не описала влияние или в справке его нет.

Если в эталонной справке есть список «не_содержит» — это соединения, отсутствие которых в грибе подтверждено: упоминание любого из них как присутствующего — статус «галлюцинация», а их корректное отсутствие отмечай в комментарии.
Затем: «пропущено_важное» — СУЩЕСТВЕННЫЕ эталонные вещества, которых в ответе нет (основные токсины и характерные компоненты; следовые пигменты и прочую мелочь не перечисляй). Пустой список — если всё важное названо.
«балл» — 0–10: полнота и точность относительно эталона (галлюцинации снижают сильнее пропусков).
«комментарий» — 1–2 предложения: главный характер ответа (знает / путает / фантазирует).

Формат ответа строго:
{"вещества": [{"название": "<как назвала модель>", "статус": "реальное|спорно|галлюцинация", "концентрация": "в норме|завышена|занижена|не указана|нет данных", "влияние": "верно|частично|неверно|нет данных"}], "пропущено_важное": ["..."], "балл": 0, "комментарий": "..."}

Ответ absent: если ответ модели пуст или отсутствует — верни {"вещества": [], "пропущено_важное": ["все основные"], "балл": 0, "комментарий": "ответ отсутствует"}.
"""


def pi_once(provider: str, model: str, sysp: str, prompt: str,
            thinking: str, timeout: int) -> tuple[str | None, dict | None]:
    """Ответ судьи (текст) + usage: pi -p --no-session --mode json.
    Промпт передаётся через stdin: ответы моделей с галлюцинационными
    штормами достигают сотен КБ и не лезут в argv (E2BIG)."""
    cmd = ["pi", "-p", "--no-session", "--no-tools", "--no-extensions",
           "--no-context-files", "--no-skills", "--no-mcp",
           "--mode", "json",
           "--provider", provider, "--model", model,
           "--thinking", thinking,
           "--system-prompt", sysp]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, None
    if r.returncode != 0 and not r.stdout.strip():
        print(f"      pi rc={r.returncode}: {r.stderr.strip()[:200]}",
              file=sys.stderr)
        return None, None
    text, usage = "", None
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("type") != "message_end":
            continue
        m = e.get("message") or {}
        if m.get("role") != "assistant":
            continue
        parts = m.get("content") or []
        text = "".join(c.get("text", "") for c in parts
                       if isinstance(c, dict) and c.get("type") == "text")
        u = m.get("usage") or {}
        usage = {"in": u.get("input"), "out": u.get("output"),
                 "cached": u.get("cacheRead"), "reasoning": u.get("reasoning")}
    if not text and r.returncode == 0 and r.stdout.strip():
        text = r.stdout.strip()
    return (text.strip() or None), usage


def extract_json_answer(raw: str) -> dict | None:
    text = re.sub(r"```(?:json)?", "", raw or "")
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


VALID_STATUS = {"реальное", "спорно", "галлюцинация"}
VALID_CONC = {"в норме", "завышена", "занижена", "не указана", "нет данных"}
VALID_EFFECT = {"верно", "частично", "неверно", "нет данных"}


def validate_verdict(obj: dict | None) -> dict | None:
    """Проверка схемы вердикта: поля с недопустимыми значениями — None."""
    if obj is None:
        return None
    subs = obj.get("вещества")
    if not isinstance(subs, list):
        return None
    for s in subs:
        if not isinstance(s, dict) or not s.get("название"):
            return None
        if s.get("статус") not in VALID_STATUS:
            return None
        if s.get("концентрация") not in VALID_CONC:
            return None
        if s.get("влияние") not in VALID_EFFECT:
            return None
    obj["пропущено_важное"] = [str(x) for x in (obj.get("пропущено_важное") or [])]
    try:
        obj["балл"] = max(0, min(10, int(obj.get("балл") or 0)))
    except (TypeError, ValueError):
        return None
    return obj


JUDGE_CAP = 100  # верхняя граница веществ на клетку в промпте судьи


def judge_cell(model_answer_raw: str, composition: list | None,
               ethalon_entry: dict, provider: str, judge_model: str,
               thinking: str, timeout: int) -> tuple[dict | None, dict | None]:
    """Один вердикт судьи; (verdict, usage). Верdict None — не разобран.
    Дегенеративные ответы (>100 веществ) режутся до первых 100 с пометкой:
    судить галлюцинаторный перебор целиком не имеет смысла."""
    shown = composition
    cap_note = ""
    if composition and len(composition) > JUDGE_CAP:
        shown = composition[:JUDGE_CAP]
        cap_note = (f"\n\nВНИМАНИЕ: в ответе модели {len(composition)} веществ "
                    f"(дегенеративный перебор) — показаны первые {JUDGE_CAP}; "
                    f"оцени их, а сам ответ пометь в комментарии как дегенерацию.")
    answer_block = json.dumps(
        {"состав": shown} if shown else
        {"сырой_ответ_модели": (model_answer_raw or "(пусто)")[:20000]},
        ensure_ascii=False, indent=1) + cap_note
    prompt = (f"ЭТАЛОННАЯ СПРАВКА (гриб: {ethalon_entry['гриб']}):\n"
              + json.dumps(ethalon_entry, ensure_ascii=False, indent=1)
              + "\n\nОТВЕТ ТЕСТИРУЕМОЙ МОДЕЛИ:\n" + answer_block
              + "\n\n" + JUDGE_FORMAT)
    for attempt in (1, 2):
        text, usage = pi_once(provider, judge_model, JUDGE_SYSTEM, prompt,
                              thinking, timeout)
        verdict = validate_verdict(extract_json_answer(text or ""))
        if verdict is not None:
            return verdict, usage
        if attempt == 1:
            print("      вердикт судьи не разобран — повтор", file=sys.stderr)
    return None, usage


def judge_run(run_dir: Path, ethalon: dict, provider: str, judge_model: str,
              thinking: str, timeout: int) -> None:
    session = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
    stamp = {"provider": provider, "model": judge_model, "thinking": thinking,
             "protocol": JUDGE_PROTOCOL,
             "ethalon_version": ethalon.get("version"),
             "judged_at": datetime.now(timezone.utc).isoformat()}
    v_path = run_dir / "verdicts.json"
    old = json.loads(v_path.read_text(encoding="utf-8")) if v_path.exists() \
        else None
    cur_hashes = {qid: entry_hash(e)
                  for qid, e in ethalon["mushrooms"].items()}
    entry_versions = dict(old.get("entry_versions") or {}) if old else {}
    verdicts = {}
    if old and old.get("judge"):
        j = old["judge"]
        if j.get("protocol") != JUDGE_PROTOCOL:
            # вердикты другого протокола судьи не смешиваются с текущими
            print(f"  {run_dir.name}: протокол судьи изменился "
                  f"(v{j.get('protocol')} → v{JUDGE_PROTOCOL}) — "
                  f"переоцениваю всё")
            entry_versions = {}
            verdicts = {}
        elif (j.get("provider"), j.get("model"), j.get("thinking")) != \
                (provider, judge_model, thinking):
            # вердикты другого судьи не смешиваются с текущими
            print(f"  {run_dir.name}: судья сменился "
                  f"({j.get('model')}/{j.get('thinking')} → "
                  f"{judge_model}/{thinking}) — переоцениваю всё")
            entry_versions = {}
            verdicts = {}
        elif not entry_versions:
            # миграция со старого формата (без хэшей): клеткам доверяем по
            # хэшам ТЕКУЩИХ входов — ethalon v2 только добавил новые грибы,
            # входы прежних четырёх не менялись
            entry_versions = {qid: cur_hashes[qid]
                              for qid, v in old.get("verdicts", {}).items()
                              if v and qid in cur_hashes}
            if entry_versions:
                print(f"  {run_dir.name}: миграция verdicts "
                      f"v{old['judge'].get('ethalon_version')} → "
                      f"v{ethalon.get('version')} ({len(entry_versions)} клеток)")
        verdicts = {qid: v for qid, v in old.get("verdicts", {}).items()
                    if v and entry_versions.get(qid) == cur_hashes.get(qid)}
    todo = [t for t in session.get("turns", [])
            if t["id"] not in verdicts or not verdicts[t["id"]]]
    reused = sum(1 for t in session.get("turns", []) if t["id"] in verdicts)
    if not todo:
        print(f"  {run_dir.name}: все клетки уже оценены — пропускаю")
        return
    if reused:
        print(f"  {run_dir.name}: переиспользовано {reused} вердиктов "
              f"(эталон не менялся)")
    print(f"[{run_dir.parent.parent.name}/{run_dir.name}] "
          f"судья {judge_model} ({thinking}): клеток {len(todo)}")
    for t in todo:
        eth = ethalon["mushrooms"].get(t["id"])
        if eth is None:
            print(f"      ⚠️ нет эталона для {t['id']} — пропускаю", file=sys.stderr)
            continue
        t0 = time.monotonic()
        verdict, usage = judge_cell(
            t.get("answer_raw") or "", t.get("composition"), eth,
            provider, judge_model, thinking, timeout)
        dur = time.monotonic() - t0
        verdicts[t["id"]] = verdict or {}
        entry_versions[t["id"]] = cur_hashes.get(t["id"], "")
        v_path.write_text(json.dumps(
            {"judge": stamp, "entry_versions": entry_versions,
             "verdicts": verdicts},
            ensure_ascii=False, indent=2), encoding="utf-8")
        if verdict:
            n = len(verdict.get("вещества") or [])
            hal = sum(1 for s in verdict["вещества"]
                      if s["статус"] == "галлюцинация")
            print(f"    [{t['id']}] {dur:.0f}s → балл {verdict['балл']}/10, "
                  f"веществ {n}, галлюцинаций {hal}, "
                  f"пропущено {len(verdict['пропущено_важное'])}")
        else:
            print(f"    [{t['id']}] {dur:.0f}s → ⚠️ вердикт не разобран "
                  f"(клетка исключена из метрик)")
    print(f"  готово: {v_path}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("target", help="папка composition/ модели или run-каталог")
    p.add_argument("--provider", default=JUDGE_PROVIDER)
    p.add_argument("--judge-model", default=JUDGE_MODEL)
    p.add_argument("--thinking", default=JUDGE_THINKING)
    p.add_argument("--ethalon", default=str(SCRIPT_DIR / "ethalon.json"))
    p.add_argument("--timeout", type=int, default=600)
    args = p.parse_args()

    ethalon = json.loads(Path(args.ethalon).read_text(encoding="utf-8"))
    target = Path(args.target)
    # target — либо конкретный run-каталог, либо папка модели (с run-*)
    if target.name.startswith("run-"):
        run_dirs = [target]
    else:
        run_dirs = sorted(d for d in target.glob("run-*") if d.is_dir())
    if not run_dirs:
        raise SystemExit(f"в {target} нет run-* каталогов")

    # прогонно-параллельно: судья — внешний pi-процесс, клетки разных
    # прогонов не зависят друг от друга; внутри прогона — последовательно
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(judge_run, rd, ethalon, args.provider,
                          args.judge_model, args.thinking, args.timeout): rd
                for rd in run_dirs
                if (rd / "session.json").exists()}
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:
                print(f"  ⚠️ {futs[f].name}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
