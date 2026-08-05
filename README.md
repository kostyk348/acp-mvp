# ACP MVP — Agent Contract Protocol

Минимально жизнеспособный прототип децентрализованной сети агентов, где автономные акторы обмениваются строгими контрактами в формате `.eml` (RFC 822/MIME) с встроенным escrow и proof-of-execution.

## Архитектура

```
┌───────────────────────────────────────────────────┐
│  ACP Agent Node                                   │
│                                                    │
│  ┌────────────┐  ┌────────────────────────────┐  │
│  │ EML Parser │  │ Contract Generator          │  │
│  └─────┬──────┘  └────────────┬───────────────┘  │
│        └──────────────────────┘                    │
│                     │                               │
│  ┌──────────────────▼───────────────────────────┐  │
│  │  SMTP MTA (aiosmtpd) — порт 2525            │  │
│  └──────────────────┬───────────────────────────┘  │
│                     │                               │
│  ┌──────────────────▼───────────────────────────┐  │
│  │  Orchestrator — capability routing            │  │
│  └──────────────────┬───────────────────────────┘  │
│                     │                               │
│  ┌──────────────────▼───────────────────────────┐  │
│  │  Sandbox (Docker) — изоляция исполнения      │  │
│  └──────────────────┬───────────────────────────┘  │
│                     │                               │
│  ┌──────────────────▼───────────────────────────┐  │
│  │  Blockchain (AgentEscrow.sol)                │  │
│  └──────────────────┬───────────────────────────┘  │
│                     │                               │
│  ┌──────────────────▼───────────────────────────┐  │
│  │  sint-ua — SQLite FACT-цепочка               │  │
│  └──────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────┘
```

## Структура проекта

```
acp-mvp/
├── contracts/              # Solidity смарт-контракты
│   └── AgentEscrow.sol
├── eml/                    # EML контракт: парсер + генератор + схема
│   ├── __init__.py
│   ├── generator.py
│   ├── parser.py
│   ├── schema.py
│   └── contract_schema.json
├── transport/              # SMTP транспорт
│   ├── __init__.py
│   ├── mta.py              # Локальный MTA на порту 2525
│   └── relay.py            # Исходящий релей
├── api/                    # REST control plane (aiohttp)
│   ├── __init__.py
│   └── server.py
├── sandbox/                # Docker sandbox исполнения
│   ├── __init__.py
│   └── executor.py
├── identity/               # ECDSA identity + keystore
│   ├── __init__.py
│   └── keystore.py
├── settlement/             # Escrow / settlement
│   ├── __init__.py
│   └── escrow.py
├── sint_ua/                # Semantic fact chain (SQLite)
│   ├── __init__.py
│   └── fact_chain.py
├── orchestrator/           # Core logic + CLI
│   ├── __init__.py
│   ├── core.py
│   └── cli.py
├── hardhat/                # Blockchain dev environment
│   ├── package.json
│   ├── hardhat.config.js
│   ├── test/
│   │   └── AgentEscrow.test.js
│   └── scripts/
│       └── deploy.js
├── tests/
│   └── test_e2e.py
├── docker-compose.yml
├── docker-compose.sandbox.yml
├── Dockerfile.agent
├── requirements.txt
├── pyproject.toml
└── README.md
```

## Быстрый старт

### 1. Установка зависимостей

```bash
cd acp-mvp
pip install -r requirements.txt
```

### 2. Запуск локального блокчейна

```bash
# Вариант A: Hardhat node
cd hardhat && npm install && npx hardhat node

# Вариант B: Anvil (через Docker)
docker compose up anvil
```

### 3. Запуск двух агентов

```bash
# Терминал 1 — заказчик
python -m orchestrator --role customer --agent-id customer --smtp-port 2526 --process-inbox

# Терминал 2 — исполнитель
python -m orchestrator --role executor --agent-id executor --smtp-port 2527 --process-inbox
```

### 4. Генерация тестового контракта

```bash
python -m orchestrator --role customer --generate-contract
```

### 5. Запуск e2e тестов

```bash
python -m pytest tests/test_e2e.py -v
```

### 6. REST API (control plane)

```bash
python -m api.server   # порт 8080 (переопределяемый через ACP_API_PORT)
```

| Метод | Путь | Описание |
|---|---|---|
| GET | `/health` | Liveness узла + статус escrow |
| GET | `/inbox` | Список входящих контрактов |
| GET | `/inbox/{message_id}` | Чтение одного контракта |
| POST | `/outbox` | Публикация контракта в mesh (SMTP) |
| GET | `/escrow/{message_id}` | Состояние escrow-задачи |
| GET | `/facts?tag=&register=` | Запрос fact-chain |
| GET | `/reputation/{agent_id}` | Репутация агента (on-chain) |
| POST | `/sandbox/test` | Dry-run верификация в sandbox (без средств) |

### 7. Docker Compose (полный стек)

```bash
docker compose up --build
```

Поднимает: Anvil (блокчейн), SMTP relay, agent-customer, agent-executor.

Сэндбокс-контейнеры запускаются покомпонентно с жёсткими лимитами ресурсов:

```bash
docker compose -f docker-compose.sandbox.yml run --rm \
  -e TASK_ID=task-01 -e TESTS="cargo test" sandbox
```

## Ключевые модули

### EML Контракт
- Генерация валидных MIME-сообщений (`.eml`) по RFC 822
- Обязательные заголовки: `Message-ID`, `From`, `To`, `Subject`, `X-Agent-Protocol`, `X-Escrow-*`, `X-Timeout-UTC`, `X-Proof-of-Stake`
- Broadcast-адресация: `To: broadcast+capability.{category}.{domain}@mesh.local`
- JSON payload по схеме `agent-contract/v1` (с `schema_version`)
- Источник: `source` (base64 | ipfs | git)
- Верификация: `tests[]` как объекты `{cmd, timeout, expected_exit_code}` + `artifacts_expected[]`
- Settlement: `royalty_split {human_principal, agent_executor}` + `auto_verify`
- Парсинг входящих `.eml` с валидацией по JSON Schema

### Транспорт
- Локальный SMTP MTA на порту 2525 (`aiosmtpd`)
- Capability-based routing по заголовку `To:`
- Anti-spam: отбраковка писем без `X-Proof-of-Stake` или с бюджетом < 0.01

### Sandbox
- Docker-контейнер с жёсткими лимитами: 1 CPU `--cpus`, 512MB `--memory`, `--pids-limit 64`
- Read-only FS (`--read-only`) + tmpfs `/tmp`
- Нет сети (`--network none`), `--cap-drop ALL`, `--security-opt no-new-privileges`
- Capture stdout/stderr, формирование `proof.json` из `exit_codes`

### Escrow (v0.1)
- Solidity контракт `AgentEscrow` с полным жизненным циклом:
  `createTask → acceptTask → submitResult → verifyAndRelease`
- ERC-20 поддержка (default USDC) + нативный ETH fallback
- `slashOnTimeout()` — слэйшинг ставки и возврат средств заказчику по дедлайну
- `reputation` mapping: успех `+1`, провал `-1`, timeout-слэйш `-5`
- Royalty split через `royaltyHuman` / `royaltyAgent` (uint8, %)

```bash
cd hardhat
npx hardhat test          # 10 контрактных тестов
npx hardhat run scripts/deploy.js --network localhost
```

### Identity
- ECDSA keypair (SECP256R1) при первом запуске
- Зашифрованный keystore (`AESGCM` + `PBKDF2HMAC`)
- Подпись/верификация исходящих `.eml`

### sint-ua
- SQLite хранилище FACT/LOGIC/OPINION/ACTION-блоков
- Hash chain integrity (SHA256)
- Регистры валидируются через SQL `CHECK` + `register`-параметр
- Блоки привязаны к контрактам через `contract_id`
- Теги — JSON-массивы; фильтр по `register` / `contract_id`
- MCP-интеграция: `cross_reference()` опрашивает sint-algo, sint-crypto,
  sint-dso-matcher, sint-resonance; ссылки пишутся в `mcp_references`
- API: `record()`, `query()`, `verify_chain()`, `query_mcp_enriched()`

## Безопасность (v0.1 трейд-оффы)

| Угроза | Митигация |
|---|---|
| Убегающий код в sandbox | `--pids-limit`, `--network none`, `read-only`, no-new-privileges |
| Agent не выполняет контракт | `slashOnTimeout` возвращает principal |
| Agent сливает бюджет | Escrow удерживает средства до proof |
| Спам по mesh | `X-Proof-of-Stake` + min budget + capability routing |
| Фальшивый proof | Хеш stdout/stderr привязан к артефакту |
| Зависший deadlock | `timeout` на каждый test + on-chain deadline |

1. Два агента закрывают сделку по компиляции без человеческого вмешательства
2. Бюджет из escrow переходит исполнителю только после successful proof
3. Выполнение в sandbox без доступа к хостовой FS и сети
4. Лог выполнения + хеш артефакта записываются и проверяются
5. `docker compose up` поднимает полный стек

## Риски MVP (ограничения)

| Риск | Статус |
|---|---|
| Docker sandbox ≠ TEE | MVP достаточно |
| Нет реального DHT | Hardcoded релеи |
| Нет ZK-proof | Доверие к sandbox + escrow timeout |
| Sepolia gas | Тестнет токены / Anvil для dev |

## Лицензия

MIT — см. LICENSE
