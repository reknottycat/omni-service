"""Inspect independently deployed company bridges for configuration conflicts."""

import argparse
import hmac
import html
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


COMPANY_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")


class CompanyConfigurationError(Exception):
    pass


def _private_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 512_000):
                raise ValueError
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise CompanyConfigurationError(f"无法读取公司 {path.parent.name} 的私有配置。") from None


def _companies(root):
    root = Path(root)
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise CompanyConfigurationError("公司配置目录无效。")
    result = []
    for directory in sorted(root.iterdir()):
        if directory.name.startswith(".") or not COMPANY_NAME.fullmatch(directory.name):
            continue
        if directory.is_symlink():
            raise CompanyConfigurationError(f"公司 {directory.name} 的配置目录不能是链接。")
        if not directory.is_dir():
            continue
        path = directory / "personal.json"
        if path.exists() or path.is_symlink():
            result.append((directory.name, _private_json(path)))
    return result


def _key(config):
    if config.get("mode") == "disabled":
        return None
    if config.get("mode") not in ("personal-managed", "personal-text-pilot"):
        raise CompanyConfigurationError("公司配置的接入模式无效。")
    try:
        telegram, feishu = config["telegram"], config["feishu"]
        bot, connection, owner, app = (telegram["bot_username"], telegram["connection_id"],
                                       telegram["expected_owner_id"], feishu["app_id"])
        if (not isinstance(bot, str) or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot)
                or not isinstance(connection, str) or not connection
                or type(owner) is not int or owner <= 0
                or not isinstance(app, str) or not re.fullmatch(r"cli_[A-Za-z0-9_]+", app)):
            raise ValueError
        management = config.get("management") or {}
        port = management.get("port", 8081) if management.get("enabled") else None
        callback = management.get("callback_url") if management.get("enabled") else None
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            raise ValueError
        if callback is not None:
            parsed = urlsplit(callback)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path != "/admin/auth/callback" or parsed.query or parsed.fragment):
                raise ValueError
            callback = (parsed.scheme, parsed.hostname.lower(), parsed.port, parsed.path)
        return {"telegram_bot": bot.casefold(), "connection": connection, "owner": owner,
                "feishu_app": app, "management_port": port, "management_callback": callback}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CompanyConfigurationError("已启用的公司配置不完整。") from None


def configuration_conflicts(root):
    seen = {}
    conflicts = []
    for name, config in _companies(root):
        try:
            keys = _key(config)
        except CompanyConfigurationError as exc:
            raise CompanyConfigurationError(f"公司 {name}：{exc}") from None
        if keys is None:
            continue
        for field, value in keys.items():
            if value is None:
                continue
            pair = field, value
            if pair in seen:
                conflicts.append({"companies": [seen[pair], name], "field": field})
            else:
                seen[pair] = name
    return conflicts


def validate_company(root, company):
    if not COMPANY_NAME.fullmatch(company):
        raise CompanyConfigurationError("公司代号无效。")
    conflicts = [item for item in configuration_conflicts(root) if company in item["companies"]]
    if conflicts:
        fields = {"telegram_bot": "Telegram 机器人", "connection": "Telegram 连接",
                  "owner": "Telegram 原账号", "feishu_app": "飞书应用",
                  "management_port": "管理端口", "management_callback": "管理地址"}
        descriptions = [f"{'、'.join(item['companies'])} 共用{fields[item['field']]}" for item in conflicts]
        raise CompanyConfigurationError("配置冲突：" + "；".join(descriptions))


def list_companies(root):
    conflicts = configuration_conflicts(root)
    result = []
    for name, config in _companies(root):
        enabled = config.get("mode") in ("personal-managed", "personal-text-pilot")
        management = config.get("management") or {}
        callback = management.get("callback_url", "") if enabled and management.get("enabled") else ""
        parsed = urlsplit(callback)
        address = f"{parsed.scheme}://{parsed.netloc}/admin/" if parsed.hostname else None
        try:
            state = subprocess.run(["systemctl", "--user", "is-active", f"omni-service-{name}.service"],
                                   capture_output=True, text=True, timeout=5, check=False)
            service = state.stdout.strip() if state.stdout.strip() in {
                "active", "inactive", "failed", "activating", "deactivating"} else "unknown"
        except (OSError, subprocess.TimeoutExpired):
            service = "unknown"
        result.append({"company": name, "configured": enabled, "service": service,
                       "admin_url": address, "conflicts": [entry["field"] for entry in conflicts
                                                           if name in entry["companies"]]})
    return result


def serve_companies(root, port):
    token = secrets.token_urlsafe(32)
    expected_host = f"127.0.0.1:{port}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if (self.headers.get('Host') != expected_host or '?' in self.path
                    or not hmac.compare_digest(self.path, '/' + token)):
                self.send_error(404)
                return
            try:
                companies = list_companies(root)
            except CompanyConfigurationError:
                self.send_error(500, '无法读取公司配置')
                return
            rows = []
            labels = {'active': '运行中', 'inactive': '已停止', 'failed': '启动失败',
                      'activating': '启动中', 'deactivating': '停止中', 'unknown': '状态未知'}
            for entry in companies:
                name = html.escape(entry['company'])
                status = labels[entry['service']]
                config = '配置完成' if entry['configured'] else '等待配置'
                conflict = ' · 配置存在冲突' if entry['conflicts'] else ''
                link = ''
                if entry['admin_url']:
                    url = html.escape(entry['admin_url'], quote=True)
                    link = f'<a href="{url}" target="_blank" rel="noopener noreferrer">打开公司管理台</a>'
                rows.append(f'<article><h2>{name}</h2><p>{status} · {config}{conflict}</p>{link}</article>')
            content = ''.join(rows) or '<p>尚未添加公司。</p>'
            page = ('''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>OmniService 公司管理</title>
<style>body{font:16px/1.6 system-ui,sans-serif;color:#1c2941;background:#f5f7fb;margin:0}
main{max-width:1000px;margin:6vh auto;padding:0 24px}h1{font-size:30px}p{color:#52617a}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
article{background:white;padding:22px;border:1px solid #dde4ee;border-radius:16px}
h2{font-size:19px;margin:0}a{display:inline-block;color:#2655bc;font-weight:600}</style>
<main><h1>公司管理</h1><p>查看各公司的运行状态，进入对应管理台。</p><div class="grid">'''
                    + content + '</div></main></html>').encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(page)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(page)

    with ThreadingHTTPServer(('127.0.0.1', port), Handler) as server:
        print(f'公司管理页 http://{expected_host}/{token}', flush=True)
        server.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description="查看和检查各公司独立的 Telegram 客服实例。")
    parser.add_argument("action", choices=("list", "check", "serve"))
    parser.add_argument("--company")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args(argv)
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "omni-service"
    try:
        if args.action == 'serve':
            if not 1 <= args.port <= 65535:
                parser.error('端口无效')
            serve_companies(root, args.port)
        elif args.action == "check":
            if not args.company:
                parser.error("check 需要 --company")
            validate_company(root, args.company)
            print(f"{args.company} 的公司配置没有发现重复绑定。")
        else:
            print(json.dumps(list_companies(root), ensure_ascii=False, indent=2))
        return 0
    except CompanyConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
