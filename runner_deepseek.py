#!/usr/bin/env python3
"""Раннер API-моделей через pi-транспорт (deepseek-flash и т.п.).

Тот же формат данных, что runner_mush (session.json / answers.csv /
run-XX), поэтому судья v2 и билдер отчётов работают без изменений.
Транспорт — pi -p (anthropic-messages к api.deepseek.com), ключ из
pi auth. Изоляция прогонов не нужна: API stateless.

Запуск:
    python3 runner_deepseek.py --runs 5            # deepseek-flash, RU
    python3 runner_deepseek.py --model deepseek-v4-pro
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from judge_mush import pi_once  # noqa: E402
from runner_mush import parse_composition, write_answers_csv  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", default="deepseek")
    p.add_argument("--model", default="deepseek-flash")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--think", choices=["off", "low"], default="off",
                   help="уровень размышлений для pi --thinking")
    p.add_argument("--questions",
                   default=str(SCRIPT_DIR / "questions-composition.json"))
    p.add_argument("--out", default=str(SCRIPT_DIR / "models" /
                                        "deepseek-flash" / "composition"))
    p.add_argument("--timeout", type=int, default=240)
    args = p.parse_args()

    import json
    qdata = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    questions = qdata["questions"]
    system_prompt = qdata["system_prompt"]
    out_root = Path(args.out)

    run_no = 1
    while (out_root / f"run-{run_no:02d}").exists():
        run_no += 1
    total_runs = run_no + args.runs - 1

    for run_no in range(run_no, total_runs + 1):
        out_dir = out_root / f"run-{run_no:02d}"
        print(f"[run-{run_no:02d}] {args.provider}/{args.model} "
              f"· think {args.think}")
        turns: list = []
        session = {
            "model": args.model,
            "run": run_no,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "base_url": "api.deepseek.com (через pi)",
            "protocol": f"pi (anthropic-messages, {args.provider} api)",
            "provider": args.provider,
            "keep_alive": "n/a (API stateless)",
            "think": args.think,
            "question_count": 0,
            "questionnaire": qdata.get("version"),
            "questionnaire_file": Path(args.questions).name,
            "system_prompt": system_prompt,
            "totals": {},
            "turns": turns,
        }

        def flush() -> None:
            session["question_count"] = len(turns)
            session["totals"] = {
                "duration_s": round(sum(t["duration_s"] for t in turns), 1),
                "prompt_tokens": sum(t.get("prompt_eval_count") or 0
                                     for t in turns),
                "output_tokens": sum(t.get("eval_count") or 0 for t in turns),
                "cached_tokens": sum(t.get("prompt_eval_cached_count") or 0
                                     for t in turns),
            }
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "session.json").write_text(
                json.dumps(session, ensure_ascii=False, indent=2),
                encoding="utf-8")

        for q in questions:
            t0 = time.monotonic()
            text, usage = pi_once(args.provider, args.model, system_prompt,
                                  q["prompt"], args.think, args.timeout)
            if text is None:
                text, usage = pi_once(args.provider, args.model,
                                      system_prompt, q["prompt"],
                                      args.think, args.timeout)
            duration = time.monotonic() - t0
            answer_raw = (text or "").strip()
            composition, json_ok = parse_composition(answer_raw)
            u = usage or {}
            turns.append({
                "id": q["id"], "topic": q["topic"],
                "prompt": q["prompt"],
                "answer_raw": answer_raw,
                "composition": composition,
                "json_ok": json_ok,
                "n_substances": len(composition) if composition else 0,
                "thinking_len": int(u.get("reasoning") or 0),
                "duration_s": round(duration, 2),
                "eval_count": u.get("out"),
                "prompt_eval_count": u.get("in"),
                "prompt_eval_cached_count": u.get("cached"),
                "done_reason": "stop" if text is not None else "error",
            })
            mark = f"{len(composition) if composition else '?'} вещ."
            print(f"    [{q['id']}] {q['topic']}: {duration:.0f}s → "
                  f"{'✅' if json_ok else '⬜'} {mark}")
            flush()
        flush()
        write_answers_csv(out_dir, turns)
        total = sum(t["duration_s"] for t in turns)
        print(f"  готово: {out_dir} ({len(turns)} клеток, "
              f"{sum(t['n_substances'] for t in turns)} веществ всего, "
              f"{total:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
