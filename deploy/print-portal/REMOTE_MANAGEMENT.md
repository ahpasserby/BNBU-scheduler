# 香橙派远程管理

## 日常使用

这台 Mac 已配置 SSH 别名，联网后执行：

```bash
ssh zero3-remote
```

该连接经云端跳转到香橙派，不需要连接 `MAXCOURSE-Debug` 或处于校园内网。它使用 Mac 已有的 `~/.ssh/id_ed25519` 密钥以及现有云端 SSH 权限，未复制私钥。其他电脑需要分别取得云端与设备的授权密钥，不能只复制此别名后访问。

可直接检查打印服务：

```bash
ssh zero3-remote 'systemctl status maxcourse-print-agent maxcourse-print-tunnel --no-pager'
```

文件传输同样支持 SSH 别名。香橙派必须保持供电和校园有线连接。校园 DHCP 地址变化不影响通道，因为设备主动连接固定云端地址。

## 通道结构

管理通道独立于打印通道：

| 用途 | 云端监听 | 设备目标 | 设备服务 |
| --- | --- | --- | --- |
| 网页打印 | `127.0.0.1:18765` | `127.0.0.1:18765` | `maxcourse-print-tunnel.service` |
| SSH 管理 | `127.0.0.1:18766` | `127.0.0.1:22` | `maxcourse-management-tunnel.service` |

两条通道使用不同的云端系统账号、设备系统账号、SSH 密钥及状态目录。管理通道的系统账号为 `maxcourse-device-tunnel`，设备端没有交互登录 shell。

云端该账号只允许反向监听 `127.0.0.1:18766`，不能打开 shell、终端、代理转发或其他监听端口。没有新增公网 SSH 端口。设备通过回环地址收到的 SSH 连接只允许公钥认证，禁止密码与键盘交互认证。普通调试热点入口的现有登录配置保留。

Mac 通过已验证的设备 SSH 连接取得设备主机公钥，固定到 `HostKeyAlias maxcourse-zero3-device`，没有关闭主机密钥验证。私钥没有上传到云端或 Git。

## 配置位置

- 设备 systemd 单元：`/etc/systemd/system/maxcourse-management-tunnel.service`
- 设备连接配置：`/etc/maxcourse-device-tunnel/tunnel.env`
- 设备密钥与云端主机公钥：`/var/lib/maxcourse-device-tunnel/`
- 云端 SSH 限制：`/etc/ssh/sshd_config` 的 `MAXCOURSE DEVICE MANAGEMENT` 标记块
- 云端授权公钥：`/var/lib/maxcourse-device-tunnel/.ssh/authorized_keys`
- 设备 SSH 公钥限制：`/etc/ssh/sshd_config` 的 `MAXCOURSE LOOPBACK MANAGEMENT` 标记块
- Mac 别名：`~/.ssh/config` 的 `MAXCOURSE REMOTE DEVICE` 标记块

SSH 配置修改前均已备份，并通过 `sshd -t` 后重载。设备和云端备份位于 `/root/maxcourse-print-backups/`，Mac 备份位于 `~/.ssh/config.backup-maxcourse-management-*`。SSH 密钥及配置目录只允许所属账号访问。

设备环境文件中的字段为：

```text
MANAGEMENT_TUNNEL_USER=maxcourse-device-tunnel
MANAGEMENT_TUNNEL_HOST=103.106.188.87
```

## 恢复与停用

服务已设置开机自启。SSH 心跳为 20 秒，连续 3 次失败后退出，systemd 在 10 秒后重新启动连接。完整断电重启未在本轮执行。

若要停用管理通道，在设备上执行：

```bash
sudo systemctl disable --now maxcourse-management-tunnel.service
```

从远程执行会断开当前管理连接，打印通道继续独立运行。

## 2026-09-30 验收

- 通过 `ssh zero3-remote` 返回 `orangepizero3` 和 root 用户，SSH 服务端看到的连接来源为 `127.0.0.1`，证实实际经过云端回环转发。
- 核对云端只绑定 `127.0.0.1:18766`，有效 SSH 策略为仅反向转发、指定监听、禁止会话与密码登录。
- 核对设备回环 SSH 为 `AuthenticationMethods publickey`，密码与键盘交互认证均关闭。
- 强制终止管理通道进程后，观察到连接暂时失败，随后新进程自动启动，`NRestarts` 从 0 增至 1，远程命令恢复成功。
- 经云端进行文件上传并核对 SHA-256 一致，临时文件已删除。
- 打印公网页面会话接口返回 200，设备仍为 `ready=true`、`demo=false`。
- 对照修改前备份，原有云端及本地 `zero3-root` 别名的用户 SSH 配置保持一致。
