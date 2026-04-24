
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

Завершено: FRAIGing, AIG balancing, synthesis scripts (`balance; rewrite; fraig`), Graphviz export, precomputed 4-input NPN AIG database, Don't-Care V1 (SDC), Don't-Care V2 (полный Mishchenko 2009: sim-based ODC propagation + window resubstitution + DC-aware exact synthesis + multi-round iterative refinement), `care_rounds_internal` как флаг `dc -C N`, V2.d EPFL-инструментация + adaptive-W + fix exhaustive-enum, Multi-Armed Bandit контроллер `ScriptBandit` (UCB1 + Thompson Sampling) с CLI `--bandit HORIZON`, Don't-Care V3 (z3-exact per-cut admissibility → 0 reverts на router/priority/i2c; window-local ODC; topology-aware 1-gate resub; regression fixtures в [tests/](tests/)). См. [TODO_done.md](TODO_done.md#фаза-27-продвинутые-манипуляции-с-графами).
* [ ] **XAG (XOR-AND Graph):** * Ввести XOR как первоклассный узел графа (в дополнение к AND с инверсиями). Сейчас XOR-паттерны детектируются только в [nand.py](nand_optimizer/nand.py) на этапе маппинга — это поздно: rewrite/FRAIG видят XOR как 3 AND и не могут нормально оптимизировать арифметику/криптографию. XAG естественно представляет сумматоры, компараторы, parity-функции и SHA-подобные схемы. Потребует адаптации AIG-API и базы NPN-классов (XAG_DB).
* [ ] **MIG (Majority-Inverter Graph):** * Альтернативное представление через 3-входовый мажоритарный элемент MAJ(a,b,c) и инверсии. Аксиоматика MIG (commutativity, associativity, distributivity, inverter propagation) даёт более компактное представление сумматоров и умножителей, чем AIG. Аспирационно — полезно как экспериментальный бэкенд для арифметических бенчмарков. Ссылка: Amarú et al., "Majority-Inverter Graph: A New Paradigm for Logic Optimization" (TCAD 2016).
* [ ] **SAT sweeping и window-based оптимизация:** * Более глубокая версия FRAIG: выделяется ограниченное окно (cone глубины ≤ D вокруг узла), для него вычисляются локальные don't-cares, затем SAT-solver минимизирует функцию окна и реструктуризирует внутренние узлы. Позволяет убирать избыточность, не обнаруживаемую глобальной сигнатурной симуляцией FRAIG.

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

* [ ] **GPU-симуляция в FRAIGing (`fraig.py`):** * Сейчас бит-параллельная симуляция сигнатур (W=64 бита на CPU-int) ограничена пропускной способностью одного ядра. На GPU: тензор `[N_nodes × B_vectors]` (B ≈ 4096–16384) с операциями AND/XOR по всем узлам одновременно через `cupy` или `torch`. Даёт плотные классы эквивалентности за один проход вместо нескольких раундов дополнения. Ожидаемый speedup: 20–100× на AIG > 1k узлов.
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
* [ ] **Learned-signature FRAIG (адаптивные паттерны симуляции):** В [`fraig.py`](nand_optimizer/fraig.py) сейчас случайные 64-битные векторы. Заменить на **coverage-directed** выборку: обучить небольшой MLP (вход = распределение сигнатур текущих классов эквивалентности) предсказывать, какие входные паттерны максимально разделят существующие классы. Чередование: 50% случайных векторов + 50% от MLP. Ожидаемый эффект: 5–15% сокращение числа SAT-вызовов при том же количестве симуляционных раундов. Аналог: [Sim-Guided Resubstitution (arxiv:2007.02579)](https://arxiv.org/abs/2007.02579).
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
