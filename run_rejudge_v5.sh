#!/usr/bin/env bash
# Переоценка всех клеток под эталон v5 (источники у соединений,
# не_содержит, перестроенный ACAE) + пересборка всех отчётов.
set -u
cd /home/dp/MyProjects/muhomoring-test

for slug in gemma4-26b gemma4-31b-it-qat huihui-ai-pi-100k medgemma-27b \
            orcarouter-Qwen3.8-27B-Uncensored-latest qwen3.8-latest; do
  echo "=== JUDGE v5 $slug · $(date '+%H:%M:%S') ==="
  python3 judge_mush.py "models/$slug/composition" || echo "JUDGE FAILED"
done

for slug in gemma4-26b gemma4-31b-it-qat huihui-ai-pi-100k medgemma-27b \
            orcarouter-Qwen3.8-27B-Uncensored-latest qwen3.8-latest; do
  python3 build_mush_report.py --model-dir "models/$slug/composition" \
    --think off >/dev/null 2>&1
  python3 build_mush_report.py --model-dir "models/$slug/composition" \
    --think low >/dev/null 2>&1
  python3 build_mush_report.py --model-dir "models/$slug/composition" \
    --think medium >/dev/null 2>&1
  python3 build_mush_report.py --model-dir "models/$slug/composition" \
    --think xhigh >/dev/null 2>&1
done

python3 build_mush_report.py --index
echo "=== REJUDGE v5 DONE $(date) ==="
