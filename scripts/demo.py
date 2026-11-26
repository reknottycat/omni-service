#!/usr/bin/env python3
"""Show three end-to-end conversations using only loopback HTTP and fixtures."""

import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from omni_service.server import create_server, DEFAULT_ACCOUNTS
from omni_service.service import Service
from omni_service.simulation import FakePlatforms, customer_payload, employee_payload, signed_headers
from omni_service.transport import HttpTransport


def run_demo():
    transcript = []
    with tempfile.TemporaryDirectory() as directory, FakePlatforms() as platforms:
        service = Service(str(Path(directory) / "demo.sqlite3"), HttpTransport(platforms.urls))
        server = create_server(service)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def call(path, data=None, headers=None):
            raw = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
            request = Request(base + path, data=raw,
                              headers={"Content-Type": "application/json", **(headers or {})})
            with urlopen(request, timeout=5) as response:
                return json.load(response)
        def webhook(channel, payload):
            account = "demo-" + channel
            raw = json.dumps(payload, ensure_ascii=False).encode()
            headers = signed_headers(channel, raw, DEFAULT_ACCOUNTS[(channel, account)])
            return call(f"/webhooks/{channel}/{account}", payload, headers)
        def admin(path, body=None):
            return call(path, body, {"Authorization": "Bearer simulation-admin"})
        try:
            for channel in ("telegram", "max", "whatsapp"):
                initial = webhook(channel, customer_payload(channel, "Здравствуйте", sequence=1))
                cid = initial["results"][0]["conversation_id"]
                admin("/api/drain", {})
                admin(f"/api/conversations/{cid}/handoff", {})
                admin("/api/drain", {})
                room = admin(f"/api/conversations/{cid}")["room_id"]
                reply = webhook("feishu", employee_payload(room, "您好，我来帮您处理。"))
                mid = reply["results"][0]["message_id"]
                admin("/api/drain", {})
                actual = [m for m in platforms.messages if m["channel"] == channel]
                assert actual[-1]["text"] == "employee-1: Здравствуйте! Я помогу вам.", actual
                admin(f"/api/conversations/{cid}/messages/{mid}/recall", {"actor_id": "employee-1"})
                admin("/api/drain", {})
                detail = admin(f"/api/conversations/{cid}")
                message = next(m for m in detail["messages"] if m["id"] == mid)
                expected = "unsupported" if channel == "whatsapp" else "deleted"
                assert message["recall_status"] == expected, message
                transcript.append({"channel": channel, "customer_text": "Здравствуйте",
                                   "language": detail["language"], "employees": len(detail["members"]),
                                   "employee_original": "您好，我来帮您处理。", "customer_received": actual[-1]["text"],
                                   "recall": message["recall_status"], "customer_messages": actual})
            return {"mode": "simulation", "passed": True, "conversations": transcript,
                    "platform_http_requests": platforms.requests}
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
            service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "demo.json")
    args = parser.parse_args()
    result = run_demo()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for row in result["conversations"]:
        print(f"{row['channel']}: 客户语言={row['language']}；飞书成员={row['employees']}；"
              f"客户收到={row['customer_received']}；撤回={row['recall']}")
    print(f"Simulation PASS. Evidence: {args.output}")


if __name__ == "__main__":
    main()
