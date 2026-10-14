"""A minimal SMTP server for tests: plain connection on 127.0.0.1, AUTH PLAIN, stores what it receives."""
import base64
import socketserver
import threading


class FakeSMTP:
    def __init__(self, username="para@example.test", password="right-password"):
        self.username, self.password, self.messages = username, password, []
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def reply(self, line):
                self.wfile.write((line + "\r\n").encode())

            def handle(self):
                self.reply("220 fake.smtp ready")
                authed, rcpts, sender = False, [], None
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode().rstrip("\r\n")
                    cmd = line.split(" ", 1)[0].upper()
                    if cmd in ("EHLO", "HELO"):
                        self.reply("250-fake.smtp")
                        self.reply("250 AUTH PLAIN")
                    elif cmd == "AUTH":
                        parts = line.split()
                        decoded = base64.b64decode(parts[2]).split(b"\0") if len(parts) > 2 else []
                        ok = len(decoded) == 3 and decoded[1].decode() == fake.username and decoded[2].decode() == fake.password
                        authed = ok
                        self.reply("235 ok" if ok else "535 authentication failed")
                    elif cmd == "MAIL":
                        if not authed:
                            self.reply("530 authenticate first")
                            continue
                        sender = line
                        self.reply("250 ok")
                    elif cmd == "RCPT":
                        rcpts.append(line.split(":", 1)[1].strip("<> "))
                        self.reply("250 ok")
                    elif cmd == "DATA":
                        self.reply("354 go ahead")
                        body = []
                        while True:
                            chunk = self.rfile.readline().decode()
                            if chunk in (".\r\n", ".\n", ""):
                                break
                            body.append(chunk)
                        fake.messages.append({"from": sender, "to": rcpts, "data": "".join(body)})
                        rcpts = []
                        self.reply("250 queued")
                    elif cmd in ("NOOP", "RSET"):
                        self.reply("250 ok")
                    elif cmd == "QUIT":
                        self.reply("221 bye")
                        return
                    else:
                        self.reply("502 not implemented")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
