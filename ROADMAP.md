# Roadmap

Мета-слой над [TODO.md](TODO.md): **что именно нужно сделать дальше** и
**почему это в таком порядке**. Полные описания завершённых пунктов —
в [TODO_done.md](TODO_done.md); каталог всех pending-задач по фазам —
в [TODO.md](TODO.md).

---

## Закрытая база доверия (для истории)

Блокеры "корректность и честность" (P0) и архитектурный долг (P1)
в основном сняты. Краткая сводка с анкерами, чтобы не ломать внешние
ссылки из README / тестов / модулей:

| id | что сделано | где смотреть |
|----|-------------|--------------|
| **P0#1** | `dc --odc` soundness gap закрыт через `odc_mode='z3-exact'` (priority −21.9%, router/i2c 0 revert'ов); `dc` убран из `DEFAULT_SCRIPT`; stderr-warning на `n_inputs>20` | [dont_care.py](nand_optimizer/synthesis/dont_care.py), [tests/test_dc_odc_soundness.py](tests/test_dc_odc_soundness.py) |
| **P0#2** | CI + QoR snapshot (pytest-обёртки T1–T13, [qor_baseline.json](benchmarks/qor_baseline.json) +5% толерантность, GitHub Actions py3.11/3.12, EPFL smoke) | [.github/workflows/ci.yml](.github/workflows/ci.yml) |
| **P1#3** | Плоский пакет разложен по 8 подпакетам (`core/`, `synthesis/`, `mapping/`, `io/`, `sequential/`, `datapath/`, `analysis/`, `testing/`) | [nand_optimizer/](nand_optimizer/) |
| **P1#4** | `aig_db_4.py` 65k LoC → binary pickle + 35-line lazy loader; `xag_db_4.pkl` добавлен | [aig_db_4.py](nand_optimizer/aig_db_4.py), [xag_db_4.py](nand_optimizer/xag_db_4.py) |
| **P1#5 (ч.1)** | cProfile baseline + bit-mask `Implicant` → `mult4` 15.8s → 6.9s (**2.3×**), QMC `can_combine` 4.9× | [benchmarks/perf_baseline.md](benchmarks/perf_baseline.md), [core/implicant.py](nand_optimizer/core/implicant.py) |
| **P2#7** | Pass QoR evaluation harness + [pass_eval.md](benchmarks/pass_eval.md): XAG −4.3%, bandit −15.4%, bdd/resub → experimental | [benchmarks/run_pass_eval.py](benchmarks/run_pass_eval.py) |
| **P3#8 (Phase 1–5)** | XAG (нативный XOR-узел во всех проходах) + `XAG_DB_4` (87% из 65 536 TT дешевле) + NAND-cost-aware comparator (`rewrite -x`): adder −24.7%, router −17.2%, sin −5.1%, 0 регрессий на 11 EPFL | [core/aig.py](nand_optimizer/core/aig.py), [synthesis/rewrite.py](nand_optimizer/synthesis/rewrite.py) |
| **P3#10** | SAT sweeping (ODC-aware FRAIG superset) — proc `sweep`, symbolic obs-builder + Z3 миттер, на 11 EPFL: adder −6.4%, i2c −0.8%, sin −0.1%, 8 ties, 0 регрессий; complementary к `dc --odc --odc-mode z3-exact` | [synthesis/sat_sweep.py](nand_optimizer/synthesis/sat_sweep.py), [tests/test_sat_sweep.py](tests/test_sat_sweep.py), [pass_eval.md §5](benchmarks/pass_eval.md) |

Все детали — в [TODO_done.md](TODO_done.md).

---

## P1 — Оставшийся перф-долг (опционально)

### P1#5 (ч.2) — Дальнейшая оптимизация Python

Шаги 1-2 дали заявленное `mult4 < 50% исходного wall-time`. Не блокирует
P2/P3, но всё ещё есть два дешёвых выигрыша:

1. **Мемоизация QMC в Ashenhurst bake-off.** На `mult4` `quine_mccluskey`
   вызывается 4698 раз — раз на каждый bipartition probe. Хэш по
   cube-cover tuple либо prune bipartition search до ре-синтеза.
   Потенциал: ещё **~2×** на синтезной части.
2. **Второй профиль** на EPFL `sin` (5416 ANDs) для оценки FRAIG.
   На `mult4` FRAIG <1% — numpy-FRAIG не окупается; нужны данные с
   действительно FRAIG-heavy схемы, прежде чем вкладываться.

Шаг 3 (Cython) оставлен как resort — только если шаги 1-2 окажутся
недостаточны.

---

## P2 — Depth-over-breadth

### P2#6 — Verilog front-end: subset не задокументирован

**Симптом.** [verilog_io.py](nand_optimizer/io/verilog_io.py) 843 LoC
нативного парсера, но извне выглядит как "Verilog support" без уточнений.

**Путь исправления:**
1. Docstring-грамматика subset'а (supported / not supported конструкции).
2. Корпус тестов `tests/verilog/` (10 файлов с expected pass/fail).
3. **Decision point:** инвестировать до ~80% покрытия real-world netlist'ов
   ИЛИ пометить deprecated и делегировать yosys→BLIF. Не решаем в
   ROADMAP — нужен сигнал от пользователей.

**Готово, когда:** неподдерживаемая конструкция даёт явную ошибку
`unsupported construct 'generate' at line N`, а не silent-miscompile.

---

## P3 — Реальные unlock'и по минимизации

Ранжированы по ожидаемому выигрышу `Δarea / LOC-effort`. Первые три
пункта — **практически готовые к старту**; 4-5 — research-tier.

### P3#8 — Phase 6: XOR-extractor-aware MFFC cost

**Почему сейчас.** Фазы 1-5 XAG дают −24.7% на adder и −17.2% на router,
но `use_xag=False` остаётся по умолчанию из-за единственной регрессии на
cube-cover built-in: `rd53` 40 → 45 NAND (+12%). Корень известен
([pass_eval.md:58-69](benchmarks/pass_eval.md)): локальная cost-модель
rewriter'а (AND=2 NAND) не знает, что post-mapping XOR-extractor в
[mapping/nand.py](nand_optimizer/mapping/nand.py) уже сжимает 3-AND
XOR-кластеры в 4-NAND. Поэтому rewriter "крадёт" 3 AND стоимостью 6 NAND
под native XOR (4 NAND), разрушая шэринг ниже по цепочке.

**Что сделать.** Пометить AND-узлы, участвующие в 3-AND XOR/XNOR паттерне,
как имеющие эффективную cost=4/3 вместо 2. Ограничение применять при
вычислении MFFC-cost в [`synthesis/rewrite.py`](nand_optimizer/synthesis/rewrite.py).
Достаточно статического pass'а "найти паттерны ANDs, которые XOR-extractor
свернёт", с кэшем на AIG.

**Готово, когда.** `use_xag=True` дефолт, `rd53` и остальные cube-cover
built-in T1–T13 не регрессируют, EPFL-выигрыши сохраняются.

**Бонус-пункт одной строкой:** добавить `'rewrite -x'` в
[`script.py:DEFAULT_ARMS`](nand_optimizer/script.py) — bandit
в EPFL-эвале сейчас оставляет adder −24.7% на столе
([pass_eval.md §4](benchmarks/pass_eval.md)).

---

### P3#9 — Structural choice nodes (ABC `compress2rs`-style)

**Крупнейший неисследованный алгоритмический рычаг** в пайплайне.

**Источник.** Mishchenko, Chatterjee, Brayton — "DAG-aware AIG rewriting"
(DAC 2006) и "Integrating Logic Synthesis, Technology Mapping, and
Retiming" (ICCAD 2006). ABC-скрипт `compress2rs` — это эталон, с которым
сравнивается любой индустриальный synthesis flow.

**Симптом / мотивация.** Текущий дефолт `rewrite; fraig; rewrite; balance` —
последовательно разрушающие проходы: каждый уничтожает структуру
предыдущего. Global bandit с h=20 даёт −15.4% именно потому, что ищет
"удачную" последовательность — но это brute-force обход фундаментальной
проблемы local destructive rewrite.

**Что сделать.** Для каждой функции хранить *несколько* альтернативных
AIG-реализаций, полученных разными проходами (snapshot'ы после baseline
/ balanced / rewritten / fraig), связанных через equivalence chain
(`choice_next: node_id → node_id`). На этапе cut-matching rewriter и
mapper выбирают лучшую альтернативу *per cut*, а не per global pass.

**Почему реалистично.** У вас уже есть:
- `AIG.snapshot()` / `restore()` — готовая инфраструктура для альтернатив.
- FRAIG equivalence-классы — готовый механизм пометки "эти два узла
  функционально равны".
- K-feasible cut enumeration в rewriter'е — готовый потребитель выбора.

**Оценка.** ~300-500 LOC в [core/aig.py](nand_optimizer/core/aig.py)
(поле `choice_next`, accessors, GC-safety) + новый proc `choice` в
[script.py](nand_optimizer/script.py) + расширение
cut-matching в [synthesis/rewrite.py](nand_optimizer/synthesis/rewrite.py).
Литературный ожидаемый выигрыш: **+10-25% поверх single-pass rewrite**
на индустриальных схемах.

**Готово, когда.** На EPFL subset (11 схем): mean Δarea лучше
`bandit (h=20)` при сопоставимом или меньшем wall-time.

---

### P3#11 — SPFD-based don't-cares

**Источник.** Yamashita, Sawada, Nagoya (ICCAD 1996); Mishchenko &
Brayton follow-ups. Sets of Pairs of Functions to be Distinguished —
**строго мощнее SDC+ODC**: не требует сохранения функции узла, а лишь
различения тех же пар входов, которые узел различает в текущей схеме.

**Мотивация.** `dc --odc --odc-mode z3-exact` уже даёт priority −21.9%
(P0#1), но на `sin` (5416 ANDs) не срабатывает — превышает
`n_ands≤3000` threshold и падает в legacy. SPFD по идее даёт +5-15% на
схемах, где ODC уже работает, плюс открывает тe схемы, где z3-exact
упирается в compute.

**Что сделать.** Либо BDD-бэкенд через `dd` (уже в зависимостях через
[synthesis/bdd_decomp.py](nand_optimizer/synthesis/bdd_decomp.py)), либо
аккуратная SAT-реализация по аналогии с z3-exact ODC. Новый
`odc_mode='spfd'` во флагах `dc`.

**Риск.** SPFD теоретически тяжелее ODC; если на практике получается
≥10× оверхед, оставить experimental.

---

### P3#12 — SAT-resub speed-up

`resub` уже даёт mean Δarea −6.1% (adder −12.4%, ctrl −6.6%), но 460×
медленнее baseline — запрещено включать в default
([pass_eval.md §3](benchmarks/pass_eval.md)).

**Три дешёвых хода:**
1. Incremental solver через `z3.Solver().push()/pop()` вместо
   пересоздания контекста на каждый cut.
2. Переиспользование learnt clauses между соседними cut'ами одного узла.
3. Sim-based фильтрация divisor-pool *перед* SAT — отбрасывать пары,
   чьи симуляционные сигнатуры не могут функционально покрыть cut.

**Цель.** 10-30× speedup; после этого resub входит в default с −5-10%
mean area.

---

### P3#13 — MIG, GPU, ML — research backlog

Браться только когда P3#8 (Phase 6) и P3#9 (choice nodes) landed
(P3#10 уже закрыт). До тех пор — в [TODO.md](TODO.md#фаза-47-gpu-ускорение)
как research-рельсы:

- **MIG (Majority-Inverter Graph)** — альтернативный primitive для
  арифметики. Реалистичный выигрыш: +10-20% на `max` / `bar` EPFL-схемах,
  которые в XAG-фазе не сдвинулись. Стоимость — недели (parallel AIG-style
  infra).
- **GPU-FRAIG / GPU-rewrite** ([TODO.md Фаза 4.5](TODO.md)) — трогать
  только после повторного профиля на FRAIG-heavy схеме.
- **ML-guided synthesis** ([TODO.md Фаза 8](TODO.md)) — RL script-control,
  GNN cut ranking, learned FRAIG signatures. Требует накопленного корпуса
  логов синтеза и QoR-датасета.

---

## Порядок исполнения

```
✅ P0#1 (dc soundness)        ──┐
✅ P0#2 (CI + QoR snapshot)    ──┤ база доверия закрыта
✅ P1#3 (package layout)       ──┤
✅ P1#4 (aig_db as .pkl)       ──┤
✅ P1#5 ч.1 (QMC bitmask)      ──┤
✅ P2#7 (pass QoR eval)        ──┤
✅ P3#8 Phase 1-5 (XAG)        ──┤
✅ P3#10 (SAT sweeping)        ──┘
                │
                ▼
         P3#8 Phase 6 (XOR-aware MFFC)  ← next, дни работы
                │
                ▼
         P3#9 Structural choice nodes    ← самый крупный unlock
                │
                ▼
         P3#11 SPFD DC   +   P3#12 SAT-resub speedup
                │
                ▼
         P3#13 MIG / GPU / ML (research backlog)

Параллельно, не блокирует:
   P1#5 ч.2 (QMC memoization)
   P2#6 (Verilog subset spec или deprecate)
```

Раздел README «Limitations» уже отражает текущие ограничения.
P0#1 и P0#2 закрыли доверительную базу: `dc --odc --odc-mode z3-exact`
устраняет revert'ы на reconvergent-fanout, а CI + QoR snapshot блокирует
PR-ы с регрессией > 5%.
