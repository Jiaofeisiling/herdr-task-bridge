import argparse
import os
import shlex
import subprocess
import sys


SSH_HOST = os.environ.get("NESI_SSH_HOST")
SENTINEL = os.environ.get("NESI_SENTINEL", "sentinel")

# Windows 上优先通过 WSL 使用 NeSI SSH 配置和 SSH multiplexing。
USE_WSL = os.environ.get("NESI_USE_WSL", "1") != "0"


def remote(command: str, capture=True):
    if not SSH_HOST:
        raise RuntimeError(
            "NESI_SSH_HOST is not set.\n"
            "Example in PowerShell:\n"
            '  $env:NESI_SSH_HOST="your-nesi-ssh-alias"'
        )

    if USE_WSL:
        cmd = ["wsl.exe", "ssh", SSH_HOST, command]
    else:
        cmd = ["ssh", SSH_HOST, command]

    result = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )

    if result.returncode != 0:
        if capture:
            print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)

    return result.stdout if capture else ""


def status():
    cmd = f"herdr agent get {shlex.quote(SENTINEL)}"
    print(remote(cmd), end="")


def prompt(text: str, timeout: int):
    cmd = (
        f"herdr agent prompt {shlex.quote(SENTINEL)} "
        f"{shlex.quote(text)} "
        f"--wait --timeout {timeout}"
    )

    print(remote(cmd), end="")


def read(lines: int):
    cmd = (
        f"herdr agent read {shlex.quote(SENTINEL)} "
        f"--source recent-unwrapped "
        f"--lines {lines}"
    )

    print(remote(cmd), end="")


def ask(text: str, timeout: int, lines: int):
    prompt(text, timeout)
    print("\n--- Sentinel output ---\n")
    read(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Windows ↔ NeSI Herdr Sentinel bridge"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")

    p_prompt = sub.add_parser("prompt")
    p_prompt.add_argument("text")
    p_prompt.add_argument("--timeout", type=int, default=120000)

    p_read = sub.add_parser("read")
    p_read.add_argument("--lines", type=int, default=80)

    p_ask = sub.add_parser("ask")
    p_ask.add_argument("text")
    p_ask.add_argument("--timeout", type=int, default=120000)
    p_ask.add_argument("--lines", type=int, default=80)

    args = parser.parse_args()

    if args.command == "status":
        status()

    elif args.command == "prompt":
        prompt(args.text, args.timeout)

    elif args.command == "read":
        read(args.lines)

    elif args.command == "ask":
        ask(args.text, args.timeout, args.lines)


if __name__ == "__main__":
    main()
