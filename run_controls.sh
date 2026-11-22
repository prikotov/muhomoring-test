#!/usr/bin/env bash
# Добор контрольной группы (ARUB, ACAE — съедобные): 2 модели × 4 уровня × 5 прогонов.
# После каждого уровня — судья (переиспользует оценённые клетки) и отчёт уровня.
set -u
cd /home/dp/MyProjects/muhomoring-test

for model in "huihui-ai:pi-100k" "gemma4:26b"; do
  slug=$(echo "$model" | tr ':' '-')
  for think in off low medium xhigh; do
    echo "=== CONTROLS $model · think=$think · $(date '+%H:%M:%S') ==="
    python3 runner_mush.py --model "$model" --runs 5 --think "$think" \
      --only ARUB,ACAE || { echo "RUNNER FAILED, продолжаю"; continue; }
    python3 judge_mush.py "models/$slug/composition" || echo "JUDGE FAILED"
    python3 build_mush_report.py --model-dir "models/$slug/composition" \
      --think "$think" || echo "REPORT FAILED"
  done
done

python3 build_mush_report.py --index
echo "=== CONTROLS DONE $(date) ==="
