"""Run one Feishu SDK socket for a bridge subprocess without CLI event consumers."""

import json
import os
import signal
import sys
import threading

from .approval_card import CARD_ACTION

RECALL_EVENT = "im.message.recalled_v1"
SDK_STREAM = "feishu-sdk"


def _dispatcher(sdk, marshal, settings, emit):
    def receive(data):
        wire = json.loads(marshal(data))
        header, event = wire.get("header"), wire.get("event")
        if (wire.get("schema") != "2.0" or not isinstance(header, dict) or not isinstance(event, dict)
                or header.get("app_id") != settings["app_id"] or header.get("tenant_key") != settings["tenant_key"]):
            return
        safe_header = {key: header.get(key) for key in ("app_id", "tenant_key", "event_type", "event_id")}
        if header.get("event_type") == CARD_ACTION:
            operator, action, context = (event.get(key) for key in ("operator", "action", "context"))
            if not all(isinstance(value, dict) for value in (operator, action, context)):
                return
            event = {"host": event.get("host"), "operator": {"open_id": operator.get("open_id")},
                     "action": {key: action.get(key) for key in ("tag", "value", "form_value")},
                     "context": {key: context.get(key) for key in ("open_chat_id", "open_message_id")}}
        emit({"schema": "2.0", "header": safe_header, "event": event})

    builder = (sdk.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(receive)
               .register_p2_im_message_recalled_v1(receive))
    if settings["approval_cards"]:
        builder.register_p2_card_action_trigger(receive)
    return builder.build()


def _exit_on_parent_close():
    # The private stdin pipe is kept open by FeishuLive for this child's whole lifetime.
    sys.stdin.buffer.read(1)
    os.kill(os.getpid(), signal.SIGTERM)


def main():
    try:
        import lark_oapi as sdk
        from lark_oapi.core.json import JSON
    except ImportError:
        print("Feishu SDK unavailable", file=sys.stderr, flush=True)
        return 1

    def ready():
        print(f"[event] ready event_key={SDK_STREAM}", file=sys.stderr, flush=True)

    # SDK 1.9.0 start() blocks and its public reconnect observer only fires
    # after a reconnect. Signal readiness after its initial WebSocket handshake.
    class Client(sdk.ws.Client):
        async def _connect(self):
            await super()._connect()
            ready()

    def emit(wire):
        print(json.dumps(wire, ensure_ascii=False), flush=True)

    try:
        settings = json.loads(sys.stdin.buffer.readline(4096))
        dispatcher = _dispatcher(sdk, JSON.marshal, settings, emit)
        client = Client(settings["app_id"], settings["app_secret"], event_handler=dispatcher,
                        log_level=sdk.LogLevel.CRITICAL)
        # Do not silently consume events after a gap. The bridge terminates and
        # waits for this process before restarting its single feed.
        client.on_reconnecting = lambda: print("WARN: sdk connection lost", file=sys.stderr, flush=True)
        threading.Thread(target=_exit_on_parent_close, daemon=True).start()
        client.start()
    except Exception:
        # The SDK's connection URL and errors may contain authentication data.
        print("Feishu SDK connection ended", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
