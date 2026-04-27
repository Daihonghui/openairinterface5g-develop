# RA UDP 接入控制接口说明

本文档说明本次新增的 gNB 本地 UDP 控制接口。这个接口用于你的 Msg3 后接入认证实验：外部 Python 程序可以从 gNB 查询当前随机接入过程中的 RNTI，并在认证失败后通过 UDP 命令请求 gNB 释放 UE 或清理 RA 过程。

这个方案不依赖 telnet server，不需要新增 telnet 动态库，也不需要修改 `common/utils/telnetsrv/CMakeLists.txt`。

## 目标

当前需求是：

1. UE 完成 Msg3 上行发送后，gNB 内部已经有对应的 `ra->rnti`。
2. Python 程序需要拿到这个 RNTI。
3. Python 完成外部认证后，如果认证失败，向 gNB 发送拒绝命令。
4. gNB 根据 RNTI 找到正在进行的 RA 或已经创建的 UE context，然后释放 UE / 清理 RA。

本次实现使用一个本机 UDP 控制口：

```text
127.0.0.1:19090/udp
```

gNB 启动后会自动监听这个端口。Python 在同一台机器上发送 UDP 文本命令即可。

## 涉及文件

本次 UDP 方案只修改 MAC 侧已有代码：

```text
openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_primitives.c
openair2/LAYER2/NR_MAC_gNB/mac_proto.h
openair2/LAYER2/NR_MAC_gNB/main.c
```

其中：

```text
gNB_scheduler_primitives.c
```

实现 UDP server、命令解析、RNTI 查询、reject 逻辑。

```text
mac_proto.h
```

声明新增函数。

```text
main.c
```

在 MAC 初始化流程中启动 UDP server。

## 对外命令

UDP server 支持三个主要命令。

心跳检测：

```text
PING
```

查询当前 RA RNTI：

```text
RNTI
```

或显式指定 `module_id` 和 `cc_id`：

```text
RNTI 0 0
```

拒绝某个 RNTI：

```text
REJECT 0x4601
```

或显式指定 `module_id` 和 `cc_id`：

```text
REJECT 0 0 0x4601
```

默认情况下，`module_id = 0`，`cc_id = 0`。

## 整体执行流程

从 Python 侧开始，完整流程如下：

```text
Python
  |
  | UDP: "PING"
  v
gNB UDP server
  |
  | 不访问 MAC 结构，直接返回 "PONG"
  v
Python 确认 UDP server 线程存活
  |
  | UDP: "RNTI"
  v
gNB UDP server
  |
  | 加 mac->sched_lock
  | 扫描 RC.nrmac[0]->common_channels[0].ra[]
  | 找 ra_state != nrRA_gNB_IDLE 的 RA procedure
  v
gNB 返回当前 RA RNTI 列表
  |
  | UDP response: "RNTI ... rnti=0x4601 ..."
  v
Python 解析 rnti=0x4601
  |
  | 执行外部认证逻辑
  v
认证失败
  |
  | UDP: "REJECT 0x4601"
  v
gNB UDP server
  |
  | 调用 nr_mac_reject_access_by_rnti(0, 0, 0x4601)
  v
gNB 查找 RA / UE context
  |
  | 找到后释放 UE 或清理 RA
  v
Python 收到 "OK rejected module=0 cc=0 rnti=0x4601"
```

这个流程是异步的。Python 什么时候发送 `REJECT`，gNB 就什么时候执行拒绝逻辑。它可以用于本地认证实验，但不能绝对保证一定发生在 Msg4 发送之前。

## PING 心跳逻辑

Python 发送：

```text
PING
```

gNB 侧在 UDP 命令解析函数中直接返回：

```text
PONG
```

关键代码：

```c
if (strncasecmp(cursor, "PING", 4) == 0) {
  cursor += 4;
  while (isspace((unsigned char)*cursor))
    cursor++;
  if (*cursor != '\0') {
    snprintf(reply, reply_len, "ERROR usage: PING\n");
    return;
  }

  snprintf(reply, reply_len, "PONG\n");
  return;
}
```

`PING` 不会访问 `RC.nrmac`，也不会加 `mac->sched_lock`。它只用于确认：

```text
gNB 进程还在
UDP server 线程还在
本机 UDP 控制端口可收发
```

如果 watchdog 只是想判断 UDP 控制接口是否还活着，可以使用 `PING`。如果还想验证 MAC/RA 查询路径是否正常，可以继续使用 `RNTI`。

## gNB 启动 UDP Server

在 `openair2/LAYER2/NR_MAC_gNB/main.c` 的 `mac_top_init_gNB()` 中，增加了启动调用：

```c
du_init_f1_ue_data();
nr_mac_start_ra_udp_server();

srand48(0);
```

`nr_mac_start_ra_udp_server()` 的声明在 `mac_proto.h`：

```c
void nr_mac_start_ra_udp_server(void);
```

启动函数内部会创建一个 detached pthread：

```c
void nr_mac_start_ra_udp_server(void)
{
  if (ra_udp_server_started)
    return;

  int ret = pthread_create(&ra_udp_server_thread, NULL, nr_mac_ra_udp_server, NULL);
  if (ret != 0) {
    LOG_E(NR_MAC, "Cannot start RA UDP control thread: %s\n", strerror(ret));
    return;
  }

  pthread_detach(ra_udp_server_thread);
  ra_udp_server_started = true;
}
```

UDP server 线程监听本机地址：

```c
#define NR_RA_UDP_CONTROL_PORT 19090
```

关键绑定代码：

```c
struct sockaddr_in addr = {0};
addr.sin_family = AF_INET;
addr.sin_port = htons(NR_RA_UDP_CONTROL_PORT);
addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
  LOG_E(NR_MAC, "Cannot bind RA UDP control socket on 127.0.0.1:%d: %s\n",
        NR_RA_UDP_CONTROL_PORT,
        strerror(errno));
  close(fd);
  return NULL;
}
```

启动成功后，gNB 日志会出现：

```text
RA UDP control socket listening on 127.0.0.1:19090
```

## RNTI 查询逻辑

Python 发送：

```text
RNTI
```

gNB 侧进入 UDP 命令解析函数：

```c
static void nr_mac_handle_ra_udp_command(char *cmd, char *reply, size_t reply_len)
```

当命令是 `RNTI` 或 `GET_RNTI` 时，调用：

```c
nr_mac_dump_ra_rnti(module_id, CC_id, reply, reply_len);
```

RNTI 查询函数的核心逻辑：

```c
static size_t nr_mac_dump_ra_rnti(module_id_t module_id, int CC_id, char *buf, size_t len)
{
  if (!ra_udp_valid_mac_context(module_id, CC_id))
    return snprintf(buf, len, "ERROR invalid MAC context module=%d cc=%d\n", module_id, CC_id);

  gNB_MAC_INST *mac = RC.nrmac[module_id];
  NR_SCHED_LOCK(&mac->sched_lock);

  size_t used = 0;
  int count = 0;
  NR_COMMON_channels_t *cc = &mac->common_channels[CC_id];
  for (int i = 0; i < NR_NB_RA_PROC_MAX && used < len; i++) {
    NR_RA_t *ra = &cc->ra[i];
    if (ra->ra_state == nrRA_gNB_IDLE)
      continue;

    int written = snprintf(buf + used,
                           len - used,
                           "RNTI module=%d cc=%d ra_index=%d state=%s rnti=0x%04x ra_rnti=0x%04x msg3=%d.%d timer=%d type=%s cfra=%d\n",
                           module_id,
                           CC_id,
                           i,
                           nrra_text[ra->ra_state],
                           ra->rnti,
                           ra->RA_rnti,
                           ra->Msg3_frame,
                           ra->Msg3_slot,
                           ra->contention_resolution_timer,
                           ra_udp_type_name(ra->ra_type),
                           ra->cfra);
    if (written < 0)
      break;
    size_t remaining = len - used;
    used += (size_t)written < remaining ? (size_t)written : remaining;
    count++;
  }

  NR_SCHED_UNLOCK(&mac->sched_lock);

  if (count == 0 && used < len)
    used += snprintf(buf + used, len - used, "NO_RNTI module=%d cc=%d\n", module_id, CC_id);
  if (used < len)
    used += snprintf(buf + used, len - used, "OK count=%d\n", count);
  return used;
}
```

这里有两个重点：

1. 查询时会加 `mac->sched_lock`，避免和 MAC scheduler 并发访问 RA 数组。
2. 查询的是 `cc->ra[]` 数组中所有 `ra_state != nrRA_gNB_IDLE` 的 RA procedure。

返回示例：

```text
RNTI module=0 cc=0 ra_index=0 state=Msg4 rnti=0x4601 ra_rnti=0x003a msg3=123.17 timer=42 type=4-step cfra=0
OK count=1
```

字段说明：

```text
rnti=0x4601
```

这是 Python 后续调用 `REJECT` 时要使用的 RNTI。

```text
state=Msg4
```

当前 RA 状态，例如 `WAIT_Msg3`、`Msg4`、`WAIT_Msg4_MsgB_ACK`。

```text
msg3=123.17
```

Msg3 预期或记录的 frame.slot。

```text
timer=42
```

当前 RA contention resolution timer。

如果没有正在进行的 RA：

```text
NO_RNTI module=0 cc=0
OK count=0
```

## Reject 命令逻辑

Python 发送：

```text
REJECT 0x4601
```

gNB 解析 RNTI 后调用：

```c
bool rejected = nr_mac_reject_access_by_rnti(module_id, CC_id, rnti);
```

函数声明：

```c
bool nr_mac_reject_access_by_rnti(module_id_t module_id, int CC_id, rnti_t rnti);
```

核心实现：

```c
bool nr_mac_reject_access_by_rnti(module_id_t module_id, int CC_id, rnti_t rnti)
{
  if (!RC.nrmac || module_id >= RC.nb_nr_macrlc_inst || !RC.nrmac[module_id] || CC_id < 0 || CC_id >= MAX_NUM_CCs)
    return false;

  gNB_MAC_INST *mac = RC.nrmac[module_id];

  NR_SCHED_LOCK(&mac->sched_lock);

  NR_COMMON_channels_t *cc = &mac->common_channels[CC_id];
  for (int i = 0; i < NR_NB_RA_PROC_MAX; i++) {
    NR_RA_t *ra = &cc->ra[i];
    if (ra->ra_state == nrRA_gNB_IDLE || ra->rnti != rnti)
      continue;

    LOG_W(NR_MAC, "External access rejection for RA RNTI 0x%04x at RA index %d state %s\n", rnti, i, nrra_text[ra->ra_state]);
    NR_UE_info_t *UE = find_nr_UE(&mac->UE_info, rnti);
    if (UE)
      nr_mac_release_ue(mac, rnti);
    else
      nr_clear_ra_proc(ra);

    NR_SCHED_UNLOCK(&mac->sched_lock);
    return true;
  }

  NR_UE_info_t *UE = find_nr_UE(&mac->UE_info, rnti);
  if (UE) {
    LOG_W(NR_MAC, "External access rejection for established UE RNTI 0x%04x\n", rnti);
    bool requested = nr_mac_request_release_ue(mac, rnti);
    if (!requested)
      nr_mac_release_ue(mac, rnti);

    NR_SCHED_UNLOCK(&mac->sched_lock);
    return true;
  }

  NR_SCHED_UNLOCK(&mac->sched_lock);
  return false;
}
```

返回给 Python 的成功响应：

```text
OK rejected module=0 cc=0 rnti=0x4601
```

找不到对应 RA 或 UE：

```text
NOT_FOUND module=0 cc=0 rnti=0x4601
```

## UE 释放和 RA 清理逻辑

Reject 接口处理两种主要情况。

### 情况一：RA 还在进行中

如果在 `cc->ra[]` 中找到：

```c
ra->ra_state != nrRA_gNB_IDLE && ra->rnti == rnti
```

说明这个 RNTI 仍然和一个 RA procedure 相关。

此时继续判断 UE context 是否已经创建：

```c
NR_UE_info_t *UE = find_nr_UE(&mac->UE_info, rnti);
if (UE)
  nr_mac_release_ue(mac, rnti);
else
  nr_clear_ra_proc(ra);
```

如果 UE context 已经存在，调用：

```c
nr_mac_release_ue(mac, rnti);
```

这个函数内部会：

```c
nr_rlc_remove_ue(rnti);
mac_remove_nr_ue(mac, rnti);
```

而 `mac_remove_nr_ue()` 会从 MAC UE 列表删除 UE，并遍历 RA 数组：

```c
if (ra->rnti == UE->rnti) {
  nr_clear_ra_proc(ra);
  ra->cfra = 0;
}
```

所以，如果 UE context 已经创建，`nr_mac_release_ue()` 会同时完成：

```text
删除 RLC UE
删除 MAC UE
清理对应 RA procedure
```

如果 UE context 还不存在，说明 RA 结构里有 RNTI，但还没有完整 UE context。这时直接调用：

```c
nr_clear_ra_proc(ra);
```

它会把 RA procedure 清回 idle：

```c
memset(ra, 0, sizeof(*ra));
ra->ra_state = nrRA_gNB_IDLE;
```

### 情况二：没有找到 RA，但找到已存在 UE

如果 RA 数组里找不到匹配项，但 MAC UE 列表里能找到 UE：

```c
NR_UE_info_t *UE = find_nr_UE(&mac->UE_info, rnti);
```

说明 UE 可能已经走过 RA，或者 RA 已经被清掉。此时逻辑是：

```c
bool requested = nr_mac_request_release_ue(mac, rnti);
if (!requested)
  nr_mac_release_ue(mac, rnti);
```

`nr_mac_request_release_ue()` 会尝试向 CU/RRC 发起 UE release request。如果 CU 已经知道这个 UE，应优先走正常 release 流程。

如果返回 `false`，说明 CU 不知道这个 UE，例如临时 UE 或还没有建立完整 F1/RRC 上下文。这时直接调用：

```c
nr_mac_release_ue(mac, rnti);
```

完成本地 MAC/RLC 删除。

## Python 调用示例

下面是完整 Python 示例。它先查询当前 RA RNTI，然后执行认证逻辑，认证失败时发送 `REJECT`。

```python
import re
import socket


GNB_ADDR = ("127.0.0.1", 19090)


def udp_cmd(cmd: str, timeout: float = 0.5) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(cmd.encode(), GNB_ADDR)
        data, _ = sock.recvfrom(4096)
        return data.decode(errors="ignore")


def get_ra_rntis() -> list[str]:
    out = udp_cmd("RNTI")
    print(out)

    rntis = []
    for line in out.splitlines():
        match = re.search(r"rnti=(0x[0-9a-fA-F]{1,4})", line)
        if match:
            rntis.append(match.group(1))
    return rntis


def ping_gnb() -> bool:
    out = udp_cmd("PING")
    print(out)
    return out.strip() == "PONG"


def reject_rnti(rnti_hex: str) -> bool:
    out = udp_cmd(f"REJECT {rnti_hex}")
    print(out)
    return "OK rejected" in out


def authenticate(rnti_hex: str) -> bool:
    # 这里替换成你的外部认证逻辑。
    # True 表示认证通过，允许继续接入。
    # False 表示认证失败，需要拒绝接入。
    return False


if __name__ == "__main__":
    if not ping_gnb():
        raise RuntimeError("gNB UDP server is not responding")

    for rnti in get_ra_rntis():
        if not authenticate(rnti):
            reject_rnti(rnti)
```

如果需要显式指定 `module_id` 和 `cc_id`：

```python
print(udp_cmd("PING"))
print(udp_cmd("RNTI 0 0"))
print(udp_cmd("REJECT 0 0 0x4601"))
```

## 手动测试

Linux 上可以用 `nc` 直接测试：

```bash
printf "RNTI\n" | nc -u -w1 127.0.0.1 19090
printf "PING\n" | nc -u -w1 127.0.0.1 19090
printf "REJECT 0x4601\n" | nc -u -w1 127.0.0.1 19090
```

gNB 必须正在运行，并且日志中应该能看到：

```text
RA UDP control socket listening on 127.0.0.1:19090
```

## 时序说明

这个 UDP 接口是异步接口。它的效果取决于 Python 发送 `REJECT` 的时机。

如果 Python 在 Msg4 生成之前发送 `REJECT`，则可能出现：

```text
gNB 清理 RA / 释放临时 UE
gNB 不再生成 Msg4
UE 等待 contention resolution 超时
UE 认为随机接入失败
```

如果 Python 返回较慢，Msg4 已经被调度或发送，则可能出现：

```text
Msg4 已经发出
gNB 随后释放 UE context
UE 可能短暂认为 RA 有进展，但后续无法继续正常接入
最终 UE 侧失败、重试或掉线
```

因此，这个 UDP 接口适合本地实验和异步接入拒绝，但它不严格保证一定在 Msg4 前完成拦截。

如果后续需要严格保证 Msg4 前拦截，需要在 Msg3 处理后增加同步认证等待点，或者增加 hold 标志/状态，让 gNB 在认证结果回来之前不进入 Msg4 调度。
