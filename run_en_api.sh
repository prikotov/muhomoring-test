#!/usr/bin/env bash
# EN-добор API-моделей: объёмы и уровни размышлений 1:1 с RU-сериями
# (решение владельца «в en столько же прогонов, сколько в ru»).
# deepseek/mimo — off×5, kimi/preview/qwen4-max — low×5, gemini/sol — low×2.
# Эталон ethalon-en.json, дерево models-en/. OpenRouter — через прокси.
# Раннер продолжает нумерацию с первого свободного run-XX.
set -u
cd /home/dp/MyProjects/muhomoring-test

QE="questions-composition-en.json"
EE="ethalon-en.json"
PROXY=$(cat ~/.config/models-research/proxy)

en_run () {  # $1 provider, $2 model, $3 slug, $4 think, $5 runs
  local provider="$1" model="$2" slug="$3" think="$4" runs="$5"
  echo "=== EN $model · think=$think · runs=$runs · $(date '+%H:%M:%S') ==="
  python3 runner_deepseek.py --provider "$provider" --model "$model" \
    --think "$think" --runs "$runs" --questions "$QE" \
    --out "models-en/$slug/composition" \
    || { echo "RUNNER FAILED"; return; }
  python3 judge_mush.py --ethalon "$EE" "models-en/$slug/composition" \
    || echo "JUDGE FAILED"
  python3 build_mush_report.py --model-dir "models-en/$slug/composition" \
    --think "$think" --ethalon "$EE" --questions "$QE" \
    --report-name report-en.html || echo "REPORT FAILED"
}

en_run deepseek-flash deepseek-flash deepseek-flash off 5
en_run xiaomi-token-plan-sgp mimo-v2.6-pro mimo-v2.6-pro off 5
en_run kimi-coding k3.1-pro kimi-k3.1-pro low 5
en_run qwen-token-plan-individual qwen4-max-preview qwen4-max-preview low 5
en_run qwen-token-plan-individual qwen4-max qwen4-max low 5
en_run openrouter google/gemini-4-neon gemini-4-neon low 2
en_run openrouter openai/gpt-6.2-sol gpt-6.2-sol low 2

python3 build_mush_report.py --index --models-root models-en \
  --ethalon "$EE" --questions "$QE" --report-name report-en.html \
  --alt-report report.html
python3 build_mush_report.py --index --alt-report report-en.html
python3 compare-ru-en.py >/dev/null || echo "COMPARE FAILED"
echo "=== EN-API FULL DONE $(date) ==="
