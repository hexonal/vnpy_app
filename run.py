"""
Boots the actual VeighNa MainEngine with the full safety stack wired in,
then serves the AgentBridge MCP server over localhost HTTP.

Wiring order matters (see vnpy_agentbridge/README.md "Two independent gates"):
  1. RiskManagerApp  — mechanical gate, patches main_engine.send_order.
                        Must load before anything else could cache the
                        unpatched function.
  2. IntentEngine     — structural gate. The only path any Agent-facing MCP
                        tool can reach send_order through is
                        IntentEngine.approve_intent().
  3. FutuGateway      — HK/US market connection. connect() catches its own
                        SDK/network errors and write_log()s them instead of
                        raising, so a missing/unreachable OpenD daemon logs
                        a failure and the process keeps running rather than
                        crashing.

No real credentials are hard-coded. FUTU_UNLOCK_PASSWORD_MD5 defaults to
empty (trade context stays connected-but-locked); trd_env defaults to
"SIMULATE". Set the environment variables below to point this at a real
(or real-paper) OpenD instance — that is an explicit opt-in this script
never does on its own.
"""

from __future__ import annotations

import os

# See run_gui.py for why this must be set before any `import vnpy...` —
# without it, this machine's LANG=en_US.UTF-8 makes vnpy log everything in
# English even though Chinese is vnpy's actual source language.
os.environ.setdefault("LANGUAGE", "zh_CN")

from vnpy.event import Event, EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_LOG

from vnpy_agentbridge.engine import IntentEngine
from vnpy_agentbridge.mcp_bridge import build_mcp_bridge
from vnpy_agentbridge.rules.confidence_rule import ConfidenceRule
from vnpy_futu import FutuGateway
from vnpy_riskmanager import RiskManagerApp

MCP_HOST = os.environ.get("AGENTBRIDGE_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("AGENTBRIDGE_MCP_PORT", "8765"))


def _on_log(event: Event) -> None:
    log = event.data
    print(f"[{log.gateway_name or 'MAIN'}] {log.msg}", flush=True)


def build_main_engine() -> tuple[MainEngine, IntentEngine]:
    event_engine = EventEngine()
    event_engine.register(EVENT_LOG, _on_log)

    main_engine = MainEngine(event_engine)

    main_engine.add_app(RiskManagerApp)

    intent_engine = main_engine.add_engine(IntentEngine)
    intent_engine.add_rule(ConfidenceRule(min_confidence=0.6))

    main_engine.add_gateway(FutuGateway)
    main_engine.connect(
        {
            "host": os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
            "port": int(os.environ.get("FUTU_OPEND_PORT", "11111")),
            "unlock_password_md5": os.environ.get("FUTU_UNLOCK_PASSWORD_MD5", ""),
            "trd_env": os.environ.get("FUTU_TRD_ENV", "SIMULATE"),
        },
        "FUTU",
    )

    return main_engine, intent_engine


def main() -> None:
    main_engine, intent_engine = build_main_engine()

    mcp = build_mcp_bridge(main_engine, intent_engine)
    print(
        f"[MAIN] AgentBridge MCP server starting on http://{MCP_HOST}:{MCP_PORT}/mcp",
        flush=True,
    )

    try:
        mcp.run(transport="http", host=MCP_HOST, port=MCP_PORT, log_level="warning")
    finally:
        print("[MAIN] shutting down MainEngine...", flush=True)
        main_engine.close()


if __name__ == "__main__":
    main()
