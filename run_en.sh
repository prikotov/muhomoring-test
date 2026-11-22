#!/usr/bin/env bash
# EN-цикл: вопросы и названия грибов на английском (questions-composition-en.json),
# эталон ethalon-en.json, судья v2, format-схема. Дерево моделей: models-en/.
# 5 моделей × 4 уровня × 5 прогонов; medgemma:27b — только off.
# В конце: EN-сводный отчёт report-en.html, RU-index получает ссылку на него,
# и генерируется страница сравнения compare-ru-en.html.
set -u
cd /home/dp/MyProjects/muhomoring-test

QE="questions-composition-en.json"
EE="ethalon-en.json"

run_model () {  # $1 = model tag, $2 = slug, $3+ = уровни
  local model="$1" slug="$2"; shift 2
  for think in "$@"; do
    echo "=== EN $model · think=$think · $(date '+%H:%M:%S') ==="
    python3 runner_mush.py --model "$model" --runs 5 --think "$think" \
      --questions "$QE" --out "models-en/$slug" \
      || { echo "RUNNER FAILED, продолжаю"; continue; }
    python3 judge_mush.py --ethalon "$EE" "models-en/$slug/composition" \
      || echo "JUDGE FAILED"
    python3 build_mush_report.py --model-dir "models-en/$slug/composition" \
      --think "$think" --ethalon "$EE" --questions "$QE" \
      --report-name report-en.html || echo "REPORT FAILED"
  done
}

run_model "gemma4:26b" "gemma4-26b" off low medium xhigh
run_model "gemma4:31b-it-qat" "gemma4-31b-it-qat" off low medium xhigh
run_model "huihui-ai:pi-100k" "huihui-ai-pi-100k" off low medium xhigh
run_model "medgemma:27b" "medgemma-27b" off
run_model "qwen3.8:latest" "qwen3.8-latest" off low medium xhigh
run_model "orcarouter/Qwen3.8-27B-Uncensored:latest" \
  "orcarouter-Qwen3.8-27B-Uncensored-latest" off low medium xhigh

python3 build_mush_report.py --index --models-root models-en \
  --ethalon "$EE" --questions "$QE" --report-name report-en.html \
  --alt-report report.html
# RU-index пересобираем с обратной ссылкой на EN-серию
python3 build_mush_report.py --index --alt-report report-en.html
python3 compare-ru-en.py || echo "COMPARE FAILED"
echo "=== EN FULL DONE $(date) ==="
