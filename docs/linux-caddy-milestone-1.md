# Linux 上用固定 HTTPS 地址联调微信客服转人工

在 **Ubuntu 24.04 LTS** 或 **Alibaba Cloud Linux 3.2104（OpenAnolis Edition）** 云服务器上运行 FastAPI 和 SQLite，由 Caddy 在固定子域名上接收企微 HTTPS 回调。每节标注了发行版专用命令，其余步骤通用。验收时，普通微信用户发出「测试转人工」并收到坐席回复，且服务器上的 SQLite 记录为 `accepted`。

下文用 `kf-test.example.com` 和 `ubuntu@203.0.113.10` 举例。执行前换成企微可接受的实际子域名、云服务器 SSH 用户和公网 IP。若阿里云实例以 `root` 登录，可使用 `root@你的服务器公网IP`。Linux 下普通的 `sudo` 可以省略，切换运行用户的 `sudo -u` 命令照常执行。需要服务器管理权限、域名 DNS 管理权限、企微后台管理员、一名接待坐席和一个用于发送消息的普通微信账号。

## 1. 准备域名和云服务器

1. 在 DNS 控制台添加测试子域名的 `A` 记录，指向服务器的固定公网 IPv4 地址。只有服务器也配置并开放了 IPv6 时才添加 `AAAA`。本指南由 Caddy 直接接收流量。如果 DNS 服务提供反向代理，为该测试记录选择仅 DNS 解析。
2. 在云安全组或防火墙允许 TCP `80` 和 `443` 入站。SSH 使用现有管理规则。FastAPI 的后端端口默认是 `8000`，只供本机使用。
3. 在 Windows PowerShell 检查解析结果，确认 IP 正确。首次 SSH 登录时按云控制台提供的信息核对主机密钥指纹。

阿里云 ECS 在实例关联的安全组中添加 TCP `80` 和 `443` 入方向规则。在中国内地地域开通公网 Web 服务前，先核对域名的 ICP 备案及阿里云接入状态。企微后台的域名验证仍按第 5 节操作。 [阿里云 ECS 安全组](https://help.aliyun.com/zh/ecs/user-guide/start-using-security-groups) · [备案与域名访问](https://help.aliyun.com/zh/dns/pubz-filing-domain-name-related-faq)

```powershell
$WeComDomain = 'kf-test.example.com'
$WeComSsh = 'ubuntu@203.0.113.10'
Resolve-DnsName -Name $WeComDomain -Type A
```

## 2. 从 Windows 上传当前代码

在刚才的 PowerShell 窗口切到项目根目录。若另开窗口，先重新设置 `$WeComSsh`。下面的归档只含运行需要的源码、依赖清单和配置模板，适用于里程碑代码还未推送到 GitHub 的情况。`.env`、SQLite 和已下载的工具都留在本机。

```powershell
$WeComArchive = Join-Path ([IO.Path]::GetTempPath()) "wecom-support-agent-$([guid]::NewGuid()).tar.gz"
tar.exe -czf $WeComArchive --exclude=__pycache__ --exclude='*.pyc' .env.example requirements.txt support_agent
if ($LASTEXITCODE -ne 0) { throw '创建部署包失败' }
tar.exe -tzf $WeComArchive
scp.exe $WeComArchive "${WeComSsh}:wecom-support-agent.tar.gz"
if ($LASTEXITCODE -ne 0) { throw '上传部署包失败' }
ssh.exe $WeComSsh
```

以下命令除注明 PowerShell 外，均在服务器的 SSH 会话运行。先检查系统、端口和主机防火墙。如果 `80` 或 `443` 已有服务占用，复用管理该域名的 HTTPS 网关或改用专用测试服务器。

```bash
cat /etc/os-release
uname -m
sudo ss -ltnp '( sport = :80 or sport = :443 )'
```

### 检查 FastAPI 后端端口

部署前检查服务器本机是否已有进程监听 `8000`。以下命令同时检查备用端口 `8001`，`-H` 隐藏表头。

```bash
sudo ss -H -ltnp '( sport = :8000 or sport = :8001 )'
```

- **没有 `:8000` 的记录**。按后续默认配置部署。
- **`:8000` 已被其他服务占用，`:8001` 没有记录**。保留现有服务，让本项目用 `8001`。两个端口都有记录时，选另一个空闲的 TCP 端口，并用 `sudo ss -H -ltnp 'sport = :你的端口'` 检查。
- **`:8000` 由已部署的本项目监听**。先检查 `curl -fsS http://127.0.0.1:8000/healthz`。服务正常时可以沿用该端口。

如果选择 `8001`，同步调整以下三处。FastAPI 始终绑定 `127.0.0.1`，ECS 公网继续使用 Caddy 的 `80` 和 `443`，企微回调仍使用同一个 HTTPS URL。

| 位置 | 使用 `8001` 时 |
|---|---|
| 第 3 节 systemd 的 `ExecStart` | 使用 `--host 127.0.0.1 --port 8001` |
| 第 4 节 Caddyfile | 使用 `reverse_proxy 127.0.0.1:8001` |
| 本机健康检查和第 7 节排查 | 使用 `curl -fsS http://127.0.0.1:8001/healthz` |

### Ubuntu 24.04 LTS 主机防火墙

```bash
sudo ufw status
```

仅在 UFW 已启用且尚无相应规则时添加以下两条规则。

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

### Alibaba Cloud Linux 3 主机防火墙

检查 `firewalld` 的运行状态。若未运行，沿用实例已有的防火墙管理方式，并在 ECS 安全组核对放行规则。

```bash
systemctl is-active firewalld
```

若输出 `active`，先列出活跃 zone。把下方的 `public` 换成承载公网入站流量的网卡所属 zone，再分别添加运行时规则和永久规则。 [firewalld 命令参考](https://firewalld.org/documentation/man-pages/firewall-cmd.html)

```bash
sudo firewall-cmd --get-active-zones
```

```bash
WECOM_FIREWALL_ZONE=public
for wecom_service in http https; do
    sudo firewall-cmd --zone="$WECOM_FIREWALL_ZONE" --add-service="$wecom_service"
    sudo firewall-cmd --permanent --zone="$WECOM_FIREWALL_ZONE" --add-service="$wecom_service"
done
sudo firewall-cmd --zone="$WECOM_FIREWALL_ZONE" --list-services
```

## 3. 安装并启动 FastAPI

按发行版安装 Python。

**Ubuntu 24.04 LTS**

```bash
sudo apt-get update
sudo apt-get install -y python3-venv curl
```

**Alibaba Cloud Linux 3**

使用阿里云官方 Yum 源的 Python 3.11。项目用它创建独立虚拟环境，系统命令继续使用发行版默认的 Python。 [阿里云 Python 3.11 安装指南](https://help.aliyun.com/zh/alinux/support/install-a-newer-python-3-on-alibaba-cloud-linux-3)

```bash
sudo yum install -y python3.11 curl tar ca-certificates
python3.11 --version
```

**通用步骤**

创建专用运行用户，将代码装到 `/srv/wecom-support-agent`，把 SQLite 目录交给运行用户。已有 `.env` 会保留。第一次部署时先用空配置启动健康检查。

```bash
if ! id -u wecom-support >/dev/null 2>&1; then
    sudo useradd --system --user-group --no-create-home \
        --home-dir /srv/wecom-support-agent --shell /usr/sbin/nologin wecom-support
fi
sudo install -d -m 0755 /srv/wecom-support-agent
sudo tar --extract --gzip --no-same-owner --no-same-permissions \
    --file "$HOME/wecom-support-agent.tar.gz" --directory /srv/wecom-support-agent
cd /srv/wecom-support-agent
sudo install -d -m 0750 -o wecom-support -g wecom-support data
if ! sudo test -e .env; then
    sudo install -m 0640 -o root -g wecom-support .env.example .env
fi
```

### 创建虚拟环境并安装依赖

按发行版任选一组命令。已解包项目时，可以在新的 SSH 会话从这里继续。创建虚拟环境成功后才会安装依赖。

**Ubuntu 24.04 LTS**

```bash
cd /srv/wecom-support-agent &&
    sudo python3 -m venv .venv &&
    sudo .venv/bin/python -m pip install -r requirements.txt
```

**Alibaba Cloud Linux 3**

```bash
cd /srv/wecom-support-agent &&
    sudo python3.11 -m venv .venv &&
    sudo .venv/bin/python -m pip install -r requirements.txt
```

### 启动 FastAPI 服务

创建 systemd 服务。应用的一个进程同时处理回调和后台转接任务。保持单进程，避免多个进程同时消费这个 SQLite 队列。如果第 2 节选了其他端口，先修改下方 `ExecStart` 中的 `--port 8000`。如果服务文件已经写入，用 `sudoedit /etc/systemd/system/wecom-support-agent.service` 修改。

```bash
sudo tee /etc/systemd/system/wecom-support-agent.service >/dev/null <<'SYSTEMD'
[Unit]
Description=WeCom Support Agent
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=wecom-support
Group=wecom-support
WorkingDirectory=/srv/wecom-support-agent
ExecStart=/srv/wecom-support-agent/.venv/bin/python -m uvicorn support_agent.app:app --env-file /srv/wecom-support-agent/.env --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
Restart=on-failure
RestartSec=3s
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=multi-user.target
SYSTEMD
```

首次启动时执行以下命令。如果服务已经运行，且刚修改了服务文件，执行 `sudo systemctl daemon-reload && sudo systemctl restart wecom-support-agent` 使新端口生效。在服务器本机用实际端口请求 `/healthz`。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wecom-support-agent
sudo systemctl status wecom-support-agent --no-pager
curl -fsS http://127.0.0.1:8000/healthz
```

健康检查应返回 `{"status":"ok"}`。应用启动失败时查看日志。

```bash
sudo journalctl -u wecom-support-agent -n 50 --no-pager
```

## 4. 安装 Caddy，启用公网 HTTPS

已经装好 Caddy 且由 `caddy.service` 管理时，可以跳过安装，直接配置测试域名。

### Ubuntu 24.04 LTS 安装 Caddy

新服务器使用 Caddy 的稳定版软件源。 [Caddy 在 Ubuntu 上的安装说明](https://caddyserver.com/docs/install#debian-ubuntu-raspbian)

```bash
sudo apt-get install -y gnupg debian-keyring debian-archive-keyring apt-transport-https
CaddyAptTemp=$(mktemp -d)
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key -o "$CaddyAptTemp/signing.key"
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o "$CaddyAptTemp/caddy.list"
gpg --dearmor --output "$CaddyAptTemp/caddy.gpg" "$CaddyAptTemp/signing.key"
sudo install -m 0644 "$CaddyAptTemp/caddy.gpg" /usr/share/keyrings/caddy-stable-archive-keyring.gpg
sudo install -m 0644 "$CaddyAptTemp/caddy.list" /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update
sudo apt-get install -y caddy
caddy version
```

### Alibaba Cloud Linux 3 安装 Caddy

在新装实例上使用 Caddy 官方 Linux 二进制。命令按服务器架构选包，并在安装前校验 SHA-256。以后升级时，从官方发布页核对版本和对应架构的校验值。 [Caddy 静态二进制安装](https://caddyserver.com/docs/install#static-binaries) · [Caddy v2.11.9 发布文件及 SHA-256](https://github.com/caddyserver/caddy/releases/expanded_assets/v2.11.9)

若 ECS 下载失败，可以在 Windows 从官方发布页下载对应的 Linux 压缩包，再用 `scp.exe` 按原文件名上传到 SSH 用户的主目录。下面的安装命令会优先使用该文件，执行同样的校验。

```bash
(
    set -eu
    case "$(uname -m)" in
        x86_64)
            caddy_arch=amd64
            caddy_sha256=5b8cb7faf8726a2028ad96a66e54e6e81e8f361bc6175e5d22bbd997cc5177fb
            ;;
        aarch64)
            caddy_arch=arm64
            caddy_sha256=5e377cff994fc9362a6fcf695c53f226cfca3a3ef85bd3a3687cf252d6310165
            ;;
        *)
            echo '请从 Caddy 官方发布页面选择与服务器匹配的架构' >&2
            exit 1
            ;;
    esac
    caddy_version=2.11.9
    caddy_tmp=$(mktemp -d)
    archive="caddy_${caddy_version}_linux_${caddy_arch}.tar.gz"
    cd "$caddy_tmp"
    if [ -f "$HOME/$archive" ]; then
        cp -- "$HOME/$archive" "$archive"
    else
        curl -fSL --retry 3 --connect-timeout 10 --max-time 180 -o "$archive" \
            "https://github.com/caddyserver/caddy/releases/download/v${caddy_version}/${archive}"
    fi
    printf '%s  %s\n' "$caddy_sha256" "$archive" | sha256sum --check
    tar -xzf "$archive" caddy
    sudo install -m 0755 caddy /usr/bin/caddy
    /usr/bin/caddy version
)
```

首次安装时，创建专用的 Caddy 运行账号、证书存储目录和 systemd 服务。完成后继续配置域名。 [Caddy Linux 服务和 SELinux 配置](https://caddyserver.com/docs/running#linux-service)

```bash
if ! id -u caddy >/dev/null 2>&1; then
    sudo useradd --system --user-group --no-create-home \
        --home-dir /var/lib/caddy --shell /usr/sbin/nologin caddy
fi
sudo install -d -m 0755 /etc/caddy
sudo install -d -m 0750 -o caddy -g caddy /var/lib/caddy
sudo tee /etc/systemd/system/caddy.service >/dev/null <<'SYSTEMD'
[Unit]
Description=Caddy web server
Wants=network-online.target
After=network-online.target

[Service]
Type=notify
User=caddy
Group=caddy
Environment=HOME=/var/lib/caddy
ExecStart=/usr/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile --force
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
SYSTEMD
if command -v restorecon >/dev/null 2>&1; then
    sudo restorecon -v /usr/bin/caddy /etc/systemd/system/caddy.service
fi
sudo systemctl daemon-reload
```

### 配置测试域名并验证 HTTPS

运行 `sudoedit /etc/caddy/Caddyfile`，在文件末尾添加以下站点块并换成真实域名。如果已有同名站点，在原站点内合并路径规则。把 `reverse_proxy` 的端口设为第 3 节本机健康检查通过的端口。Caddy 会保留回调请求的路径和查询参数。配置里不加访问日志，避免把回调查询参数写进访问日志。 [Caddy 反向代理](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)

```caddyfile
kf-test.example.com {
    @wecom path /healthz /webhooks/wecom/kf
    handle @wecom {
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        respond 404
    }
}
```

以服务用户校验 Caddy 配置，确认它可以读取配置文件。校验成功后启动或重载服务。域名指向服务器且端口开放时，Caddy 自动申请和续期公网证书。 [Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https) · [Caddy 服务管理](https://caddyserver.com/docs/running#linux-service)

```bash
if sudo -u caddy -H caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
    if systemctl is-active --quiet caddy; then
        sudo systemctl reload caddy
    else
        sudo systemctl enable --now caddy
    fi
fi
sudo journalctl -u caddy -n 50 --no-pager
```

回到服务器外的 **Windows PowerShell**。已关闭原窗口时，先重新设置 `$WeComDomain`。保持证书校验开启。

```powershell
curl.exe -fsS "https://$WeComDomain/healthz"
$WeComCallback = "https://$WeComDomain/webhooks/wecom/kf"
$WeComCallback
```

公网应返回 `{"status":"ok"}`。如果这一步失败，先排查 DNS、端口或证书，再配置企微。

## 5. 配置企微测试客服和保存回调

1. 管理员在企业微信管理后台打开 **应用管理 → 微信客服**，创建独立测试客服账号，添加负责接起的接待人员，保存这个客服账号的接入链接或二维码。
2. 在微信客服的 **开启 API** 或 **API 配置** 页面，开启 **会话消息管理**，并确认测试账号已配置为可由 API 管理。已有生产回调的企业应由管理员确认测试接入的配置范围。将上一步的固定 URL 填入回调表单，在后台生成 **Token** 和 **EncodingAESKey**，先保留这组值。企业 ID 从「我的企业」的企业信息取得。 [微信客服开启 API](https://s.apifox.cn/apidoc/docs-site/406014/doc-417793)
3. 在服务器运行 `sudoedit /srv/wecom-support-agent/.env`，按下表填写。凭据只存放在服务器配置文件中。

| 配置项 | 填写内容 |
|---|---|
| `WECOM_CORP_ID` | 企业 ID |
| `WECOM_KF_SECRET` | 后台的微信客服 Secret。暂未显示时填 `bootstrap-only` |
| `WECOM_CALLBACK_TOKEN` | 回调表单中的 Token |
| `WECOM_ENCODING_AES_KEY` | 回调表单中的 43 字符 EncodingAESKey |
| `WECOM_OPEN_KFID` | 本次测试客服账号的 API ID。暂时未知时填 `bootstrap-only` |
| `WECOM_DB_PATH` | 正式联调用 `data/wecom.sqlite3`。用到任何占位值时填 `data/wecom-bootstrap.sqlite3` |

如果需要先保存回调才能看到 Secret，可用占位配置完成 URL 验证。企业 ID、Token 和 EncodingAESKey 必须是真实值。更新配置后重启应用。

```bash
sudo systemctl restart wecom-support-agent
sudo systemctl status wecom-support-agent --no-pager
```

在 Windows 测回调路由，再回到后台点 **保存**。另开窗口时，先按第 4 节重新设置 `$WeComCallback`。

```powershell
curl.exe -sS -o NUL -w 'HTTP %{http_code}\n' $WeComCallback
```

手动 GET 缺少企微签名参数，预期返回 `HTTP 422`，说明请求已到达应用的回调路由。点保存时，企微发送带签名和加密 `echostr` 的 GET，应用验签和解密后返还明文。后台显示保存成功才代表企微验证通过。 [企微回调 URL 验证](https://wdk-docs.github.io/wework-docs/server/dev-guide/callback-setting/)

### 换成正式 Secret 和客服账号 API ID

如果使用了占位值，保存后点击 **查看 Secret**，按后台提示获取微信客服的 Secret 并填回 `.env`。后台若要求 **企业可信 IP**，以服务器访问 `qyapi.weixin.qq.com` 时的公网出口 IP 为准。使用 NAT 时从云控制台确认实际出口地址。查询接口返回 `60020` 时优先核对这项设置。

如果已有正确的 `open_kfid`，直接填入。若还未知，先确保 `.env` 已填真实的企业 ID 和微信客服 Secret，再在服务器执行如下查询。它只显示账号名、`open_kfid` 或错误码。 [获取客服账号列表](https://developer.work.weixin.qq.com/document/path/94661)

```bash
cd /srv/wecom-support-agent
sudo -u wecom-support .venv/bin/python - <<'PYTHON'
import httpx
from dotenv import dotenv_values

env = dotenv_values('.env')
try:
    with httpx.Client(base_url='https://qyapi.weixin.qq.com', timeout=10) as client:
        token = client.get('/cgi-bin/gettoken', params={
            'corpid': env['WECOM_CORP_ID'], 'corpsecret': env['WECOM_KF_SECRET'],
        }).raise_for_status().json()
        if token.get('errcode') != 0 or not token.get('access_token'):
            raise SystemExit(f"gettoken errcode={token.get('errcode')}")
        offset = 0
        while True:
            page = client.post('/cgi-bin/kf/account/list',
                               params={'access_token': token['access_token']},
                               json={'offset': offset, 'limit': 100}).raise_for_status().json()
            if page.get('errcode') != 0:
                raise SystemExit(f"kf/account/list errcode={page.get('errcode')}")
            accounts = page.get('account_list', [])
            for account in accounts:
                print(account['name'], account['open_kfid'])
            if len(accounts) < 100:
                break
            offset += len(accounts)
except (httpx.HTTPError, ValueError) as error:
    raise SystemExit(f"WeCom request failed ({type(error).__name__})") from None
PYTHON
```

将测试客服对应的 ID 填入 `WECOM_OPEN_KFID`，并将 `WECOM_DB_PATH` 改为 `data/wecom.sqlite3`。已有其他账号的数据库请为这个测试账号使用单独文件。重启服务并打开日志观察。回调地址沿用刚才保存的固定 URL。

```bash
sudoedit /srv/wecom-support-agent/.env
sudo systemctl restart wecom-support-agent
sudo journalctl -u wecom-support-agent -f
```

## 6. 真实消息验收

1. **坐席**在企业微信客户端打开 **工作台 → 微信客服**，确认已加入同一个测试账号并处于接待状态。
2. **测试客户**用普通微信打开测试客服的接入链接或二维码，开启一条尚未被人工接管的会话，发送 `测试转人工`。
3. **服务器**应出现 `WeCom page persisted`。看到 `WeCom handoff result: queued`，表示会话已进入接待池。**坐席**在「待接入」选择该会话，接起并回复「人工已接起」。**测试客户**在原微信会话里确认收到回复。
4. 在另一个 SSH 窗口查询服务器的 SQLite。若配置使用了其他数据库文件，换成实际路径。可根据时间和 `message_id` 匹配本次消息。

   ```bash
   cd /srv/wecom-support-agent
   sudo -u wecom-support .venv/bin/python - <<'PYTHON'
   import sqlite3

   with sqlite3.connect('file:data/wecom.sqlite3?mode=ro', uri=True) as db:
       for row in db.execute('''
           SELECT datetime(send_time, 'unixepoch') AS sent_utc, message_id,
                  state, servicer_id IS NOT NULL AS has_servicer, attempts, last_error
           FROM messages ORDER BY rowid DESC LIMIT 5
       '''):
           print(row)
   PYTHON
   ```

**验收通过需要两项证据**。普通微信在同一会话收到人工回复，且本次记录为 `accepted`、`has_servicer` 为 `1`。`queued` 表示等待接起。在直播画面展示状态时，隐藏客户和消息标识。 [微信客服读取消息与接起事件](https://developer.work.weixin.qq.com/document/path/94670)

## 7. 排查和日常更新

| 现象 | 检查项 |
|---|---|
| 本机 `/healthz` 不通 | 看 `sudo journalctl -u wecom-support-agent -n 50 --no-pager`，检查依赖、`.env` 和 SQLite 目录权限。 |
| 公网 HTTPS 无法访问或证书有误 | 检查域名的 `A` 记录和已有 `AAAA` 记录，确认云安全组及主机防火墙允许 `80`、`443`，看 `sudo journalctl -u caddy -n 50 --no-pager`。 |
| Uvicorn 日志提示 `Address already in use` | 用第 2 节的 `ss` 命令确认占用进程，再按端口对照表选择空闲端口。 |
| 公网返回 `502` | 检查 Caddy 的上游端口是否与 Uvicorn 一致，并在服务器上用实际端口请求本机 `/healthz`。 |
| Alibaba Cloud Linux 3 上的 Yum 找不到 Python 3.11 | 运行 `yum repolist --enabled` 和 `yum --showduplicates list python3.11`，检查阿里云官方源是否已启用。 |
| Alibaba Cloud Linux 3 上 Caddy 日志出现 `Permission denied` | 检查 `caddy` 用户能否读取配置、证书目录是否归它所有。若 `getenforce` 显示 `Enforcing`，按 Caddy 官方指南检查程序和服务文件的 SELinux 标签。 |
| 公网 `/healthz` 正常，但回调返回 `404` | 核对路径 `/webhooks/wecom/kf` 和 Caddy 的匹配规则。看应用是否已读取完整 `.env` 并重启。 |
| 企微后台无法保存 URL | 查看后台给出的连接、域名或验签错误。若后台要求上传域名验证文件，按下方配置。验签失败时核对企业 ID、Token、EncodingAESKey 是否为同一配置。 |
| 拉取报 `60020` 或账号不在列表 | 分别核对服务器公网出口 IP 和微信客服 Secret 的 API 管理范围。确认已换成真实 `open_kfid` 并重启。 |
| 坐席看到会话，但本地一直是 `queued` | 让坐席手动接起，确认普通微信收到回复，再检查接起事件是否回调并拉取到同一个账号。 |

### 后台要求上传域名验证文件时

如果企微要求通过站点根路径下载验证文件，只为后台指定的文件名增加一个精确匹配。把以下示例里的 `WW_verify_EXAMPLE.txt` 全部换成后台下载的真实文件名，在 Windows 执行上传命令。

```powershell
scp.exe '.\WW_verify_EXAMPLE.txt' "${WeComSsh}:WW_verify_EXAMPLE.txt"
```

在服务器安装验证文件。

```bash
sudo install -d -m 0755 /var/www/wecom-verify
sudo install -m 0644 "$HOME/WW_verify_EXAMPLE.txt" /var/www/wecom-verify/WW_verify_EXAMPLE.txt
```

把以下配置加到对应域名的 Caddy 站点块内，与 `handle @wecom` 同级。按第 4 节校验重载后，从 Windows 通过 `curl.exe -fsS "https://$WeComDomain/WW_verify_EXAMPLE.txt"` 确认原文件可访问，再在后台完成验证。

```caddyfile
handle /WW_verify_EXAMPLE.txt {
    root * /var/www/wecom-verify
    file_server
}
```

### 更新代码

再次按第 2 节打包和上传，然后在服务器解包并安装依赖。包内只包含代码、依赖清单和 `.env.example`。如果部署时用了其他端口，将下面健康检查的 `8000` 替换为实际端口。

```bash
sudo tar --extract --gzip --no-same-owner --no-same-permissions \
    --file "$HOME/wecom-support-agent.tar.gz" --directory /srv/wecom-support-agent
cd /srv/wecom-support-agent
sudo .venv/bin/python -m pip install -r requirements.txt
sudo systemctl restart wecom-support-agent
curl -fsS http://127.0.0.1:8000/healthz
```

回到 Windows 再验证公网 `/healthz`，用测试客服账号重跑真实消息验收。域名不变时继续使用已保存的企微回调。
