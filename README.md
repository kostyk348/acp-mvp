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
├── tests/
│   └── test_e2e.py
├── docker-compose.yml
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

### 6. Docker Compose (полный стек)

```bash
docker compose up --build
```

Поднимает: Anvil (блокчейн), SMTP relay, agent-customer, agent-executor.

## Ключевые модули

### EML Контракт
- Генерация валидных MIME-сообщений (`.eml`) по RFC 822
- Обязательные заголовки: `Message-ID`, `From`, `To`, `Subject`, `X-Agent-Protocol`, `X-Escrow-*`, `X-Timeout-UTC`
- JSON payload по схеме `agent-contract/v1`
- Поддержка `multipart/mixed` (payload + вложения)
- Парсинг входящих `.eml` с валидацией по JSON Schema

### Транспорт
- Локальный SMTP MTA на порту 2525 (`aiosmtpd`)
- Capability-based routing по заголовку `To:`
- Anti-spam: отбраковка писем без `X-Proof-of-Stake` или с бюджетом < 0.01

### Sandbox
- Docker-контейнер с ограничениями: 1 CPU, 512MB RAM, 5 min timeout
- Read-only FS (кроме `/workspace`)
- Нет сетевого доступа
- Capture stdout/stderr, формирование `proof.json`

### Escrow
- Solidity контракт `AgentEscrow` с методами `createTask`, `submitResult`, `verifyAndRelease`
- Royalty split: 90% заказчик / 10% исполнитель
- Локальный Anvil для dev, Sepolia для демо

### Identity
- ECDSA keypair (SECP256R1) при первом запуске
- Зашифрованный keystore (`AESGCM` + `PBKDF2HMAC`)
- Подпись/верификация исходящих `.eml`

### sint-ua
- SQLite хранилище FACT-блоков
- Hash chain integrity (SHA256)
- API: `record()`, `query()`, `verify_chain()`

## Критерии приёмки MVP

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
