#!/usr/bin/env bash
# Полный цикл v7: промпт v4.3 («Перечисли основные химические
# соединения гриба») + структурированный вывод (format = JSON-схема,
# maxItems 100) +
# судья v2 (строгий: только вхождение в переданный состав, опечатки =
# галлюцинация). 5 моделей × 4 уровня × 5 прогонов; medgemma:27b — без
# thinking, один уровень off. После каждого уровня — судья и отчёт.
set -u
cd /home/dp/MyProjects/muhomoring-test

run_model () {  # $1 = model tag, $2 = slug, $3+ = уровни
  local model="$1" slug="$2"; shift 2
  for think in "$@"; do
    echo "=== V7 $model · think=$think · $(date '+%H:%M:%S') ==="
    python3 runner_mush.py --model "$model" --runs 5 --think "$think" \
      || { echo "RUNNER FAILED, продолжаю"; continue; }
    python3 judge_mush.py "models/$slug/composition" || echo "JUDGE FAILED"
    python3 build_mush_report.py --model-dir "models/$slug/composition" \
      --think "$think" || echo "REPORT FAILED"
  done
}

run_model "gemma4:26b" "gemma4-26b" off low medium xhigh
run_model "gemma4:31b-it-qat" "gemma4-31b-it-qat" off low medium xhigh
run_model "huihui-ai:pi-100k" "huihui-ai-pi-100k" off low medium xhigh
run_model "medgemma:27b" "medgemma-27b" off
run_model "qwen3.8:latest" "qwen3.8-latest" off low medium xhigh
run_model "orcarouter/Qwen3.8-27B-Uncensored:latest" \
  "orcarouter-Qwen3.8-27B-Uncensored-latest" off low medium xhigh

python3 build_mush_report.py --index
echo "=== V7 FULL DONE $(date) ==="
