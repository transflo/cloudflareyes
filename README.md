# Cloudflare 优选 IP → DNSPod 自动更新器

自动抓取 Cloudflare 优选 IP 页面，把「电信 / 联通 / 移动」每条线路排名前 3 的 IPv4 自动同步到腾讯云 DNSPod 的 A 记录。定时运行、全自动，IP 变化无需人工处理。

- 每 10 分钟自动抓取一次优选 IP（可用 `INTERVAL_MINUTES` 调整）
- 自动 **新增 / 修改 / 删除** DNS 记录，保持每条线路只有目标 IP
- 页面数据不足时不填充虚假地址，多余旧记录会被清理
- 支持 `DRY_RUN` 预览变更、`RUN_ONCE` 单次执行
- 容器内以非 root 用户运行，`.env` 密钥不会进入镜像

## 快速开始（Docker Compose）

### 1. 准备

- 安装 Docker 与 Docker Compose（Compose v2），检查：`docker compose version`
- 在 [腾讯云控制台](https://console.cloud.tencent.com/cam/capi) 创建 API 密钥（SecretId / SecretKey），并确认账号拥有目标域名的 DNSPod 解析管理权限

### 2. 获取项目文件

```bash
git clone https://github.com/transflo/cloudflareyes.git
cd cloudflareyes
```

### 3. 创建并填写配置

```bash
cp .env.example .env
```

编辑 `.env`，至少填写：

```dotenv
TENCENT_SECRET_ID=你的SecretId
TENCENT_SECRET_KEY=你的SecretKey
DOMAIN=example.com
SUBDOMAIN=@
```

- `SUBDOMAIN`：`@` 表示根域名 `example.com`；要更新 `abc.example.com` 就填 `abc`，不要写完整域名
- `LINES`：要维护的线路，默认 `电信,联通,移动`，必须与 DNSPod 线路名完全一致

### 4. 启动

**方式 A（推荐）：使用 Docker Hub 镜像**

镜像已发布到 Docker Hub（`transflo/cloudflareyes`），无需本地构建：

```bash
docker compose pull cf-dns-updater
docker compose up -d
```

**方式 B：本地构建镜像**

```bash
docker compose up -d --build
```

两种方式都会启动两个容器：

| 容器 | 作用 | 端口 |
|---|---|---|
| `flaresolverr` | 绕过目标页面的人机校验 | 仅本机 `127.0.0.1:8191` |
| `cf-dns-updater` | 抓取、解析并同步 DNSPod | 无 |

### 5. 查看与验证

```bash
docker compose ps                  # 两个容器都应为 Up
docker compose logs -f cf-dns-updater
```

**首次使用建议先预览再生效**：在 `.env` 中临时设置

```dotenv
RUN_ONCE=true
DRY_RUN=true
```

然后重启并查看日志：

```bash
docker compose up -d
docker compose logs cf-dns-updater
```

日志会打印 `DRY-RUN: 将创建/修改/删除…` 而不会真正改动 DNS。确认无误后改回：

```dotenv
RUN_ONCE=false
DRY_RUN=false
```

再次执行 `docker compose up -d` 即可进入定时循环。

### 6. 日常运维

| 操作 | 命令 |
|---|---|
| 查看日志 | `docker compose logs -f cf-dns-updater` |
| 升级到最新镜像 | `docker compose pull && docker compose up -d` |
| 手动跑一次 | 在 `.env` 设 `RUN_ONCE=true`，`docker compose up -d`，完成后改回 |
| 停止并删除容器 | `docker compose down` |
| 连同网络一起删除 | `docker compose down -v` |

## 中国大陆网络加速（拉取 / 构建太慢时）

默认镜像来自 Docker Hub / ghcr.io，在国内拉取可能很慢。提供以下三种方式：

### 方式一：使用内置加速覆盖文件（推荐）

仓库提供 `docker-compose.china.yml`，会把 FlareSolverr 换成 ghcr 国内代理、更新器换成 Docker Hub 加速地址：

```bash
# 镜像模式（拉取加速）
docker compose -f docker-compose.yml -f docker-compose.china.yml pull
docker compose -f docker-compose.yml -f docker-compose.china.yml up -d

# 本地构建（pip 也走国内源）
docker compose -f docker-compose.yml -f docker-compose.china.yml build \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
docker compose -f docker-compose.yml -f docker-compose.china.yml up -d
```

当前覆盖内容（以下地址均为实测可用，但公共镜像站可能随时变动）：

| 镜像 | 默认地址 | 加速地址 |
|---|---|---|
| FlareSolverr | `ghcr.io/flaresolverr/flaresolverr` | `ghcr.nju.edu.cn/flaresolverr/flaresolverr`（南京大学镜像站） |
| 更新器 | `transflo/cloudflareyes` | `docker.m.daocloud.io/transflo/cloudflareyes`（DaoCloud 代理，镜像发布后可用） |

### 方式二：配置 Docker 镜像加速器（一劳永逸）

在 Docker 守护进程配置 `registry-mirrors`：

- 普通 Docker（需要 root）：编辑 `/etc/docker/daemon.json` 后重启
- rootless Docker：编辑 `~/.config/docker/daemon.json` 后执行 `systemctl --user restart docker`

```json
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://docker.1ms.run"
  ]
}
```

注意：公共加速器随时可能失效；腾讯云 / 阿里云服务器建议优先使用云厂商提供的私有加速地址。

### 方式三：构建时使用国内 pip 源

Dockerfile 支持 `PIP_INDEX_URL` 构建参数：

```bash
docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t transflo/cloudflareyes:latest .
```

## 不使用仓库文件？手动编写 compose 也可以

如果不想 clone 整个仓库，把下面内容保存为 `docker-compose.yml`，再按第 3 步创建 `.env`：

```yaml
services:
  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    container_name: flaresolverr
    restart: unless-stopped
    ports:
      - "127.0.0.1:8191:8191"

  cf-dns-updater:
    image: transflo/cloudflareyes:latest
    container_name: cf-dns-updater
    restart: unless-stopped
    depends_on:
      - flaresolverr
    env_file:
      - .env
    environment:
      - FLARESOLVERR_URL=http://flaresolverr:8191
      - TARGET_URL=https://api.uouin.com/cloudflare.html
```

然后启动：

```bash
docker compose up -d
```

## 配置项

| 变量 | 默认值 | 必填 | 说明 |
| --- | --- | --- | --- |
| `TENCENT_SECRET_ID` | 无 | 是 | 腾讯云 API SecretId |
| `TENCENT_SECRET_KEY` | 无 | 是 | 腾讯云 API SecretKey |
| `DOMAIN` | 无 | 是 | DNSPod 中的主域名，例如 `example.com` |
| `SUBDOMAIN` | `@` | 否 | DNSPod 主机记录；`@` 表示 `example.com`，填 `abc` 表示 `abc.example.com` |
| `LINES` | `电信,联通,移动` | 否 | 要同步的线路，逗号分隔，必须与 DNSPod 线路名一致 |
| `MAX_IPS_PER_LINE` | `3` | 否 | 每条线路最多维护的 IP 数量 |
| `INTERVAL_MINUTES` | `10` | 否 | 循环运行时的同步间隔（分钟） |
| `TTL` | `600` | 否 | 新建/修改记录时的 TTL，范围 1–604800 秒 |
| `DRY_RUN` | `false` | 否 | 只打印将要执行的变更，不创建/修改/删除 |
| `RUN_ONCE` | `false` | 否 | 只执行一轮后退出 |
| `LOG_LEVEL` | `INFO` | 否 | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `HTTP_TIMEOUT` | `90` | 否 | HTTP 请求超时（秒） |
| `FLARE_MAX_TIMEOUT` | `60000` | 否 | FlareSolverr 单次浏览器请求最大超时（毫秒） |
| `FLARESOLVERR_URL` | `http://flaresolverr:8191` | 否 | FlareSolverr 服务地址 |
| `DNSPOD_ENDPOINT` | `https://dnspod.tencentcloudapi.com` | 否 | DNSPod API 地址，生产环境不要修改 |
| `DNSPOD_ALLOW_HTTP` | `false` | 否 | 仅本地集成测试使用；`true` 才允许 `DNSPOD_ENDPOINT` 使用 http |
| `TARGET_URL` | `https://api.uouin.com/cloudflare.html` | 否 | 优选 IP 页面地址 |
| `DOCKER_IMAGE` | `transflo/cloudflareyes:latest` | 否 | compose 使用的镜像名（仅使用镜像部署时需要关注） |

## DNSPod 线路说明

- DNSPod 的分线路记录通常要求同一主机记录先存在一条「默认」线路记录。如果创建线路记录时收到 `MustAddDefaultLineFirst`，请先在 DNSPod 控制台为相同的 `DOMAIN/SUBDOMAIN` 创建默认线路记录，再重新运行。
- 程序只处理精确匹配 `DOMAIN/SUBDOMAIN/线路` 的 A 记录，并按 `RecordId` 排序作为 3 个 DNS 位置。
- 每轮同步后，该线路只保留目标列表中的记录；页面数据不足 3 个有效 IPv4 时不会填充虚假地址，但会删除多余的旧记录。

## 安全说明

- FlareSolverr 是无鉴权的浏览器代理服务，compose 已将其限制为只监听 `127.0.0.1`；请勿把 `8191` 端口发布到公网。
- `DNSPOD_ENDPOINT` 强制使用 HTTPS，避免腾讯云 API 签名头明文传输。
- 程序只接受公网 IPv4，自动过滤内网、回环、链路本地、保留/组播地址。
- 数据源（默认 `api.uouin.com`）是第三方页面；页面被篡改等同于攻击者可以改写你的解析记录，请确认来源可信。
- 不要把 `.env` 或真实密钥提交到 Git。

## 常见问题

**Q：日志提示 `MustAddDefaultLineFirst`？**
A：DNSPod 要求先存在「默认」线路记录。到 DNSPod 控制台为该域名/主机记录添加一条默认线路的 A 记录，再重新运行。

**Q：某条线路一直没有更新？**
A：检查 `LINES` 里的线路名是否与 DNSPod 线路名完全一致；页面没有该线路的数据时程序会跳过并在日志中提示。

**Q：日志显示“无需更新”正常吗？**
A：正常。说明页面排名 IP 与当前 DNS 记录一致，程序不会做无意义的修改。

**Q：如何只跑一次而不是循环？**
A：`.env` 设置 `RUN_ONCE=true` 后重启容器；执行完会自动退出。

**Q：更新器容器退出，FlareSolverr 还在？**
A：`RUN_ONCE=true` 执行完退出是预期行为。正式使用请保持 `RUN_ONCE=false`。

## 本地开发与测试（贡献者）

项目不依赖 pytest，使用标准库 `unittest`：

```bash
python -m py_compile app.py
python -m unittest discover -v
```

本地 Docker 集成测试使用内置 mock 服务（模拟 FlareSolverr 与 DNSPod），详见 `tests/mock_services.py` 与「Docker 集成测试」相关注释。

## 发布到 Docker Hub（维护者）

### 手动发布

```bash
docker build -t transflo/cloudflareyes:latest .
docker tag transflo/cloudflareyes:latest transflo/cloudflareyes:v1.0.0
docker login          # 推荐使用 Access Token
docker push transflo/cloudflareyes:latest
docker push transflo/cloudflareyes:v1.0.0
```

### 自动发布（GitHub Actions）

仓库内置 `.github/workflows/docker-publish.yml`：推送到 `main` 或 `v*` 标签时，自动构建 `linux/amd64`、`linux/arm64` 双架构并推送到 Docker Hub。需要先在 GitHub 仓库配置两个 Secrets：`DOCKERHUB_USERNAME`（`transflo`）与 `DOCKERHUB_TOKEN`（Docker Hub Access Token）。

## 项目文件

- `app.py`：主程序；
- `test_app.py`：单元测试；
- `Dockerfile`：应用镜像；
- `docker-compose.yml`：FlareSolverr 与更新器编排；
- `.env.example`：配置模板；
- `requirements.txt`：Python 依赖；
- `tests/mock_services.py`：本地集成测试用的 FlareSolverr/DNSPod mock 服务；
- `.github/workflows/docker-publish.yml`：自动发布到 Docker Hub 的 GitHub Actions 工作流。
