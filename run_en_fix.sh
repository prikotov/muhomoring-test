#!/usr/bin/env bash
# Дострелять пострадавших EN: deepseek (баг провайдера), mimo (чужой think),
# gemini+sol (не экспортирован прокси). Объёмы 1:1 с RU.
set -u
cd /home/dp/MyProjects/muhomoring-test
QE="questions-composition-en.json"; EE="ethalon-en.json"
PROXY=$(cat ~/.config/models-research/proxy)

en_run () { # provider model slug think runs
  local provider="$1" model="$2" slug="$3" think="$4" runs="$5"
  echo "=== EN-FIX $model · $think × $runs · $(date '+%H:%M:%S') ==="
  python3 runner_deepseek.py --provider "$provider" --model "$model" \
    --think "$think" --runs "$runs" --questions "$QE" \
    --out "models-en/$slug/composition" || { echo "RUNNER FAILED"; return; }
  python3 judge_mush.py --ethalon "$EE" "models-en/$slug/composition" || echo "JUDGE FAILED"
  python3 build_mush_report.py --model-dir "models-en/$slug/composition" \
    --think "$think" --ethalon "$EE" --questions "$QE" \
    --report-name report-en.html || echo "REPORT FAILED"
}

en_run deepseek deepseek-flash deepseek-flash off 5
en_run xiaomi-token-plan-sgp mimo-v2.6-pro mimo-v2.6-pro off 5
PROXY="$PROXY" HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" \
  python3 runner_deepseek.py --provider openrouter --model google/gemini-4-neon \
  --think low --runs 2 --questions "$QE" --out models-en/gemini-4-neon/composition \
  || echo "RUNNER FAILED"
python3 judge_mush.py --ethalon "$EE" models-en/gemini-4-neon/composition || echo "JUDGE FAILED"
python3 build_mush_report.py --model-dir models-en/gemini-4-neon/composition \
  --think low --ethalon "$EE" --questions "$QE" --report-name report-en.html || echo "REPORT FAILED"
PROXY="$PROXY" HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" \
  python3 runner_deepseek.py --provider openrouter --model openai/gpt-6.2-sol \
  --think low --runs 2 --questions "$QE" --out models-en/gpt-6.2-sol/composition \
  || echo "RUNNER FAILED"
python3 judge_mush.py --ethalon "$EE" models-en/gpt-6.2-sol/composition || echo "JUDGE FAILED"
python3 build_mush_report.py --model-dir models-en/gpt-6.2-sol/composition \
  --think low --ethalon "$EE" --questions "$QE" --report-name report-en.html || echo "REPORT FAILED"

python3 build_mush_report.py --index --models-root models-en \
  --ethalon "$EE" --questions "$QE" --report-name report-en.html --alt-report report.html
python3 build_mush_report.py --index --alt-report report-en.html
python3 compare-ru-en.py >/dev/null || echo "COMPARE FAILED"
echo "=== EN-FIX DONE $(date) ==="
