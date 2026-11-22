#!/usr/bin/env bash
# Прогон medgemma:27b — модель без thinking, одна серия (off).
# 5 прогонов × 8 клеток + судья low + отчёт + сводный индекс.
set -u
cd /home/dp/MyProjects/muhomoring-test

echo "=== V4 medgemma:27b · think=off · $(date '+%H:%M:%S') ==="
python3 runner_mush.py --model medgemma:27b --runs 5 --think off \
  || { echo "RUNNER FAILED"; exit 1; }
python3 judge_mush.py models/medgemma-27b/composition || echo "JUDGE FAILED"
python3 build_mush_report.py --model-dir models/medgemma-27b/composition \
  --think off || echo "REPORT FAILED"
python3 build_mush_report.py --index
echo "=== MEDGEMMA DONE $(date) ==="
