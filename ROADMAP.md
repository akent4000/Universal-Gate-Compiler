# Roadmap

Честный список известных слабостей проекта и путей их исправления, в порядке
приоритета. Старая версия (распределение задач по моделям Claude) устарела и
заменена этим документом.

Живой каталог всех pending-задач по фазам остаётся в [TODO.md](TODO.md).
Этот файл — мета-слой: **что именно сломано / недоделано / оверсолд** и
**как чинить**.

---

## P0 — Корректность и честность (блокеры доверия)

### 1. `dc --odc` не работает на reconvergent-fanout схемах (soundness gap)

**Симптом.** По результатам EPFL-прогона (V2.d) safety-net miter стабильно
срабатывает на `router` (n_inputs=60), `priority` (128), `i2c` (147),
`sin` (24). Каждый revert стирает весь проход — на этих схемах
`dc --odc` даёт 0% выигрыша. Подъём `sim-W` до 16384 не помогает →
это теоретический зазор в V2 admissibility check, а не coverage.

**Видимость для пользователя сейчас.** Шаги 1–2 выполнены: дефолтный скрипт
теперь `"rewrite; fraig; rewrite; balance"` (без `dc`), а `dc --odc` на AIG
с `n_inputs > 20` печатает явный warning в stderr. Пользователь, запускающий
умолчания, больше не получает silent no-op; явный вызов `dc` остаётся
доступен через `--script`.

**Путь исправления (в порядке возрастания стоимости):**
1. ✅ **Выполнено:** `dc` убран из `DEFAULT_SCRIPT`
   ([nand_optimizer/script.py](nand_optimizer/script.py)); остался
   `"rewrite; fraig; rewrite; balance"`. Флаг `--script "... dc ..."`
   продолжает работать явно.
2. ✅ **Выполнено:** `dc_optimize()` с `use_odc=True` и `n_inputs > 20`
   печатает в stderr `WARN: dc --odc has known soundness gap on
   reconvergent-fanout circuits (n_inputs=... > 20); see ROADMAP.md P0#1.`
   ровно один раз за вызов (см. `_ODC_WARN_DEPTH` в
   [nand_optimizer/dont_care.py](nand_optimizer/dont_care.py)).
3. ✅ **Выполнено:** minimal revert cone извлечён из `router`
   (outport[1]) через delta-debug с constant-substitution внутренних AND-узлов:
   [tests/fixtures/router_outport1_minimal.aig](tests/fixtures/router_outport1_minimal.aig)
   — **14 ANDs / 15 inputs / 1 output** (29 node ids). Revert
   воспроизводится при W ∈ {128, 1024, 16384}, а `dc` без `--odc` на
   том же входе работает корректно. Regression-тест:
   [tests/test_dc_odc_soundness.py](tests/test_dc_odc_soundness.py)
   (3 инварианта + «фикстура обязана ревертить» как live-сигнал, что
   фикс ещё не landed).
4. ✅ **Выполнено (V3 — z3-exact admissibility):** Корень проблемы —
   not care underestimation, but **admissibility coverage**: при 2^60 PI
   паттернов sim-based check (W=128) пропускает плохие шаблоны. Реализован
   `odc_mode='z3-exact'` в [dont_care.py](nand_optimizer/dont_care.py) —
   для каждого template и каждой resub-операции выполняется точная Z3-проверка
   `UNSAT(ODC_v AND T(cut_old) ≠ old_v)`, где `ODC_v` вычисляется через
   `z3.substitute` (кэшируется на узел — O(n_nodes × n_POs × substitute_cost)
   один раз, потом O(SAT) per check). Результаты:

   | benchmark | n_ands | no-odc | z3-exact | reverts |
   |-----------|--------|--------|----------|---------|
   | router    | 257    | -3.5%  | -2.3%    | 0 ✓    |
   | priority  | 978    | -8.0%  | **-21.9%** | 0 ✓  |
   | i2c       | 1342   | -2.2%  | -0.1%    | 0 ✓    |
   | sin       | 5416   | TBD    | TBD      | TBD    |

   Скорость: 4–27s на benchmark против 0.2–2.3s legacy (10–15× overhead
   из-за Z3 per-template). Флаги: `dc --odc --odc-mode z3-exact`.
   Regression: [tests/test_dc_odc_soundness.py](tests/test_dc_odc_soundness.py)
   (test_z3_exact_no_revert, test_z3_exact_circuit_equivalence).
5. ✅ **Выполнено (V3 — window-local ODC):** `odc_mode='window'` реализует
   forward-flip per-node симуляцию в K-уровневом fanout-окне
   ([dont_care.py: _propagate_care_sim_window](nand_optimizer/dont_care.py)).
   Работает на фикстуре (depth=1: reverts=0 при исчерпывающем W=32768), но
   на полных EPFL-схемах (60-147 PI) сохраняет reverts из-за coverage-проблемы
   admissibility check. Полезен для малых схем как более консервативная
   альтернатива; флаги: `dc --odc --odc-mode window --window-depth K`.

**Статус (шаги 1–5 выполнены):** `dc --odc --odc-mode z3-exact` даёт:

| bench    | legacy rev | z3-exact rev | z3-exact area | z3-exact time |
|----------|-----------|--------------|---------------|---------------|
| router   | 1         | **0** ✓     | -2.3%         | 4.5s          |
| priority | 1         | **0** ✓     | **-21.9%** ✓  | 26s           |
| i2c      | 1         | **0** ✓     | -0.1%         | 27s           |
| sin      | 1         | 1 (>3000 ANDs → legacy fallback) | 0% | 58s |

Criterion ≥5% выполнен на priority (-21.9% vs baseline -8%). router и i2c
дают скромный выигрыш; sin (5416 ANDs) превышает threshold n_ands≤3000 и
автоматически падает в legacy. Для production нужен дальнейший speed-up
(параллельный SAT, incremental formulas, lazy evaluation).

---

### 2. Нет CI и регрессионного snapshot-тестирования

**Симптом.** 13.7k LoC, 41 модуль, 0 автоматических gate'ов. T1–T10 запускаются
только внутри `optimize()` по явному вызову. `proptest` и EPFL CEC — ручные.
При таком количестве проходов регрессия при любой правке почти гарантирована.

**Путь исправления:**
1. **Создать `tests/`:** перенести T1–T10 в pytest-совместимые модули
   (`test_truth_table.py`, `test_aig.py`, `test_fsm.py`, `test_nand.py`).
2. **QoR snapshot:** закоммитить `benchmarks/qor_baseline.json` с эталонными
   NAND counts для `rd53, mult3, mult4, misex1, parity9, z4ml, 7seg, adder,
   excess3`. Тест-функция сравнивает текущий `optimize()` и падает при
   регрессии > 5%.
3. **`.github/workflows/ci.yml`:**
   - matrix `python-version: [3.9, 3.11]`
   - `pytest`
   - `python -m nand_optimizer proptest --cases 50`
   - `python -m nand_optimizer epfl --subset arithmetic/adder,random_control/ctrl --no-verify`
   - проверка QoR snapshot
4. **Pre-commit hook:** `pytest -x` на изменённых модулях (опционально).

**Готово, когда:** PR, ломающий QoR на `mult3` > 5%, блокируется CI.

---

## P1 — Архитектурный долг

### 3. Плоский пакет из 41 файла

**Симптом.** Навигация между слоями (I/O, synthesis passes, sequential,
testing) требует знать все имена модулей. `CLAUDE.md` вынужден держать
таблицу «какой модуль за что отвечает» именно потому, что иерархии нет.

**Путь исправления.** Выполнить Phase 6 из [TODO.md](TODO.md#L108) одним
механическим коммитом:
- `nand_optimizer/core/` — `aig.py, expr.py, truth_table.py, implicant.py`
- `nand_optimizer/synthesis/` — `rewrite.py, fraig.py, balance.py, decomposition.py, dont_care.py, exact_synthesis.py, optimize.py, bidec.py, bdd_decomp.py, sat_resub.py`
- `nand_optimizer/mapping/` — `nand.py, circ_export.py`
- `nand_optimizer/io/` — `aiger_io.py, blif_io.py, verilog_io.py, dot_export.py`
- `nand_optimizer/sequential/` — `fsm.py`
- `nand_optimizer/datapath/` — `structural.py, datapath.py`
- `nand_optimizer/analysis/` — `sta.py, switching.py, atpg.py`
- `nand_optimizer/testing/` — `tests.py, property_tests.py, benchmark_runner.py, epfl_bench.py, profile.py`
- оркестраторы наверху: `pipeline.py, script.py, verify.py, __init__.py, __main__.py`

**Риск:** bootstrap `precompute_4cut.py` использует env-guard
(`_NAND_OPTIMIZER_BOOTSTRAPPING=1`) и subprocess — после переезда нужно
проверить, что guard всё ещё ловит все пути импорта.

**Готово, когда:** все `from nand_optimizer import ...` работают, `python -m
nand_optimizer` не тормозит на bootstrap, CLAUDE.md обновлён.

---

### 4. `aig_db_4.py` — 65k-строчный Python-файл как хранилище данных

**Симптом.** Grep по кодовой базе спотыкается о БД; IDE открывает её как
source и лезет его парсить. Это данные, не код.

**Путь исправления.**
1. В `precompute_4cut.py` сериализовать результат в `aig_db_4.pkl`
   (`pickle.dump` или `numpy.savez` — 16-bit индексы templates влезают в
   uint16-массивы).
2. В `rewrite.py` / `__init__.py` лениво грузить через `pickle.load` при
   первом обращении.
3. Удалить генерацию `.py`-файла; `.gitignore` обновить.

**Готово, когда:** `wc -l nand_optimizer/*.py` не содержит 65k-строчного
файла; bootstrap время и lookup-латентность не выросли.

---

### 5. Python-перформанс не измерен

**Симптом.** Заявлено «для 20+ входов BDD / GPU» в Phase 4/4.5, но нет
базового cProfile-отчёта по текущему коду. Скорее всего есть «свободные»
ускорения через numpy, которые не сделаны.

**Путь исправления.**
1. **Profile baseline:** `python -m cProfile -o prof.out -m nand_optimizer mult4 --script "rewrite; fraig; rewrite; balance"`
   + snakeviz. Зафиксировать top-10 hotspots в `benchmarks/perf_baseline.md`.
2. **Numpy-векторизация FRAIG-симуляции:** сейчас в [fraig.py](nand_optimizer/fraig.py)
   сигнатуры — list of Python int. Перейти на `np.ndarray[uint64, (N_nodes, B_words)]`,
   операции AND/XOR батчем. Ожидание: 5–20× на симуляционной фазе.
3. **Только если numpy недостаточно:** рассмотреть Cython для AIG
   structural-hash lookup.

**Готово, когда:** `mult4` дефолтный прогон < 50% исходного wall-time;
baseline и текущие числа в repo.

---

## P2 — Depth-over-breadth

### 6. Verilog front-end заявлен, но subset не задокументирован

**Симптом.** 843 LoC нативного парсера — наверняка работает на узком
подмножестве, но внешне выглядит как «Verilog support».

**Путь исправления.**
1. В docstring [verilog_io.py](nand_optimizer/verilog_io.py) зафиксировать
   **явную грамматику subset**'а: какие конструкции парсер принимает, какие
   даёт явную ошибку. Что-то вроде:
   ```
   Supported:
     - module / endmodule, input, output, wire, assign
     - Primitives: and, or, nand, nor, xor, xnor, not, buf
     - Behavioural: always @(*) with if/else, case
   Not supported:
     - parameter, generate, for/while, task/function
     - Sequential always @(posedge clk) — use FSM front-end instead
     - $-functions, `-directives beyond simple `define
   ```
2. **Корпус тестов:** 10 файлов в `tests/verilog/` с expected
   pass/fail-диагностиками.
3. **Decision point** (в ROADMAP не решаем): либо инвестировать до ~80%
   покрытия реальных netlist'ов, либо deprecate и делегировать yosys→BLIF.

**Готово, когда:** пользователь, подающий unsupported-конструкцию, получает
конкретную ошибку `unsupported construct 'generate' at line N`, а не
silent-miscompile.

---

### 7. Bandit / auto-compose / BDD-rebuild / SAT-resub — QoR не измерены

**Симптом.** Каждый из этих проходов есть в коде и в README, но нет ответа
на вопрос «насколько лучше baseline на EPFL?». Риск breadth-over-depth.

**Путь исправления.** Для каждого прохода:
1. Прогнать EPFL subset с / без прохода, записать в
   `benchmarks/pass_eval.md` таблицу `(benchmark, baseline_area,
   with_pass_area, delta_%, wall_time_delta_%)`.
2. Если delta < 2% на всём subset — пометить проход `experimental` в
   `--help` и в README; рассмотреть удаление, если depth не планируется.
3. Если delta > 5% — описать в README, **на каких классах схем** проход
   выигрывает (arithmetic? control? dense SOPs?).

**Готово, когда:** ни один пасс в дефолтном скрипте не существует «на веру».

---

## P3 — Следующий реальный unlock

### 8. XAG (XOR-AND Graph) как основное расширение AIG

**Мотивация.** AIG моделирует XOR как 3 AND'а с инверсиями. Structural
hashing не канонизирует их до одной формы → FRAIG и rewrite видят дубликаты,
которых быть не должно. Это самая очевидная причина отставания по QoR от ABC
на арифметике (adder/multiplier/sha). MIG — отдельная тема, не делать одновременно.

**Путь исправления (поэтапно):**
1. **XAG-lite в AIG:** добавить `XORNode(a, b)` как первоклассный узел с
   key = `('XOR', min(a^1, a), min(b^1, b))` (канонизация по полярности).
   Constant folding: `XOR(x, 0) = x`, `XOR(x, 1) = ~x`, `XOR(x, x) = 0`.
2. **FRAIG-симуляция:** add XOR as native op (дешевле чем 3×AND).
3. **Rewrite:** расширить `aig_to_gates` / `nand.py` detector — XOR-pattern
   напрямую из XAG-узла, а не reverse-engineering из AND-tree.
4. **NPN DB:** `aig_db_4.py` содержит XOR-heavy NPN классы как AND-trees;
   после XAG надо пересобрать DB с XOR-узлами для этих классов (меньше
   записей, меньше размер).

**Риск:** все существующие проходы (rewrite, balance, fraig) должны уметь
обрабатывать XOR-узел. Это **ломающий рефакторинг API AIG** — заложить
неделю работы минимум, плюс регрессионные T1–T10.

**Готово, когда:** EPFL `arithmetic/*` показывает 10–20% снижение AIG-узлов
по сравнению с чистым AIG-бэкендом.

---

### 9. MIG, GPU, ML — только после P0–P3

**Обоснование.** MIG (Phase 2.7), GPU-проходы (Phase 4.5), ML-guided
synthesis (Phase 8) — исследовательские направления. Браться за них имеет
смысл только когда:
- QoR baseline измерен и стабилен (пункт 8);
- нет известных soundness-дыр (пункт 1);
- есть CI, ловящий регрессии (пункт 2);
- перформанс baseline не упирается в очевидные python-потери (пункт 6).

До тех пор эти фазы в [TODO.md](TODO.md) остаются как research backlog,
но не должны приоритезироваться.

---

## Порядок исполнения

```
P0#1 (dc out of default)    ──┐
P0#2 (CI + QoR snapshot)    ──┘ база доверия
         │
         ▼
P1#3 (package layout)
P1#4 (aig_db as .pkl)
P1#5 (numpy FRAIG)
         │
         ▼
P2#6 (Verilog subset spec)
P2#7 (pass QoR eval)
         │
         ▼
P3#8 (XAG)
         │
         ▼
P3#9 (MIG / GPU / ML — по готовности)
```

Раздел README «Limitations» уже добавлен (см. [README.md](README.md)) —
пользователь сразу видит ограничения, а P0#1 закроет их фактически.
