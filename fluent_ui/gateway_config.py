"""
Persistent gateway connection config in QuestDB (the user asked for the
selections to land in "数据库", and QuestDB is this project's active
backend). Stores, per gateway: capability flags (is_quote / is_trade —
these also feed the split quote/trade routing design) + auto_connect +
the full connect setting dict. run_gui reads this at startup and
auto-connects every gateway flagged auto_connect.

QuestDB is a columnar time-series DB, so config is modeled the QuestDB
way: every save APPENDS a row stamped `ts`, and reads take the newest
row per gateway via `LATEST ON ts PARTITION BY gateway_name`. No UPDATE
in place (QuestDB has none) — the append-and-latest pattern is the
idiomatic key-value-over-time approach and keeps a full audit trail of
config changes for free.

Connection reuses vnpy's SETTINGS (database.host/port/user/password/
database), which already point at the same QuestDB the bar data lives in
(see ~/.vntrader/vt_setting.json). Fails soft: any DB error logs and
returns an empty config so a QuestDB hiccup never blocks the GUI from
starting — the user can still connect gateways manually.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import psycopg

from vnpy.trader.setting import SETTINGS

_TABLE = "gateway_config"

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    gateway_name SYMBOL CAPACITY 64 CACHE,
    is_quote BOOLEAN,
    is_trade BOOLEAN,
    auto_connect BOOLEAN,
    setting_json STRING,
    ts TIMESTAMP
) TIMESTAMP(ts) PARTITION BY YEAR WAL
DEDUP UPSERT KEYS(ts, gateway_name);
"""


@dataclass
class GatewayConfig:
    gateway_name: str
    is_quote: bool = True
    is_trade: bool = False
    auto_connect: bool = False
    setting: dict = field(default_factory=dict)


def _conninfo() -> str:
    return (
        f"host={SETTINGS.get('database.host', 'localhost')} "
        f"port={int(SETTINGS.get('database.port', 8812))} "
        f"user={SETTINGS.get('database.user', 'admin')} "
        f"password={SETTINGS.get('database.password', 'quest')} "
        f"dbname={SETTINGS.get('database.database', 'qdb')}"
    )


def _ensure_table(cur: psycopg.Cursor) -> None:
    cur.execute(_CREATE_SQL)


def save_config(config: GatewayConfig) -> None:
    """Append one config row for this gateway (newest wins on read)."""
    try:
        with psycopg.connect(_conninfo(), autocommit=True) as conn:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    f"INSERT INTO {_TABLE} "
                    "(gateway_name, is_quote, is_trade, auto_connect, setting_json, ts) "
                    "VALUES (%s, %s, %s, %s, %s, now())",
                    (
                        config.gateway_name,
                        config.is_quote,
                        config.is_trade,
                        config.auto_connect,
                        json.dumps(config.setting, ensure_ascii=False),
                    ),
                )
    except Exception as exc:  # noqa: BLE001 — config persistence must never crash the connect flow
        print(f"[gateway_config] 保存到 QuestDB 失败(不影响本次连接): {exc}")


def load_config(gateway_name: str) -> GatewayConfig | None:
    """Newest saved config for one gateway, or None if never saved."""
    try:
        with psycopg.connect(_conninfo(), autocommit=True) as conn:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    f"SELECT gateway_name, is_quote, is_trade, auto_connect, setting_json "
                    f"FROM {_TABLE} WHERE gateway_name = %s "
                    "LATEST ON ts PARTITION BY gateway_name",
                    (gateway_name,),
                )
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        print(f"[gateway_config] 从 QuestDB 读取失败: {exc}")
        return None

    if row is None:
        return None
    name, is_quote, is_trade, auto_connect, setting_json = row
    setting = json.loads(setting_json) if setting_json else {}
    return GatewayConfig(name, bool(is_quote), bool(is_trade), bool(auto_connect), setting)


def load_all_configs() -> list[GatewayConfig]:
    """Newest config for every gateway ever saved — used at startup to
    decide what to auto-connect."""
    try:
        with psycopg.connect(_conninfo(), autocommit=True) as conn:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    f"SELECT gateway_name, is_quote, is_trade, auto_connect, setting_json "
                    f"FROM {_TABLE} LATEST ON ts PARTITION BY gateway_name"
                )
                rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"[gateway_config] 从 QuestDB 读取全部配置失败: {exc}")
        return []

    configs: list[GatewayConfig] = []
    for name, is_quote, is_trade, auto_connect, setting_json in rows:
        setting = json.loads(setting_json) if setting_json else {}
        configs.append(GatewayConfig(name, bool(is_quote), bool(is_trade), bool(auto_connect), setting))
    return configs
