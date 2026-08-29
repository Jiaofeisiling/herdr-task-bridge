import json
import os
import re
import subprocess
import uuid

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HOST = "127.0.0.1"
PORT = int(os.environ.get("SENTINEL_BRIDGE_PORT", "8765"))

HERDR = os.environ.get("HERDR_BIN", "herdr")
SENTINEL = os.environ.get("SENTINEL_AGENT", "sentinel")


def run_herdr(*args, timeout=None):
    result = subprocess.run(
        [HERDR, *args],
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def build_delegation_prompt(task, task_id):
    token = task_id.replace("-", "")

    return f"""
你正在接受来自 Windows Codex 的远程委派任务。

Task token:
{token}

任务：
{task}

请正常完成任务。

完成后：
1. 简洁总结结果。
2. 如果执行了重要操作，请说明。
3. 如果涉及 Slurm job，请给出 job ID。
4. 如果修改了文件，请列出文件。
5. 如果被阻塞，请明确说明原因和 Windows Codex 下一步需要做什么。

最后一行必须是：
字面前缀 SENTINEL_DONE_ 后立即连接上面的 Task token，中间不得有空格。

最后一行之后不要输出其他内容。
""".strip()

def extract_task_response(raw, task_id):
    token = task_id.replace("-", "")
    marker = f"SENTINEL_DONE_{token}"

    pos = raw.rfind(marker)

    if pos == -1:
        return None

    before = raw[:pos]

    # Claude Code 的最终回答通常以 "● " 开始。
    assistant_pos = before.rfind("\n● ")

    if assistant_pos == -1:
        assistant_pos = before.rfind("● ")

    if assistant_pos != -1:
        response = before[assistant_pos + 2:].strip()
    else:
        # fallback：至少返回 marker 前面的最近文本
        response = before[-4000:].strip()

    return response


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # 保留最基本日志即可。
        print(
            f"{self.client_address[0]} "
            f"{self.command} "
            f"{self.path}"
        )

    def send_json(self, obj, status=200):
        data = json.dumps(
            obj,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header(
            "Content-Length",
            str(len(data))
        )
        self.end_headers()

        self.wfile.write(data)

    def read_json(self):
        length = int(
            self.headers.get("Content-Length", "0")
        )

        raw = self.rfile.read(length)

        if not raw:
            return {}

        return json.loads(raw.decode("utf-8"))

    def do_GET(self):

        if self.path == "/health":
            self.send_json({
                "ok": True,
                "service": "nesi-sentinel-bridge",
                "version": 2,
                "agent": SENTINEL,
            })
            return

        if self.path == "/status":
            result = run_herdr(
                "agent",
                "get",
                SENTINEL,
            )

            self.send_json(
                result,
                200 if result["ok"] else 500,
            )
            return

        if self.path.startswith("/read"):
            result = run_herdr(
                "agent",
                "read",
                SENTINEL,
                "--source",
                "recent-unwrapped",
                "--lines",
                "120",
            )

            self.send_json(
                result,
                200 if result["ok"] else 500,
            )
            return

        self.send_json(
            {"error": "not found"},
            404,
        )

    def do_POST(self):

        if self.path not in (
            "/prompt",
            "/ask",
        ):
            self.send_json(
                {"error": "not found"},
                404,
            )
            return

        try:
            body = self.read_json()

            task = body["task"].strip()

            timeout_ms = int(
                body.get(
                    "timeout_ms",
                    120000,
                )
            )

            read_lines = int(
                body.get(
                    "lines",
                    500,
                )
            )

            if not task:
                raise ValueError(
                    "task cannot be empty"
                )

        except Exception as e:
            self.send_json(
                {
                    "ok": False,
                    "error": f"invalid request: {e}",
                },
                400,
            )
            return

        task_id = str(uuid.uuid4())

        delegated_prompt = build_delegation_prompt(
            task,
            task_id,
        )

        try:
            prompt_result = run_herdr(
                "agent",
                "prompt",
                SENTINEL,
                delegated_prompt,
                "--wait",
                "--timeout",
                str(timeout_ms),
                timeout=(timeout_ms / 1000) + 15,
            )

        except subprocess.TimeoutExpired:
            self.send_json(
                {
                    "ok": False,
                    "task_id": task_id,
                    "error":
                        "bridge timeout while waiting for Sentinel",
                },
                504,
            )
            return

        if not prompt_result["ok"]:
            self.send_json(
                {
                    "ok": False,
                    "task_id": task_id,
                    "error":
                        "Herdr prompt command failed",
                    "prompt": prompt_result,
                },
                500,
            )
            return

        if self.path == "/prompt":
            self.send_json({
                "ok": True,
                "task_id": task_id,
                "prompt": prompt_result,
            })
            return

        read_result = run_herdr(
            "agent",
            "read",
            SENTINEL,
            "--source",
            "recent-unwrapped",
            "--lines",
            str(read_lines),
        )

        if not read_result["ok"]:
            self.send_json(
                {
                    "ok": False,
                    "task_id": task_id,
                    "error":
                        "Unable to read Sentinel output",
                    "read": read_result,
                },
                500,
            )
            return

        sentinel_response = extract_task_response(
            read_result["stdout"],
            task_id,
        )

        if sentinel_response is None:
            self.send_json(
                {
                    "ok": False,
                    "task_id": task_id,
                    "error":
                        "Sentinel completed but did not emit "
                        "the completion marker.",
                    "raw_output":
                        read_result["stdout"],
                },
                502,
            )
            return

        self.send_json({
            "ok": True,
            "task_id": task_id,
            "result": {
                "text": sentinel_response
            },
        })


if __name__ == "__main__":
    print("Sentinel Bridge v2")
    print(f"Agent: {SENTINEL}")
    print(
        f"Listening: "
        f"http://{HOST}:{PORT}"
    )

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler,
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print("\nStopping.")
