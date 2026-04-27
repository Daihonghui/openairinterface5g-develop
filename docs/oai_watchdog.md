# OAI gNB Python 看门狗说明

本文档说明 `tools/oai_watchdog.py` 的用途、执行流程、健康检查机制和使用方式。

## 目标

`tools/oai_watchdog.py` 用于守护 OAI gNB 进程。它负责：

1. 启动 gNB 命令。
2. 将 gNB 的 stdout/stderr 写入独立日志文件。
3. 检测 gNB 进程是否退出。
4. 通过 UDP `PING` 命令检测 gNB UDP 控制线程是否仍然响应。
5. 如果进程退出或心跳连续失败，自动终止并重启 gNB。
6. 记录 watchdog 自身日志和每次重启摘要。
7. 限制短时间内过多重启，避免无限快速重启。

## 依赖前提

gNB 侧需要已经包含本地 UDP 控制接口：

```text
127.0.0.1:19090/udp
```

其中 `PING` 命令应返回：

```text
PONG
```

这个接口由 `openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_primitives.c` 中的 UDP server 实现。

## 文件位置

```text
tools/oai_watchdog.py
docs/oai_watchdog.md
```

## 基本使用

示例：

```bash
python3 tools/oai_watchdog.py \
  --workdir /path/to/openairinterface5g-develop/cmake_targets/ran_build/build \
  --cmd "./nr-softmodem -E --rfsim -O /path/to/gnb.conf"
```

如果你的 gNB 启动需要 `sudo`：

```bash
python3 tools/oai_watchdog.py \
  --workdir /path/to/openairinterface5g-develop/cmake_targets/ran_build/build \
  --cmd "sudo ./nr-softmodem -E --rfsim -O /path/to/gnb.conf"
```

建议在真实长期运行时配置免密码 sudo，或者直接用 systemd/root 启动 watchdog，否则 `sudo` 密码交互可能阻塞启动。

## 默认参数

主要默认值：

```text
log-dir               logs/oai_watchdog
health-host           127.0.0.1
health-port           19090
health-command        PING
health-expected       PONG
health-interval       2 秒
health-timeout        0.5 秒
max-health-failures   5 次
startup-grace         20 秒
restart-delay         5 秒
term-timeout          5 秒
max-restarts          20 次
restart-window        600 秒
```

## 执行流程

完整流程如下：

```text
watchdog 启动
  |
  | subprocess.Popen 启动 gNB
  v
gNB 运行
  |
  | stdout/stderr 写入 logs/oai_watchdog/oai_时间戳.log
  v
watchdog 主循环
  |
  | proc.poll()
  | 如果进程退出，记录退出码并重启
  v
如果进程仍在
  |
  | 跳过 startup-grace 启动宽限期
  v
发送 UDP: PING
  |
  | 收到 PONG
  |   health_failures = 0
  |
  | 未收到 PONG
  |   health_failures += 1
  v
连续失败达到 max-health-failures
  |
  | SIGTERM 终止 gNB 进程组
  | 等待 term-timeout
  | 如果还未退出，SIGKILL
  v
等待 restart-delay
  |
  v
重新启动 gNB
```

## 进程存活检测

watchdog 使用：

```python
exit_code = proc.poll()
```

判断 gNB 进程是否退出。

如果 `exit_code is None`，说明进程仍在运行。

如果 `exit_code is not None`，说明进程已经退出。常见情况：

```text
0    正常退出
-11  SIGSEGV，通常是段错误
-9   SIGKILL
-15  SIGTERM
```

进程退出后，watchdog 会记录：

```text
gNB exited pid=... exit_code=... uptime=...s
```

并写入：

```text
logs/oai_watchdog/restart_summary.log
```

## UDP 心跳检测

watchdog 默认每 2 秒发送一次：

```text
PING
```

目标地址：

```text
127.0.0.1:19090
```

核心代码：

```python
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
```

如果返回内容包含 `PONG`，认为健康。

如果 UDP 超时或返回内容不符合预期，认为失败一次。连续失败达到：

```text
--max-health-failures
```

之后，watchdog 会认为 gNB 不可用，执行重启。

## 启动宽限期

gNB 启动后，UDP server 可能需要一段时间才能绑定端口。因此 watchdog 默认在启动后的 20 秒内不做 UDP 健康检查：

```text
--startup-grace 20
```

如果你的 gNB 启动较慢，可以调大：

```bash
--startup-grace 60
```

## 终止和重启逻辑

当心跳连续失败后，watchdog 会先尝试温和终止：

```text
SIGTERM
```

等待：

```text
--term-timeout 5
```

如果 gNB 没有退出，再使用：

```text
SIGKILL
```

脚本启动 gNB 时使用：

```python
start_new_session=True
```

因此在 Linux 上会通过进程组终止：

```python
os.killpg(proc.pid, signal.SIGTERM)
os.killpg(proc.pid, signal.SIGKILL)
```

这样可以避免只杀掉 shell，而留下 gNB 子进程。

## 重启限流

为了避免程序持续崩溃后无限快速重启，watchdog 有重启限流：

```text
--max-restarts 20
--restart-window 600
```

含义是：

```text
600 秒内最多允许 20 次启动
```

超过后 watchdog 退出，返回码为 `2`。

## 日志文件

默认日志目录：

```text
logs/oai_watchdog
```

包含：

```text
watchdog.log
```

watchdog 自身日志，包括启动、退出、心跳失败、重启原因。

```text
oai_YYYYmmdd_HHMMSS.log
```

每次 gNB 启动对应一个独立日志文件，保存 gNB stdout/stderr。

```text
restart_summary.log
```

每次进程退出或心跳超时的摘要，包括原因、退出码、运行时长和对应 gNB 日志路径。

## 示例：使用 RNTI 作为健康检查

如果想让 watchdog 检查更深一点的 MAC/RA 查询路径，可以把心跳命令改成 `RNTI`：

```bash
python3 tools/oai_watchdog.py \
  --cmd "./nr-softmodem -E --rfsim -O /path/to/gnb.conf" \
  --health-command RNTI \
  --health-expected "OK count="
```

区别：

```text
PING
  只确认 UDP server 线程可响应。

RNTI
  会访问 MAC 上下文并扫描 RA 数组。
```

当前需求只要求简单心跳，因此默认使用 `PING`。

## 手动验证

先确认 gNB UDP 接口可用：

```bash
printf "PING\n" | nc -u -w1 127.0.0.1 19090
```

预期返回：

```text
PONG
```

启动 watchdog 后，可以手动 kill gNB 验证自动重启：

```bash
kill -11 <gnb_pid>
```

watchdog 应记录退出码并重新启动 gNB。

也可以临时让 UDP 不响应，验证心跳失败重启逻辑。

## 注意事项

1. `--cmd` 是完整 gNB 启动命令，脚本内部使用 shell 执行。
2. 如果命令需要 `sudo`，必须处理好 sudo 密码问题。
3. 如果 gNB 启动很慢，应调大 `--startup-grace`。
4. 这个 watchdog 负责进程级恢复，不负责定位崩溃根因。
5. 需要定位崩溃原因时，应额外启用 core dump 或保存 gdb backtrace。
