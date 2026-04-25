
#  Дорожная карта развития Universal-Gate-Compiler (EDA Synthesizer)

Этот документ описывает эволюцию компилятора логики от базового оптимизатора до масштабируемого промышленного инструмента логического синтеза. Задачи сгруппированы по логическим фазам развития.

**Только активные `[ ]` задачи.** Полные описания завершённых пунктов — в [TODO_done.md](TODO_done.md).

---

## Фаза 1: Инфраструктура, Бенчмарки и Надежность
*Надежный фундамент критически важен, так как сложные оптимизации на графах (AIG) очень легко ломают логику схемы.*

Завершено: MCNC-бенчмарки, miter-верификация через Z3, property-based testing (Hypothesis), профилирование пайплайна, **pytest-обёртки для T1–T13 + QoR snapshot (benchmarks/qor_baseline.json, +5% tolerance) + GitHub Actions CI (py3.9/3.11, pytest → proptest-50 → EPFL smoke)** — закрывает ROADMAP P0#2. См. [TODO_done.md](TODO_done.md#фаза-1-инфраструктура-бенчмарки-и-надежность).

---

## Фаза 2: Булевский и Структурный Синтез
*Текущий конвейер требует усиления математической базы для преодоления порога в ~300 гейтов на сложных бенчмарках (напр. BCD-to-7seg).*

Завершено: multi-output Espresso, Brayton kernel extraction, Ashenhurst-Curtis / Roth-Karp декомпозиция, SAT-based exact synthesis, рекурсивная Ashenhurst-Curtis, shared-support multi-output декомпозиция, bi-decomposition (AND/OR/XOR, команда `bidec`), BDD-guided rebuild через sifting (команда `bdd`), SAT-style functional resubstitution для k=5..7 cut'ов (команда `resub`), auto-composition симметричных output-групп (`--auto-compose`, `auto_compose.py`) + hierarchical multi-stage pipeline (`hierarchical_optimize`, `--compose`). См. [TODO_done.md](TODO_done.md#фаза-2-булевский-и-структурный-синтез).

---

## Фаза 2.5: Технический долг и исправление маппинга (Hotfixes)

Завершено: fanout-aware rewriter с MFFC cost model, bubble pushing AIG→NAND, AIG garbage collection, прямая генерация AIG без AST-оверхеда, извлечение XOR/XNOR-структур. См. [TODO_done.md](TODO_done.md#фаза-25-технический-долг-и-исправление-маппинга).

---

## Фаза 2.7: Продвинутые манипуляции с графами (Advanced AIG)
*Инструментарий промышленного качества для работы с AIG.*

Завершено: FRAIGing, AIG balancing, synthesis scripts (`balance; rewrite; fraig`), Graphviz export, precomputed 4-input NPN AIG database, Don't-Care V1 (SDC), Don't-Care V2 (полный Mishchenko 2009: sim-based ODC propagation + window resubstitution + DC-aware exact synthesis + multi-round iterative refinement), `care_rounds_internal` как флаг `dc -C N`, V2.d EPFL-инструментация + adaptive-W + fix exhaustive-enum, Multi-Armed Bandit контроллер `ScriptBandit` (UCB1 + Thompson Sampling) с CLI `--bandit HORIZON`, Don't-Care V3 (z3-exact per-cut admissibility → 0 reverts на router/priority/i2c; window-local ODC; topology-aware 1-gate resub; regression fixtures в [tests/](tests/)), **XAG (XOR-AND Graph): нативный XOR-узел во всех проходах пайплайна + `XAG_DB_4` NPN-шаблоны (AND+XOR), 87% из 65 536 TT дешевле в XAG**, **XAG Phase 5: NAND-cost-aware comparator в `rewrite_aig` (AND=2, XOR=4 NAND), флаг `rewrite -x`, EPFL adder −24.7%, router −17.2%, sin −5.1%, 0 регрессий на 11 EPFL — см. [pass_eval.md](benchmarks/pass_eval.md), ROADMAP P3#8**, **Pass QoR evaluation harness ([benchmarks/run_pass_eval.py](benchmarks/run_pass_eval.py)) + [pass_eval.md](benchmarks/pass_eval.md): bdd/resub помечены experimental, ROADMAP P2#7 закрыт**, **SAT sweeping (ROADMAP P3#10): proc `sweep` в [`synthesis/sat_sweep.py`](nand_optimizer/synthesis/sat_sweep.py) — ODC-aware FRAIG superset, на 11 EPFL adder −6.4% (единственный pass, который превосходит FRAIG на сумматоре), 0 регрессий, complementary к `dc --odc --odc-mode z3-exact`; regression-suite в [tests/test_sat_sweep.py](tests/test_sat_sweep.py)**, **Structural choice nodes (ROADMAP P3#9): `AIG._choice_next` linked list + proc `choice` + флаг `rewrite -c`, variants объединяются через `AIG.compose()`, cross-variant equivalences SAT-верифицируются через Z3-miter перед `add_choice`; choice-aware cut matching в [synthesis/rewrite.py](nand_optimizer/synthesis/rewrite.py); regression-suite в [tests/test_choice_nodes.py](tests/test_choice_nodes.py)**. См. [TODO_done.md](TODO_done.md#фаза-27-продвинутые-манипуляции-с-графами).
* [ ] **XAG Phase 6 — XOR-extractor-aware MFFC cost:** * Унифицировать локальную cost-модель rewriter'а с post-mapping XOR-extractor в [`mapping/nand.py`](nand_optimizer/mapping/nand.py): тегировать AND-узлы, входящие в 3-AND XOR/XNOR-паттерн, как cost=4/3 вместо 2. Это закрывает единственную регрессию включения XAG по умолчанию (`rd53` 40 → 45 NAND на Espresso-входах), после чего `use_xag=True` становится default. Ожидаемый эффект на смеси EPFL+MCNC: сохранить `adder −24.7%`, `router −17.2%`, `sin −5.1%` без opt-in и без регрессий на cube-cover built-ins. См. [pass_eval.md §1](benchmarks/pass_eval.md), [ROADMAP.md P3#8 "Оставшееся"](ROADMAP.md).
* [ ] **Добавить `rewrite -x` в `DEFAULT_ARMS` bandit'а:** * [`script.py:DEFAULT_ARMS`](nand_optimizer/script.py) сейчас `['balance', 'rewrite', 'rewrite -z', 'fraig', 'dc']`; bandit в EPFL-эвале даёт +0.0% на adder и оставляет XAG-win −24.7% на столе при общем mean −15.4%. Добавление одной строки расширяет combined XAG ∪ bandit выигрыш на арифметику. См. [pass_eval.md §4](benchmarks/pass_eval.md#L148).
* [ ] **SPFD-based don't-cares (Sets of Pairs of Functions to be Distinguished):** * Yamashita, Sawada, Nagoya (ICCAD 1996); позже Mishchenko & Brayton. Строго мощнее SDC+ODC: не требует сохранения функции узла, а лишь различения тех же пар пар-входов, на которых она различает. После `dc --odc --odc-mode z3-exact` (priority −21.9%, [ROADMAP.md P0#1](ROADMAP.md)) это следующий level-up на reconvergent-fanout схемах; снимает revert'ы на оставшихся (sin 5416 ANDs, которую z3-exact не тянет из-за threshold). Требует BDD-бэкенда или аккуратной SAT-реализации; `dd` уже в зависимостях через [`synthesis/bdd_decomp.py`](nand_optimizer/synthesis/bdd_decomp.py). Ожидаемый эффект: +5-15% на схемах, где ODC уже работает, покрытие sin/i2c/large.
* [ ] **SAT-resub speed-up (incremental Z3, clause reuse, sim-prefilter):** * Сейчас `resub` даёт mean Δarea −6.1% (adder −12.4%, ctrl −6.6%), но 460× медленнее baseline — запрещено включать в default ([pass_eval.md §3](benchmarks/pass_eval.md)). Ходы: (a) incremental solver через `z3.Solver().push()/pop()` вместо пересоздания контекста на каждый cut; (b) переиспользование learnt clauses между соседними cut'ами одного узла; (c) sim-based фильтрация divisor-pool *перед* SAT (отбрасывать пары, чьи симуляционные сигнатуры не могут функционально покрыть cut). Цель: 10-30× speedup, после чего resub входит в default с −5-10% mean area.
* [ ] **MIG (Majority-Inverter Graph):** * Альтернативное представление через 3-входовый мажоритарный элемент MAJ(a,b,c) и инверсии. Аксиоматика MIG (commutativity, associativity, distributivity, inverter propagation) даёт более компактное представление сумматоров и умножителей, чем AIG. Аспирационно — полезно как экспериментальный бэкенд для арифметических бенчмарков. Ссылка: Amarú et al., "Majority-Inverter Graph: A New Paradigm for Logic Optimization" (TCAD 2016).

---

## Фаза 3: Синтез последовательностной логики (FSM / RTL)
*Превращение проекта из простого комбинаторного оптимизатора в полноценный компилятор конечных автоматов и регистровых передач.*

Завершено: `StateTable` + KISS2 парсер, D-FF и JK-FF примитивы, state encoding (binary/onehot/gray), генерация excitation logic, разрыв комбинаторных циклов, минимизация completely- и incompletely-specified FSM, асинхронный reset/preset как отдельный control-tract, Bounded Model Checking (BMC). См. [TODO_done.md](TODO_done.md#фаза-3-синтез-последовательностной-логики-fsm--rtl).

---

## Фаза 3.5: Структурный синтез RTL-блоков (Datapath Library)
*Для схем с $>20$ входов, где `TruthTable` из $2^N$ минтермов нереализуема физически (память/время). Обходим логический синтез через таблицу истинности: строим AIG сразу из RTL-описания, а существующие проходы `rewrite; fraig; balance` доводят его до минимума.*

Завершено: `structural.py` (StructuralModule), `datapath.py` (параметрические блоки), `examples/jk_counter.py` (8-bit JK-счётчик, 248 NAND, 256×4 регрессий). См. [TODO_done.md](TODO_done.md#фаза-35-структурный-синтез-rtl-блоков-datapath-library).

* [ ] **Интеграция structural-блоков с FSM-пайплайном:** В `synthesize_fsm()` добавить режим `backend={'truth_table'|'structural'}` и callback `next_state_fn: Callable[[StructuralModule, Dict[str, lit]], Dict[str, lit]]`. Пользователь описывает функцию перехода структурно (через datapath-блоки), а FSM-фреймворк оборачивает её flip-flop-ами, reset/enable-трактами и feedback-связями. Разблокирует «Verilog-подобное» описание схем, не влезающих в таблицу истинности, при сохранении всей остальной инфраструктуры (кодирование, экспорт, cycle-accurate симуляция).

---

## Фаза 4: Масштабирование алгоритмов (High-Performance EDA)
*Для работы с функциями от 20+ переменных ($N \ge 20$), где массивы таблиц истинности начинают потреблять гигабайты RAM.*

Завершено: 
- cube calculus (отказ от хранения $2^N$ минтермов)
- Static Timing Analysis (STA) — см. [nand_optimizer/sta.py](nand_optimizer/sta.py)

* [ ] **Интеграция ROBDD / BDD:** * Использовать библиотеки Binary Decision Diagrams (например, пакет `dd`) в качестве внутреннего ядра для компактного представления логики. BDD позволяет мгновенно проверять эквивалентность и эффективно вычислять кофакторы Шеннона.
* [ ] **Delay/Area Trade-off (Оптимизация с ограничениями):** * Добавить эвристики, которые позволяют пользователю выбирать: сгенерировать глубокую узкую цепочку (максимальная экономия гейтов) или широкое сбалансированное дерево (максимальное быстродействие за счет большего числа вентилей).
* [ ] **Параллелизация проходов (Multi-process rewrite/FRAIG):** * Rewrite и FRAIG тривиально распараллеливаются по независимым cone'ам выходов: для каждого cone'а — своя копия состояния, merge через общую базу структурного хеша. На больших AIG (>10k узлов) даёт почти линейный speedup без изменения качества результата. Использовать `multiprocessing` + разделяемый менеджер для db.
* [ ] **Incremental synthesis (инкрементальные проходы):** * Отслеживать "грязные" подграфы между итерациями скрипта синтеза (`balance; rewrite; fraig; balance; rewrite`) и перезапускать проход только на изменённой части AIG. Резко сокращает стоимость итеративной отладки скриптов и длинных цепочек оптимизации.

---

## Фаза 4.5: GPU-ускорение (флаг `--gpu`)
*Проходы с массовым параллелизмом данных, которые тривиально переносятся на GPU через CuPy / PyTorch. Активируются единым флагом `--gpu` в CLI и параметром `use_gpu=True` в `optimize()`. При отсутствии CUDA — прозрачный fallback на CPU.*

### Высокий потенциал

* [ ] ~~**GPU-симуляция в FRAIGing (`fraig.py`):**~~ **DROPPED (2026-04-25, ROADMAP P1#5 ч.2).** EPFL `sin` profile показал, что на FRAIG-heavy схеме симуляция = 0.05% от FRAIG cumtime (0.016 s из 32.3 s); 93% уходит в Z3 `_check_pair`. GPU-симуляция ускорит мёртвый рычаг. См. [benchmarks/perf_baseline.md](benchmarks/perf_baseline.md) (раздел "EPFL `sin` profile"). Реальные lever'ы: incremental Z3 solver, sim-based pre-filtering пар, расширение P3#10 SAT sweeping.
* [ ] **Параллельный NPN-lookup в рерайтере (`rewrite.py`):** * Для каждого cut вычисляется TT и выполняется lookup в `AIG_DB_4`. При N разрезах — N независимых запросов. На GPU: весь список TT передаётся батчем, lookup через `cupy` scatter/gather по таблице из 65536 элементов. Speedup линейный с числом разрезов.
* [ ] **Массовая генерация импликант и покрытие (`implicant.py`):** * Операции Quine–McCluskey (попарное объединение кубов, проверка смежности) и матрица покрытия — чистые битовые операции над прямоугольными массивами. Перенести на GPU для функций с > 12 входами, где таблица покрытия > 10^5 строк.

### Средний потенциал

* [ ] **Batched exact synthesis (`exact_synthesis.py`):** * Каждый SAT-запрос независим. Использовать GPU-параллельный SAT (cuBool / батчевый Z3 через subprocess pool + GPU-оракул для симуляции кандидатов). Актуально при заполнении кэша с нуля или при k=5,6 разрезах.
* [ ] **Параллельный синтез независимых выходов (`pipeline.py`):** * Выходы, не имеющие общих fan-out в AIG до слияния структурным хешем, можно оптимизировать в отдельных GPU-потоках. Реализовать как `torch.multiprocessing` + общая структура `AIG` с lock-free structural hash на GPU.

---

## Фаза 5: Входные языки и Технологический маппинг
*Интеграция инструмента во внешний мир электроники.*

Завершено: 
- AIGER / BLIF I/O (бинарный и ASCII форматы, round-trip верификация)
- EPFL Combinational Benchmark Suite (20 индустриальных бенчмарков, manifest + audit)
- Verilog Front-end (структурный и простой поведенческий, нативный парсер без зависимостей)

Все три — подробно в [TODO_done.md](TODO_done.md#фаза-5-входные-языки-и-технологический-маппинг).

* [ ] **Standard Cell Mapping (Библиотечный маппинг):** * Отойти от жесткой привязки к `NAND`. Реализовать алгоритм покрытия дерева (Tree Mapping / Dynamic Programming) для трансляции AIG в произвольную библиотеку логических элементов (AND, OR, NOR, AOI, OAI, MUX).
* [ ] **LUT-маппинг (FPGA Technology Mapping):** * Группировка цепей AIG в K-входовые макроячейки (Look-Up Tables) для прямого синтеза под архитектуры FPGA (например, 4-LUT или 6-LUT, применяемые в Xilinx/Altera).
* [ ] **Имитация отжига (Simulated Annealing):** * Внедрить вероятностные алгоритмы переподключения узлов во время маппинга для выхода из локальных минимумов оптимизации.

---

## Фаза 6: Реструктуризация проекта (Package Layout)
*Разбиение плоского пакета на подпакеты по логическим слоям.*

Завершено: плоский пакет из 37 модулей разложен по 8 подпакетам (`core/`, `synthesis/`, `mapping/`, `io/`, `sequential/`, `datapath/`, `analysis/`, `testing/`); оркестраторы (`pipeline.py`, `script.py`, `verify.py`, `__init__.py`, `__main__.py`) остались на верхнем уровне; монолитный `mapping/circ_export.py` разбит на подпакет `mapping/circ_export/` (5 модулей: `_layout`, `_decoder_builder`, `_decoder`, `_fsm`, `_counter`) — добавлена новая функция `export_counter_circ`; `aig_db_4.py` (65k строк Python) мигрирован в бинарный pickle `aig_db_4.pkl` (~2 МБ), `aig_db_4.py` превращён в тонкий lazy-loader (~35 строк, ROADMAP P1#4 ✅). Все `from nand_optimizer import ...` работают без изменений через re-export в `__init__.py`. См. [TODO_done.md](TODO_done.md#фаза-6-реструктуризация-проекта-package-layout).

---

## Фаза 7: Тестопригодность, мощность и физическая реализация (Testability & Physical)
*Слой, который отделяет учебный оптимизатор от инструмента, пригодного для реального ASIC-флоу.*

Завершено:
- **ATPG (Automatic Test Pattern Generation):** [nand_optimizer/atpg.py](nand_optimizer/atpg.py) — SAT-based мiter-кодирование stuck-at-faults, детектирование паттернов, метрики покрытия.
- **Static Timing Analysis (STA):** [nand_optimizer/sta.py](nand_optimizer/sta.py) — вычисление critical path, slack-анализ, arrival times от входов до выходов.
- **Switching Activity Estimation:** [nand_optimizer/switching.py](nand_optimizer/switching.py) — вероятностное распространение переключений по AIG, основа для power-aware оптимизации.

* [ ] **Power-aware optimization:** * Использовать оценки активности для выбора вариантов рерайтинга: при равной площади предпочитать структуры с меньшим switching × capacitance на узлах. Также — glitch reduction через балансировку путей (сигналы с разными arrival time на AND-входах генерируют импульсы).
* [ ] **Design-for-Test (DFT) — scan-chain insertion:** * После реализации Фазы 3 (D-триггеры) — автоматическая вставка scan-chain: замена обычных D-FF на Scan-FF с мультиплексором на входе (test/functional mode), соединение их в последовательную цепочку. Scan-chain делает все состояния автомата наблюдаемыми и управляемыми, без чего ATPG на последовательной логике нереалистичен.
* [ ] **Анализ тестопригодности (Testability metrics — SCOAP):** * Вычисление controllability (сложности установить узел в 0/1 со входов) и observability (сложности наблюдать узел на выходах) по формулам Sandia Controllability/Observability Analysis Program. Метрики помогают находить "слепые зоны" схемы и подсказывают, куда ставить test points.

---

## Фаза 8: ML-ускорение и нейросетевая оптимизация (ML-Guided Synthesis)
*Интеграция методов машинного обучения в пайплайн синтеза: от замены эвристик нейросетевыми предикторами до RL-агентов, управляющих последовательностью проходов. Источники: DRiLLS (ASP-DAC 2020), FlowTune (ICCAD 2022), DeepGate4 (2025), OpenABC-D датасет (NYU). Требует Phase 5 (AIGER I/O) и стабильного EPFL-бенчмарка для измерения QoR.*

### Быстрые победы (Low effort, самостоятельные модули)

* [ ] **RL-управление скриптом синтеза (FlowTune / DRiLLS):** Заменить фиксированный дефолт `"rewrite; fraig; balance"` обучаемым агентом. Два варианта по сложности: (a) **Multi-Armed Bandit** — FlowTune-стиль: каждая команда скрипта — «рука» бандита, награда = delta(NAND count) после прохода; накопленная статистика прокачивает вероятности выбора команд без нейросети. Интеграция в [`script.py`](nand_optimizer/script.py): добавить класс `ScriptBandit(commands, horizon)` с UCB1 или Thompson Sampling. (b) **DRiLLS-стиль RL** — состояние = вектор статистик AIG (n_nodes, n_edges, depth, avg_fanout, ratio_AND); действие = следующий проход из словаря; награда = -delta_gates; агент A2C/PPO на 3–5-слойном MLP. Датасет для pre-training: логи собственного бенчмарка (EPFL + MCNC), ~10k прогонов. Литература: [DRiLLS (arxiv:1911.04021)](https://arxiv.org/abs/1911.04021), [FlowTune (arxiv:2202.07721)](https://arxiv.org/abs/2202.07721).
* [ ] **QoR-предиктор (GNN/Transformer) для быстрой оценки потока:** Вместо запуска полного синтеза — предсказывать (area, depth) из пары (AIG_stats, pass_sequence). Обучение supervised: накопить N запусков `optimize()` на разных бенчмарках и скриптах, сохранять `(feature_vector, script_encoding) → (n_nand, depth)`. Модель: простой MLP или малый Transformer на sequence of commands + AIG-фичах. Использовать для pruning в пространстве скриптов до реального запуска. Готовый датасет: [OpenABC-D (arxiv:2110.11292)](https://arxiv.org/abs/2110.11292) — 870k AIG'ов от 1500 прогонов на 29 open-source IP. Точность: MAE ~0.4 на невиданных парах (из литературы). Интеграция: отдельный модуль `nand_optimizer/ml/qor_predictor.py`, вызывается из `script.py` при поиске лучшего потока.

### Средние задачи (Medium effort, интеграция с ядром)

* [ ] **GNN-предиктор качества разрезов (Cut Ranking) в реврайтере:** Заменить/дополнить эвристику MFFC-cost в [`rewrite.py`](nand_optimizer/rewrite.py) нейросетевым предиктором. Архитектура: локальный GNN на k-cut cone (k=4–6 уровней в TFI-окне) → регрессия на `delta_gates_if_accepted`. Обучение: для каждого принятого/отброшенного cut сохранять факт (cone_graph_features, was_beneficial_after_fraig). После накопления ~50k примеров обучать GNN; интегрировать как дополнительный scoring в `_evaluate_cut()`. Ожидаемый эффект: меньше «принять → потом откатить» циклов; лучший отбор при конкурирующих cut'ах одного узла.
* [ ] **Learned-signature FRAIG (адаптивные паттерны симуляции):** В [`fraig.py`](nand_optimizer/fraig.py) сейчас случайные 64-битные векторы. Заменить на **coverage-directed** выборку: обучить небольшой MLP (вход = распределение сигнатур текущих классов эквивалентности) предсказывать, какие входные паттерны максимально разделят существующие классы. Чередование: 50% случайных векторов + 50% от MLP. Ожидаемый эффект: 5–15% сокращение числа SAT-вызовов при том же количестве симуляционных раундов. Аналог: [Sim-Guided Resubstitution (arxiv:2007.02579)](https://arxiv.org/abs/2007.02579). **Note (2026-04-25):** sin profile подтверждает, что Z3-вызовы — реальный bottleneck (93% FRAIG времени), так что любой механизм сокращения числа SAT-вызовов имеет высокую отдачу — в отличие от ускорения симуляции (см. [perf_baseline.md](benchmarks/perf_baseline.md) "EPFL `sin` profile").
* [ ] **Нейросетевой NPN-матчинг для k=5,6 разрезов:** Текущий [`aig_db_4.py`](nand_optimizer/aig_db_4.py) покрывает только 4-входовые функции (65536 классов). Расширение до k=5 (4млн классов) через полную таблицу нереально. Альтернатива: train GNN/MLP, который из truth-table 5/6-входовой функции напрямую предсказывает оптимальный AIG-шаблон (число AND + структуру). Обучение: сгенерировать ~200k пар (TT_5bit, best_aig_template) через `exact_synthesis.py` заблаговременно; train MLP с bottleneck-архитектурой (TT → 128-dim embedding → template_id). Интеграция в `rewrite.py` как дополнительный lookup для 5-cut'ов. Литература: [OpenABC-D датасет](https://github.com/NYU-MLDA/OpenABC), данные по NPN-классам.

### Продвинутые задачи (High effort, исследовательский уровень)

* [ ] **DeepGate-style GNN embeddings для узлов AIG:** Обучить GNN (по архитектуре DeepGate2/DeepGate4) на собственном корпусе синтезируемых схем. Узлу AIG сопоставляется вектор, кодирующий одновременно структурную позицию (глубина, fanout) и функциональный смысл (signal probability, cofactor-паттерн). Эмбеддинги использовать для: (a) предсказания критического пути без полного STA; (b) ускорения поиска эквивалентных классов во FRAIG (кластеризация по embedding-близости вместо бит-параллельной симуляции); (c) guided decomposition — предсказание «хорошей» граничной переменной для Ashenhurst. Источники: [DeepGate4 (arxiv:2502.01681)](https://arxiv.org/abs/2502.01681), [FuncGNN (ICLR 2025)](https://proceedings.iclr.cc/paper_files/paper/2025/file/d75f561eaaf2cb754bc8d7e36d8af362-Paper-Conference.pdf).
* [ ] **ML-ускорение SAT-вызовов в exact synthesis и miter verification:** Интегрировать нейросетевые подсказки (branching hints) в Z3-вызовы: перед SAT-поиском прогнать малую сеть (обученную на исторических miter-инстансах), получить приоритет литералов для CDCL-разветвления. Реализуется через Z3 Python API `solver.set("phase_selection", ...)` + предварительное присвоение фаз по предсказанию. Более радикальная альтернатива: заменить первый раунд поиска эквивалентности в `fraig.py` нейросетью NeuroSAT-стиля (если ответ «UNSAT» с высокой confidence — пропускать SAT-вызов). Источники: [MAS-SAT (OpenReview)](https://openreview.net/forum?id=EWT7ILOzjK), [NeuroSAT-style methods (arxiv:2203.04755)](https://arxiv.org/abs/2203.04755).
* [ ] **DNAS (Differentiable Neural Architecture Search) для малых функций:** Для функций с n≤8 входами попробовать подход Google DeepMind (победитель IWLS 2023): дифференцируемый поиск по пространству NAND-схем через relaxed gate-selection. Реализуется как PyTorch-модуль с soft gate-type (learnable mixture AND/NAND/NOR/XOR) + straight-through estimator для бинаризации. После конвергенции — binarize и верифицировать через `miter_verify()`. Область применения: пополнение `aig_db_4.py` для редких NPN-классов, поиск нестандартных шаблонов для exact synthesis кэша.

**Датасеты и инфраструктура для ML-задач:**
- [OpenABC-D](https://github.com/NYU-MLDA/OpenABC) — 870k AIG'ов, PyTorch-ready (потребуется AIGER I/O, уже готово)
- [CircuitNet 2.0](https://github.com/circuitnet/CircuitNet) — полная цепочка синтез→разводка
- Собственный корпус: накапливать через `--log-aig` флаг в `__main__.py` при каждом запуске бенчмарка
- Обзорные статьи: [Survey ML for Logic Synthesis (TODAES 2024)](https://dl.acm.org/doi/10.1145/3785362), [ML for EDA Survey (arxiv:2102.03357)](https://arxiv.org/pdf/2102.03357)
