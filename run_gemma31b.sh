#!/usr/bin/env bash
# Полный прогон v4 для gemma4:31b-it-qat: 4 уровня × 5 прогонов × 8 клеток.
# После каждого уровня — судья (glm-5.3-flash low) и отчёт уровня.
set -u
cd /home/dp/MyProjects/muhomoring-test

model="gemma4:31b-it-qat"
slug="gemma4-31b-it-qat"
for think in off low medium xhigh; do
  echo "=== V4 $model · think=$think · $(date '+%H:%M:%S') ==="
  python3 runner_mush.py --model "$model" --runs 5 --think "$think" \
    || { echo "RUNNER FAILED, продолжаю"; continue; }
  python3 judge_mush.py "models/$slug/composition" || echo "JUDGE FAILED"
  python3 build_mush_report.py --model-dir "models/$slug/composition" \
    --think "$think" || echo "REPORT FAILED"
done

python3 build_mush_report.py --index
echo "=== GEMMA31B DONE $(date) ==="
