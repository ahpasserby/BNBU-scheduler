# MAXCOURSE 网页打印

页面为 `/print/`，后端为 `/api/print/*`，香橘派执行端只监听 `127.0.0.1:18765`。默认关闭提交，只有正确配置私有通道、执行端和学校有线网络后才开放。

## 数据流与范围

1. 用户使用已绑定学校账号的 MAXCOURSE 会话登录。后端从用户表取得学校账号，不接受前端指定其他账号。
2. 选择 PDF 后，设备检查页数、加密状态和文件有效性。后端签发绑定用户、文件哈希和页数的短期检查凭证。
3. 用户确认页数及固定打印设置，输入本次学校密码，再提交。文档与密码仅在同步请求中流转，设备离线不排队保存。
4. 学校服务确认传输后，状态为“已交给学校队列”。用户需要本人刷卡取件。网站无法确认释放、纸张输出或扣费。

首版支持 PDF，10 MiB 以内、最多 50 页、A4、灰度、单面、单份。没有价格估算或站内付款。任务状态保留约 24 小时，服务运行时每分钟清理过期记录，读取时也会清理。文件名、文件内容和学校密码不存入任务数据库。设备临时文档位于 systemd 创建的 `/run/maxcourse-print-agent`，完成或异常后清理，服务停止及重启也会清理运行目录。

每个明确的提交意图有一个幂等编号，云端和执行端都去重。提交途中断线或执行端在发送时重启会保留“结果待确认”，不会自动重发。学校明确拒绝认证时才显示密码验证失败。

## 部署香橘派

先取得设备独立使用的校园网络地址，不能长期占用原机房电脑地址。保持现有独立调试热点。将本仓库的 `campus_print/` 与本目录传到设备，然后执行：

```bash
sudo bash deploy/print-portal/install-agent.sh /path/to/checkout
```

安装脚本建立 `maxcourse-print` 非登录用户，安装 Python、Samba 客户端、Ghostscript、Poppler 和 Bubblewrap。PDF 解析及转换在无网络的 Bubblewrap 环境运行，只挂载系统运行库、字体与当前任务目录；子进程另有 CPU、地址空间、输出文件和超时限制。若系统禁用用户命名空间导致沙箱不可用，执行端保持未就绪，不降级到无沙箱执行。

唯一设备令牌由安装脚本写入 `/etc/maxcourse-print-agent/agent.env`，权限为 root `0600`。不要把令牌打印、提交到 Git、放进网页或日志。将同一令牌安全配置到云端服务环境。安装脚本还会生成执行端 TLS 证书和私钥，私钥只允许服务用户读取。将公开的 `agent.crt` 通过已验证的 SSH 通道复制到云端，作为该执行端的专用 CA 文件，不能关闭 TLS 校验。证书有效期为两年，到期前需重新签发并更新云端信任文件。

## 配置私有通道

设备主动 SSH 连接 MAXCOURSE 主机，反向监听必须为云端回环地址 `127.0.0.1:18765`。不得把代理接口或 SMB 端口直接暴露到公网。

使用专用云端 SSH 用户和专用密钥，并在云端 sshd 的该用户 Match 块限制：

```text
Match User maxcourse-print-tunnel
    AllowTcpForwarding remote
    GatewayPorts no
    PermitListen 127.0.0.1:18765
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    MaxSessions 0
```

为该用户保留正常密钥认证，但不授予 sudo 或其他应用数据权限。修改 sshd 前运行 `sshd -t` 并保留现有管理会话。先用已验证渠道核对云端 SSH 主机公钥，再写入设备专用 `known_hosts`，不关闭主机密钥检查。

设备密钥路径为 `/var/lib/maxcourse-print-agent/tunnel_key`，只允许服务用户读取。设备 `/etc/maxcourse-print-agent/tunnel.env` 为 root `0600`，写入：

```text
PRINT_TUNNEL_USER=maxcourse-print-tunnel
PRINT_TUNNEL_HOST=YOUR_VERIFIED_CLOUD_HOST
```

配置完成后开启 `maxcourse-print-tunnel.service`。中断会重连，但没有自动重发打印任务。

## 启用云端入口

在现有 `maxcourse.service` 的私密环境文件中配置：

```text
MAXCOURSE_PRINT_ENABLED=1
MAXCOURSE_PRINT_AGENT_URL=https://127.0.0.1:18765
MAXCOURSE_PRINT_AGENT_CA=/etc/maxcourse-print-agent/agent.crt
MAXCOURSE_PRINT_AGENT_TOKEN=THE_SAME_PRIVATE_TOKEN
```

不要把占位值原样部署。测试模式不能在生产启用。云端仅允许配置回环 HTTPS 执行端，并校验专用执行端证书，同时关闭 Requests 的环境代理继承和自动重定向。即使另一进程占用了同一个回环端口，也无法冒充执行端读取学校密码。公共 API 需要有效登录、学校身份、CSRF、同源检查、页数检查凭证、限流和任务所有权。

现有 Flask 单进程服务会在一次提交中等待设备处理，最长约 150 秒。反向代理的打印 API 请求超时需至少 180 秒，body limit 为 16 MiB，保留 HTTPS。将本目录 `nginx-location-settings.conf` 的指令应用在打印 API 的代理 location 内，保留现有上游、头部、WAF 和限流配置，再运行 `nginx -t`。

必须关闭请求体磁盘缓冲，并使用 HTTP/1.1 转发和足够的内存缓冲，避免包含学校密码的 JSON 或 PDF 被写入 Nginx 的请求体临时目录。不要开启请求正文日志或把完整请求发送到错误监控。部署验收需一并检查代理层，不只检查 Flask。未来扩大吞吐量时应扩展执行端容量，不能直接重试不确定的任务。

## 验收

先保持 `MAXCOURSE_PRINT_ENABLED=0` 发布页面或本地预览，再部署设备和隧道。检查设备离线时不可提交。上线前验证两个学校账号互相看不到任务、错误密码不会自动重试、异常 PDF 被拒绝、重复请求只发送一次、发送后断线仍可查询、进程重启时不重发、文档和密码没有写入持久存储。

最后用本人学校账号提交一张真实文件，核对本人刷卡列表和实物。此前命令行测试成功不代替这条网页、私有通道及执行端的完整验收。

关闭入口可将 `MAXCOURSE_PRINT_ENABLED=0` 后重启云端服务。停用设备服务会清理临时文档，但已经送到学校队列的作业仍须在学校端处理，不能把停服务解释为取消学校作业。

## 本地预览与回归

```bash
python deploy/print-portal/preview.py --port 5019
```

预览固定绑定 `127.0.0.1`，使用临时数据库与模拟执行端，页面持续标明本地演示。演示登录为 `demo` / `demo`，默认已登录，不需要填写真实学校密码。预览不会发送真实打印任务，不得作为生产 WSGI 应用启动。

后端检查：`python -m pytest tests/test_campus_print.py -q`。浏览器验收使用 `browser-checks.js`，从仓库根目录运行 Playwright CLI，将 `tests/fixtures/print-portal.pdf` 分别复制为 `.codex/slow.pdf` 和 `.codex/fast.pdf` 后，在已打开预览页面的会话中执行 `run-code --filename=deploy/print-portal/browser-checks.js`。真实设备的沙箱、隧道、证书、两个学校账号及实体打印仍需上线前验收。
