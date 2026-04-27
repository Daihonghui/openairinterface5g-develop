#!/usr/bin/env python3
import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def write_watchdog_log(log_dir: Path, message: str) -> None:
    line = f"[{now_text()}] {message}\n"
    print(line, end="", flush=True)
    with (log_dir / "watchdog.log").open("a", encoding="utf-8") as log:
        log.write(line)


def udp_health_check(host: str, port: int, command: str, expected: str, timeout: float) -> tuple[bool, str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto((command.strip() + "\n").encode(), (host, port))
            data, _ = sock.recvfrom(4096)
            text = data.decode(errors="ignore").strip()
            return expected in text, text
    except OSError as err:
        return False, str(err)


def start_gnb(cmd: str, workdir: Path | None, log_dir: Path) -> tuple[subprocess.Popen, Path]:
    log_path = log_dir / f"oai_{stamp()}.log"
    log_file = log_path.open("ab", buffering=0)
    proc = subprocess.Popen(
        cmd,
        cwd=str(workdir) if workdir else None,
        shell=True,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"),
    )
    proc._watchdog_log_file = log_file
    return proc, log_path


def close_process_log(proc: subprocess.Popen) -> None:
    log_file = getattr(proc, "_watchdog_log_file", None)
    if log_file:
        log_file.close()


def stop_gnb(proc: subprocess.Popen, term_timeout: float, log_dir: Path) -> None:
    if proc.poll() is not None:
        close_process_log(proc)
        return

    write_watchdog_log(log_dir, f"stopping gNB pid={proc.pid} with SIGTERM")
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=term_timeout)
    except subprocess.TimeoutExpired:
        write_watchdog_log(log_dir, f"gNB pid={proc.pid} did not exit after {term_timeout}s, sending SIGKILL")
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait()
    finally:
        close_process_log(proc)


def append_crash_summary(
    log_dir: Path,
    reason: str,
    exit_code: int | None,
    uptime: float,
    gnb_log: Path,
) -> None:
    summary = log_dir / "restart_summary.log"
    with summary.open("a", encoding="utf-8") as out:
        out.write(f"[{now_text()}] reason={reason}\n")
        out.write(f"exit_code={exit_code}\n")
        out.write(f"uptime_seconds={uptime:.1f}\n")
        out.write(f"gnb_log={gnb_log}\n\n")


def restart_limit_exceeded(restarts: deque[float], max_restarts: int, window: float) -> bool:
    now = time.time()
    while restarts and now - restarts[0] > window:
        restarts.popleft()
    return len(restarts) >= max_restarts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watchdog for OAI gNB process with UDP heartbeat.")
    parser.add_argument("--cmd", required=True, help="gNB startup command, for example './nr-softmodem -E --rfsim -O gnb.conf'")
    parser.add_argument("--workdir", help="Working directory for the gNB command")
    parser.add_argument("--log-dir", default="logs/oai_watchdog", help="Directory for watchdog and gNB logs")
    parser.add_argument("--restart-delay", type=float, default=5.0, help="Seconds to wait before restart")
    parser.add_argument("--term-timeout", type=float, default=5.0, help="Seconds to wait after SIGTERM before SIGKILL")
    parser.add_argument("--health-host", default="127.0.0.1", help="gNB UDP health host")
    parser.add_argument("--health-port", type=int, default=19090, help="gNB UDP health port")
    parser.add_argument("--health-command", default="PING", help="UDP command for heartbeat")
    parser.add_argument("--health-expected", default="PONG", help="Expected substring in heartbeat response")
    parser.add_argument("--health-interval", type=float, default=2.0, help="Seconds between health checks")
    parser.add_argument("--health-timeout", type=float, default=0.5, help="UDP heartbeat timeout")
    parser.add_argument("--max-health-failures", type=int, default=5, help="Consecutive heartbeat failures before restart")
    parser.add_argument("--startup-grace", type=float, default=20.0, help="Seconds to skip heartbeat after each start")
    parser.add_argument("--max-restarts", type=int, default=20, help="Maximum restarts within restart window")
    parser.add_argument("--restart-window", type=float, default=600.0, help="Restart limit window in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(args.workdir).resolve() if args.workdir else None

    restarts: deque[float] = deque()
    health_failures = 0

    while True:
        if restart_limit_exceeded(restarts, args.max_restarts, args.restart_window):
            write_watchdog_log(log_dir, f"restart limit exceeded: {args.max_restarts} restarts in {args.restart_window}s")
            return 2

        proc, gnb_log = start_gnb(args.cmd, workdir, log_dir)
        start_time = time.time()
        restarts.append(start_time)
        health_failures = 0
        write_watchdog_log(log_dir, f"started gNB pid={proc.pid} log={gnb_log}")

        while True:
            exit_code = proc.poll()
            uptime = time.time() - start_time
            if exit_code is not None:
                close_process_log(proc)
                write_watchdog_log(log_dir, f"gNB exited pid={proc.pid} exit_code={exit_code} uptime={uptime:.1f}s")
                append_crash_summary(log_dir, "process_exit", exit_code, uptime, gnb_log)
                break

            if uptime < args.startup_grace:
                time.sleep(args.health_interval)
                continue

            ok, response = udp_health_check(
                args.health_host,
                args.health_port,
                args.health_command,
                args.health_expected,
                args.health_timeout,
            )
            if ok:
                health_failures = 0
            else:
                health_failures += 1
                write_watchdog_log(
                    log_dir,
                    f"health check failed {health_failures}/{args.max_health_failures}: {response}",
                )

            if health_failures >= args.max_health_failures:
                uptime = time.time() - start_time
                write_watchdog_log(log_dir, f"gNB heartbeat failed, restarting pid={proc.pid}")
                append_crash_summary(log_dir, "heartbeat_timeout", proc.poll(), uptime, gnb_log)
                stop_gnb(proc, args.term_timeout, log_dir)
                break

            time.sleep(args.health_interval)

        write_watchdog_log(log_dir, f"restart after {args.restart_delay}s")
        time.sleep(args.restart_delay)


if __name__ == "__main__":
    sys.exit(main())
