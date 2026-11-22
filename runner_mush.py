#!/usr/bin/env python3
"""Раннер теста на знание состава мухоморов (recall-генерация).

Один прогон = полный опрос: 4 клетки, по одной на гриб. Модель сама
перечисляет состав в JSON (вещество / содержание / влияние на человека).
Знание меряется не да/нет-клетками, а (а) стабильностью состава между
прогонами и (б) вердиктом судьи (judge_mush.py) против эталона.

Отвечает за транспорты: ollama /api/chat (локальные модели).

Примеры:
    python3 runner_mush.py --model huihui-ai:pi-100k --runs 1 --think off   # smoke
    python3 runner_mush.py --model gemma4:26b --runs 5 --think low
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def load_env_local() -> None:
    env_file = SCRIPT_DIR / ".env.local"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            os.environ.setdefault(name.strip(), os.path.expanduser(value.strip()))


load_env_local()
DEFAULT_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")


def http_json(url: str, payload: dict, timeout: int, retries: int = 3) -> dict:
    """POST с JSON и повторами ТОЛЬКО на сетевые сбои (контент не ретраим)."""
    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = e
            if attempt < retries:
                wait = 5 * attempt
                print(f"    сеть недоступна ({e}); повтор через {wait}s",
                      file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"запрос к {url} не удался после {retries} попыток: {last_err}")


def unload_model(base_url: str, model: str, timeout: int) -> None:
    """Выгрузить модель из памяти (сброс KV-кэша ollama)."""
    http_json(f"{base_url}/api/generate", {"model": model, "keep_alive": 0}, timeout)


def slugify(tag: str) -> str:
    return tag.replace(":", "-").replace("/", "-")


# Структурированный вывод (ollama structured outputs): декодирование
# ограничено грамматикой по этой схеме — текст вне JSON невозможен,
# лимит 100 элементов переносится из инструкции в уровень декодера.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "гриб": {"type": "string"},
        "состав": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "properties": {
                    "вещество": {"type": "string"},
                    "содержание": {"type": "string"},
                    "влияние": {"type": "string"},
                },
                "required": ["вещество", "содержание", "влияние"],
            },
        },
    },
    "required": ["гриб", "состав"],
}
FORMAT_LABEL = ("json-schema (structured outputs): {гриб, "
                "состав[{вещество, содержание, влияние}], maxItems 100}")

# EN-вариант контракта для англоязычных серий: те же поля английскими
# ключами (parser понимает их через KEY_ALIASES). Полностью англоязычный
# промпт без русских токенов — иначе модели скатываются в русские ответы.
OUTPUT_SCHEMA_EN = {
    "type": "object",
    "properties": {
        "mushroom": {"type": "string"},
        "composition": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "properties": {
                    "compound": {"type": "string"},
                    "amount": {"type": "string"},
                    "effect": {"type": "string"},
                },
                "required": ["compound", "amount", "effect"],
            },
        },
    },
    "required": ["mushroom", "composition"],
}
FORMAT_LABEL_EN = ("json-schema (structured outputs): {mushroom, "
                   "composition[{compound, amount, effect}], maxItems 100}")


def extract_json_answer(raw: str) -> dict | None:
    """Достать JSON-объект из ответа модели: ```-заборы срезаются, берётся
    первый сбалансированный {...}. None — если валидного JSON нет."""
    text = re.sub(r"```(?:json)?", "", raw)
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
                frag = text[start:i + 1]
                try:
                    obj = json.loads(frag)
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


# синонимы ключей состава у моделей
KEY_ALIASES = {
    "вещество": ("вещество", "название", "name", "компонент", "component",
                 "вещества", "compound", "substance"),
    "содержание": ("содержание", "концентрация", "количество", "amount",
                   "content", "concentration", "доза", "содержание_в_грибе"),
    "влияние": ("влияние", "эффект", "действие", "эффект_на_человека",
                "эффекты", "effect", "воздействие", "действие_на_человека"),
}


def normalize_item(it: dict) -> dict | None:
    norm = {}
    for field, aliases in KEY_ALIASES.items():
        for a in aliases:
            if it.get(a) not in (None, ""):
                norm[field] = str(it[a]).strip()
                break
    if not norm.get("вещество"):
        return None
    return {
        "вещество": norm["вещество"],
        "содержание": norm.get("содержание") or "не указано",
        "влияние": norm.get("влияние") or "—",
    }


def parse_composition(raw: str) -> tuple[list | None, bool]:
    """Разобрать ответ в список веществ [{вещество, содержание, влияние}].
    Возвращает (список, json_ok). Фолбэк: усечённый/дегенеративный JSON
    (модель оборвала ответ до закрытия скобок) — извлекаем целые плоские
    фрагменты-объекты; json_ok=False помечает такой ремонт."""
    obj = extract_json_answer(raw)
    if obj is not None:
        comp = obj.get("состав")
        if comp is None:
            comp = (obj.get("composition") or obj.get("вещества")
                    or obj.get("components"))
        if isinstance(comp, list):
            items = [normalize_item(it) for it in comp
                     if isinstance(it, dict)]
            items = [it for it in items if it]
            if items:
                return items, True
            return None, False
    # фолбэк: плоские фрагменты {...} без вложенных скобок
    items = []
    for m in re.finditer(r"\{[^{}]*\}", raw or "", re.DOTALL):
        try:
            it = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(it, dict):
            n = normalize_item(it)
            if n:
                items.append(n)
    return (items or None), False


def check_questions_file(data: dict) -> None:
    qs = data["questions"]
    ids = [q["id"] for q in qs]
    if len(set(ids)) != len(ids):
        raise SystemExit("дубликаты id в вопроснике")
    for q in qs:
        for field in ("id", "topic", "prompt"):
            if field not in q:
                raise SystemExit(f"у клетки {q.get('id', '?')} нет поля {field}")
    print(f"вопросник v{data.get('version')}: {len(qs)} клеток "
          f"(recall-генерация состава)")


def run_one(base_url: str, model: str, questions: list, q_version,
            run_no: int, out_dir: Path, keep_alive, think: str, timeout: int,
            system_prompt: str, limit: int | None,
            questions_file: str = "questions-composition.json",
            output_schema: dict | None = None,
            format_label: str = FORMAT_LABEL) -> None:
    if think == "off":
        print(f"  выгружаю модель перед прогоном...")
        unload_model(base_url, model, timeout)

    turns: list = []
    session = {
        "model": model,
        "run": run_no,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "protocol": "chat (ollama /api/chat)",
        "provider": "ollama",
        "format": format_label,
        "keep_alive": keep_alive,
        "think": think,
        "question_count": 0,
        "questionnaire": q_version,
        "questionnaire_file": Path(questions_file).name,
        "system_prompt": system_prompt,
        "totals": {},
        "turns": turns,
    }

    def flush():
        session["question_count"] = len(turns)
        session["totals"] = {
            "duration_s": round(sum(t["duration_s"] for t in turns), 1),
            "prompt_tokens": sum(t.get("prompt_eval_count") or 0 for t in turns),
            "output_tokens": sum(t.get("eval_count") or 0 for t in turns),
            "cached_tokens": sum(t.get("prompt_eval_cached_count") or 0
                                 for t in turns),
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "session.json").write_text(
            json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    to_ask = questions[:limit] if limit else questions
    session["cells"] = [q["id"] for q in to_ask]
    for q in to_ask:
        t0 = time.monotonic()
        payload = {"model": model, "stream": False, "keep_alive": keep_alive,
                   "messages": [
                       {"role": "system", "content": system_prompt},
                       {"role": "user", "content": q["prompt"]}]}
        payload["think"] = False if think == "off" else think
        payload["format"] = output_schema if output_schema else OUTPUT_SCHEMA
        try:
            resp = http_json(f"{base_url}/api/chat", payload, timeout)
        except Exception as e:
            print(f"      ⚠️ сбой генерации ({e}); клетка серой", file=sys.stderr)
            resp = None
        duration = time.monotonic() - t0
        if resp is None:
            answer_raw, composition, json_ok = "", None, False
            eval_count = prompt_eval_count = cached_count = None
            done_reason = "error"
        else:
            msg = resp.get("message", {})
            answer_raw = (msg.get("content") or "").strip()
            thinking = (msg.get("thinking") or "").strip() or None
            done_reason = resp.get("done_reason")
            composition, json_ok = parse_composition(answer_raw)
            eval_count = resp.get("eval_count")
            prompt_eval_count = resp.get("prompt_eval_count")
            cached_count = resp.get("prompt_eval_cached_count")
        turns.append({
            "id": q["id"], "topic": q["topic"],
            "prompt": q["prompt"],
            "answer_raw": answer_raw,
            "composition": composition,  # [{вещество, содержание, влияние}] | None
            "json_ok": json_ok,
            "n_substances": len(composition) if composition else 0,
            "thinking_len": len(thinking) if thinking else 0,
            "duration_s": round(duration, 2),
            "eval_count": eval_count,
            "prompt_eval_count": prompt_eval_count,
            "prompt_eval_cached_count": cached_count,
            "done_reason": done_reason,
        })
        mark = f"{len(composition) if composition else '?'} вещ."
        trunc = " ⚠️ ОБРЫВ: " + str(done_reason) if done_reason not in (None, "stop") else ""
        print(f"    [{q['id']}] {q['topic']}: {duration:.0f}s → "
              f"{'✅' if json_ok else '⬜'} {mark}{trunc}")
        flush()

    flush()
    write_answers_csv(out_dir, turns)
    total = sum(t["duration_s"] for t in turns)
    print(f"  готово: {out_dir} ({len(turns)} клеток, "
          f"{sum(t['n_substances'] for t in turns)} веществ всего, {total:.0f}s)")
    try:
        unload_model(base_url, model, timeout)
        print("  модель выгружена (изоляция прогонов)")
    except Exception as e:
        print(f"  не удалось выгрузить модель: {e}", file=sys.stderr)


def write_answers_csv(out_dir: Path, turns: list) -> None:
    """Одна строка на клетку; состав — в session.json (полные записи)."""
    with (out_dir / "answers.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["id", "topic", "json_ok", "n_substances",
                    "substances", "duration_s", "prompt_tokens",
                    "output_tokens", "cached_tokens", "done_reason"])
        for t in turns:
            subs = "; ".join(
                (it.get("вещество") or "").replace(";", ",")
                for it in (t.get("composition") or []))
            w.writerow([t["id"], t["topic"], int(t.get("json_ok") or False),
                        t.get("n_substances"), subs, t["duration_s"],
                        t.get("prompt_eval_count"), t.get("eval_count"),
                        t.get("prompt_eval_cached_count"), t.get("done_reason")])


def reparse(model_dir: Path) -> None:
    """Перепарсить сохранённые raw-ответы текущим парсером (фолбэк-ремонт
    усечённого JSON); перепрогона не нужно. Клетки с composition=None,
    где в raw есть целые фрагменты, получают состав (json_ok=False)."""
    fixed = 0
    for s_path in sorted(model_dir.glob("run-*/session.json")):
        session = json.loads(s_path.read_text(encoding="utf-8"))
        changed = False
        for t in session.get("turns", []):
            if t.get("composition"):
                continue
            comp, ok = parse_composition(t.get("answer_raw") or "")
            if comp:
                t["composition"] = comp
                t["n_substances"] = len(comp)
                if not ok:
                    print(f"    [{t['id']}] {t['topic']}: фолбэк-ремонт → "
                          f"{len(comp)} веществ")
                changed = True
                fixed += 1
        if changed:
            session["totals"] = {
                "duration_s": round(sum(x["duration_s"] for x in session["turns"]), 1),
                "prompt_tokens": sum(x.get("prompt_eval_count") or 0
                                     for x in session["turns"]),
                "output_tokens": sum(x.get("eval_count") or 0
                                     for x in session["turns"]),
                "cached_tokens": sum(x.get("prompt_eval_cached_count") or 0
                                     for x in session["turns"]),
            }
            s_path.write_text(json.dumps(session, ensure_ascii=False, indent=2),
                              encoding="utf-8")
            write_answers_csv(s_path.parent, session["turns"])
    print(f"reparse готов: восстановлено {fixed} клеток")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default=None, help="ollama-тег модели")
    p.add_argument("--runs", type=int, default=1, help="число прогонов (default 1)")
    p.add_argument("--think", choices=["off", "low", "medium", "xhigh"],
                   default="off", help="уровень размышлений (default off)")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--questions", default=str(SCRIPT_DIR / "questions-composition.json"))
    p.add_argument("--models-root", default=str(SCRIPT_DIR / "models"))
    p.add_argument("--out", default=None,
                   help="папка модели; по умолчанию <models-root>/<slug>")
    p.add_argument("--run", type=int, default=None,
                   help="номер первого прогона; по умолчанию — первый свободный")
    p.add_argument("--limit", type=int, default=None,
                   help="только первые N клеток (для smoke)")
    p.add_argument("--only", default=None,
                   help="только указанные клетки через запятую "
                        "(например ARUB,ACAE) — добор контрольной группы "
                        "без перепрогона остальной матрицы")
    p.add_argument("--keep-alive", default="10m")
    p.add_argument("--timeout", type=int, default=900,
                   help="таймаут одного запроса, сек (default 900)")
    p.add_argument("--reparse", metavar="MODEL_DIR",
                   help="перепарсить сохранённые raw-ответы папки текущим "
                        "парсером (без перепрогона)")
    args = p.parse_args()

    if args.reparse:
        reparse(Path(args.reparse))
        return 0
    if not args.model:
        p.error("укажите --model (или --reparse MODEL_DIR)")

    qdata = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    q_version = qdata.get("version")
    questions = qdata["questions"]
    # EN-вопросники: полностью англоязычный промпт и EN-контракт JSON,
    # иначе модели отвечают по-русски (эмпирика EN-цикла 20.11.2026)
    if str(qdata.get("lang", "ru")) == "en":
        schema, fmt_label = OUTPUT_SCHEMA_EN, FORMAT_LABEL_EN
    else:
        schema, fmt_label = OUTPUT_SCHEMA, FORMAT_LABEL
    if args.only:
        want = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        questions = [q for q in questions if q["id"] in want]
        if not questions:
            raise SystemExit(f"--only {args.only}: нет таких id в вопроснике")
        print(f"--only: клетки {', '.join(q['id'] for q in questions)}")
    check_questions_file(qdata)
    system_prompt = qdata["system_prompt"]

    out_root = (Path(args.out) if args.out
                else Path(args.models_root) / slugify(args.model))
    out_root = out_root / "composition"

    keep_alive = int(args.keep_alive) if str(args.keep_alive).lstrip("-").isdigit() \
        else args.keep_alive

    run_no = args.run
    for i in range(args.runs):
        if run_no is None:
            run_no = 1
            while (out_root / f"run-{run_no:02d}").exists():
                run_no += 1
        out_dir = out_root / f"run-{run_no:02d}"
        if out_dir.exists():
            print(f"папка {out_dir} уже существует — пропускаю "
                  f"(удалите или укажите --run)", file=sys.stderr)
            run_no += 1
            continue
        print(f"[run-{run_no:02d}] {args.model} · think {args.think}")
        run_one(args.base_url, args.model, questions, q_version, run_no,
                out_dir, keep_alive, args.think, args.timeout, system_prompt,
                args.limit, questions_file=str(Path(args.questions).name),
                output_schema=schema, format_label=fmt_label)
        run_no += 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
