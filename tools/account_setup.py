"""Private loopback UI for session export and user-controlled reauthentication."""

from __future__ import annotations

import argparse
import hmac
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import secrets
import threading
from urllib.parse import parse_qs
import webbrowser

from local_state import STATE_DIR, load_session, read_status, record, save_session
from protocol_setup import ApiFailure, complete_registration, finish_registration
from session_codec import InvalidSession, decode_session, encode_session

STYLE = """
:root{color-scheme:light;font:16px/1.6 system-ui,'Microsoft YaHei',sans-serif;color:#172033;background:#f4f6f9}
*{box-sizing:border-box}main{width:min(100% - 32px,640px);margin:6vh auto;padding:28px;background:white;border:1px solid #dbe1ea;border-radius:12px}
h1{font-size:25px;line-height:1.35}p{margin:16px 0}label{display:block;margin-top:18px;font-weight:600}
input,textarea{width:100%;padding:10px 12px;min-height:46px;font:inherit;border:1px solid #94a3b8;border-radius:6px;margin-top:6px}
textarea{font-family:monospace;font-size:14px;overflow-wrap:anywhere;min-height:180px}button{padding:12px 18px;margin-top:22px;font:inherit;font-weight:600;background:#1d4ed8;color:white;border:0;border-radius:6px;cursor:pointer}
:focus-visible{outline:3px solid #93c5fd;outline-offset:3px}a{color:#1d4ed8}small{color:#475569}.message{padding:14px;background:#f1f5f9;border-radius:6px;overflow-wrap:anywhere}.error{background:#fff1f2;color:#9f1239}
"""


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0, route=None):
        super().__init__(("127.0.0.1", port), SetupHandler)
        self.route = route or "/" + secrets.token_urlsafe(24)
        self.host_header = f"127.0.0.1:{self.server_port}"
        self.csrf = secrets.token_urlsafe(32)
        self.operation_lock = threading.Lock()
        self.url = "http://" + self.host_header + self.route


class SetupHandler(BaseHTTPRequestHandler):
    server: SetupServer

    def log_message(self, *_args):
        pass

    def respond(self, content: str, status=200):
        body = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Mansion Security · 会话设置</title><style>" + STYLE + "</style>"
                "<main><small>MANSION SECURITY / HOME ASSISTANT</small>" + content + "</main></html>").encode("utf-8")
        self.send_response(status)
        for name, value in {
            "Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body)),
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        }.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def action_form(self, action: str, label: str) -> str:
        return (f"<form method='post' action='{self.server.route}/{action}'>"
                f"<input type='hidden' name='csrf' value='{self.server.csrf}'>"
                f"<button type='submit'>{label}</button></form>")

    def seed_form(self, message=""):
        self.respond("<h1>恢复本机会话资料</h1><p>粘贴以前保存的 MSHA1 会话码。"
                     "此操作只把会话保存在本机，不向服务端发送请求。</p>" + message +
                     f"<form method='post' action='{self.server.route}/import'>"
                     f"<input type='hidden' name='csrf' value='{self.server.csrf}'>"
                     "<label for='session_code'>私密会话码</label><input type='password' id='session_code' name='session_code' required autocomplete='off'>"
                     "<button type='submit'>恢复本机资料</button></form>")

    def export_page(self):
        try:
            code = encode_session(load_session())
        except (FileNotFoundError, InvalidSession):
            self.seed_form()
            return
        if read_status().get("stage") != "complete":
            self.respond("<h1>登记尚未完成</h1><p>请先完成登记，再导出可供 HA 使用的会话。</p>"
                         f"<a href='{self.server.route}'>返回设置</a>")
            return
        self.respond("<h1>导入 Home Assistant</h1><p>安装自定义集成后，在 HA 的“添加集成”中选择 Mansion Security，"
                     "复制并粘贴下面的完整内容。</p><p class='message'>会话码包含账号访问密钥和配对资料，等同凭据。"
                     "请保存在自己的密码管理器中，不要发到聊天或公开日志里。</p>"
                     "<label for='session_code'>私密会话导入码</label>"
                     f"<textarea id='session_code' readonly spellcheck='false' autocomplete='off'>{html.escape(code)}</textarea>"
                     "<p><small>点击框内，按 Ctrl+A、Ctrl+C 复制。这个页面不会重新登录、改变密码或切换应答模式。"
                     "完成导入后可关闭工具；HA 使用自己的会话副本独立运行。</small></p>"
                     f"<a href='{self.server.route}'>返回设置</a>")

    def registration_form(self, message=""):
        self.respond("<h1>重新完成账号登记</h1><p>仅在 HA 提示会话失效时使用。"
                     "此流程会实际修改账号密码和昵称；请先在 HA 中停用集成，并保持手机 App 退出。</p>" + message +
                     f"<form method='post' action='{self.server.route}/register'>"
                     f"<input type='hidden' name='csrf' value='{self.server.csrf}'>"
                     "<label for='name'>用户 ID</label><input id='name' name='name' autocomplete='username' required maxlength='256'>"
                     "<label for='old_password'>当前密码</label><input id='old_password' name='old_password' type='password' autocomplete='current-password' required maxlength='256'>"
                     "<label for='new_password'>新密码</label><input id='new_password' name='new_password' type='password' autocomplete='new-password' required minlength='8' maxlength='32'>"
                     "<small>8–32 位半角字母、数字或符号，需与当前密码不同。</small>"
                     "<label for='confirm'>再次输入新密码</label><input id='confirm' name='confirm' type='password' autocomplete='new-password' required minlength='8' maxlength='32'>"
                     "<label for='nickname'>昵称</label><input id='nickname' name='nickname' value='Home Assistant' required maxlength='32'>"
                     "<p><a href='https://scmresource-prod.10fw.jp/contracttext.html' target='_blank' rel='noreferrer'>查看与 App 相同的注意事项</a></p>"
                     "<label style='display:flex;gap:12px;font-weight:400'><input name='confirm_change' type='checkbox' value='yes' required style='width:20px;min-height:20px;flex:none'>"
                     "<span>我已阅读注意事项，并确认重新配对、修改密码和昵称。</span></label>"
                     "<button type='submit'>配对、修改密码并完成登记</button></form>"
                     "<p><small>密码只用于本次请求，不写入文件。完成后请记住新密码。</small></p>")

    def home(self):
        try:
            load_session()
        except FileNotFoundError:
            self.seed_form()
            return
        status = read_status()
        if status.get("password_change_uncertain"):
            self.respond("<h1>需要确认密码状态</h1><p class='message error'>上次修改密码时连接中断，"
                         "无法确认服务端结果。请用官方 App 确认哪组密码有效并正常退出，再处理重新登记。工具不会自动重复改密。</p>")
            return
        if status.get("password_changed") and status.get("stage") != "complete":
            self.respond("<h1>继续完成登记</h1><p>密码已修改。下面只重试配对和读取，不会再次修改密码。</p>" +
                         self.action_form("finish", "重试剩余登记"))
            return
        self.respond("<h1>Mansion Security 会话</h1><p>现有账号登记资料已保存在这台电脑。"
                     "常规导入和 HA 重启不需要再次登录。</p>"
                     f"<p><a href='{self.server.route}/export'>查看并复制 HA 私密会话码</a></p>"
                     f"<p><a href='{self.server.route}/reauth'>会话失效后，重新完成账号登记</a></p>"
                     "<p><small>正常配对续期由 HA 自动执行；本工具无需常驻。</small></p>")

    def do_GET(self):
        if self.headers.get("Host") != self.server.host_header:
            self.send_error(404)
            return
        if self.path == self.server.route:
            self.home()
        elif self.path == self.server.route + "/export":
            self.export_page()
        elif self.path == self.server.route + "/reauth":
            status = read_status()
            if status.get("password_change_uncertain") or (status.get("password_changed") and status.get("stage") != "complete"):
                self.home()
            else:
                self.registration_form()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.headers.get("Host") != self.server.host_header or self.path not in {
            self.server.route + suffix for suffix in ("/import", "/register", "/finish")
        }:
            self.send_error(403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 65536:
                raise ValueError
            form = parse_qs(self.rfile.read(size).decode("utf-8"), max_num_fields=9)
        except (ValueError, UnicodeError):
            self.send_error(400)
            return
        value = lambda field: form.get(field, [""])[0]
        if not hmac.compare_digest(value("csrf"), self.server.csrf):
            self.send_error(403)
            return
        if not self.server.operation_lock.acquire(blocking=False):
            self.respond("<h1>操作正在进行</h1><p>请等待当前请求完成，不要重复提交。</p>", 409)
            return
        try:
            status = read_status()
            if self.path.endswith("/import"):
                if status.get("password_change_uncertain") or (status.get("password_changed") and status.get("stage") != "complete"):
                    self.home()
                    return
                try:
                    save_session(decode_session(value("session_code")))
                except InvalidSession:
                    self.seed_form("<p class='message error'>会话码格式有误，请复制完整内容。</p>")
                    return
                # Imported codes were exported after normal registration. Validation
                # of whether this session is still live happens in the HA config flow.
                record("complete")
                self.home()
                return
            if self.path.endswith("/finish"):
                if not status.get("password_changed") or status.get("stage") == "complete":
                    self.home()
                    return
                finish_registration()
            else:
                if status.get("password_change_uncertain") or (status.get("password_changed") and status.get("stage") != "complete"):
                    self.home()
                    return
                name, old = value("name").strip(), value("old_password")
                new, nickname = value("new_password"), value("nickname").strip()
                if (not name or not old or value("confirm_change") != "yes" or new != value("confirm") or
                        new == old or not re.fullmatch(r"[\x21-\x7e]{8,32}", new) or not 1 <= len(nickname) <= 32):
                    self.registration_form("<p class='message error'>请核对所有字段及确认勾选。</p>")
                    return
                # Prevent a browser resubmitting a previously successful password
                # change using the same form/token.
                self.server.csrf = secrets.token_urlsafe(32)
                complete_registration(name, old, new, nickname)
            self.respond("<h1>独立读取成功</h1><p>账号登记已经完成。</p>"
                         f"<a href='{self.server.route}/export'>查看并复制新的 HA 会话码</a>")
        except Exception as error:
            code = error.code if isinstance(error, ApiFailure) else type(error).__name__
            status = read_status()
            stage = str(status.get("failed_stage", "local_operation"))
            self.respond("<h1>登记尚未完成</h1><p class='message error'>步骤：" + html.escape(stage) +
                         "<br>返回：" + html.escape(code) + "</p>"
                         f"<a href='{self.server.route}'>返回并查看恢复选项</a>")
        finally:
            self.server.operation_lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--route", default=None)
    args = parser.parse_args()
    server = SetupServer(args.port, args.route)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "setup-url.txt").write_text(server.url, encoding="utf-8")
    print("Local session page: " + server.url, flush=True)
    print("Keep this window open while copying the code. Ctrl+C closes the tool.", flush=True)
    if args.open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
