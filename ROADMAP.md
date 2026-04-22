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

**Видимость для пользователя сейчас.** Нулевая. Дефолтный скрипт
`"rewrite; fraig; dc; rewrite; balance"` молча выполняет no-op на hard inputs.

**Путь исправления (в порядке возрастания стоимости):**
1. **Немедленно:** убрать `dc` из дефолтного скрипта до починки; оставить
   `"rewrite; fraig; rewrite; balance"`. Флаг `--script "... dc ..."` продолжает
   работать явно.
2. **Добавить warning:** при `dc --odc` на AIG с `n_inputs > 20` печатать
   `WARN: --odc has known soundness gap on reconvergent-fanout circuits; see ROADMAP.md`.
3. **Построить minimal revert cone** (≤50 узлов из `router`) — как regression
   fixture. Без этого любая «починка» V2 будет вслепую.
4. **Починить `_propagate_care_sim`** ([dont_care.py:99](nand_optimizer/dont_care.py)) —
   edge-signal drift после upstream-переписываний; см. гипотезу (a) в
   [TODO.md:36](TODO.md#L36).
5. **Альтернатива — window-local DC** (гипотеза (d)): вычислять ODC только в
   bounded window вокруг узла. Дороже per-node, но без reconvergence-рисков.

**Готово, когда:** EPFL-прогон `dc -r 3 --odc` на четырёх проблемных схемах
даёт ≥ 0 ревертов и ≥ 5% площади по сравнению с `dc` без `--odc`.

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
1. **Profile baseline:** `python -m cProfile -o prof.out -m nand_optimizer mult4 --script "rewrite; fraig; dc; rewrite; balance"`
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
