# Cloudflare 优选 IPv4 -> DNSPod 线路解析自动更新器

自动抓取 Cloudflare 优选 IPv4，并将每条线路排名前 3 的 IP 同步到腾讯云 DNSPod 的 A 记录。
适合部署在 Docker / Docker Compose 中定时运行。

## 功能

- 通过 [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) 获取目标页面；
- 自动解析页面中的线路和 IPv4，并保留页面排序；
- 默认取每个运营商排名前 3 的 IPv4；
- 默认维护「电信、联通、移动」三条线路；
- DNSPod 每条线路最多保留 3 条记录，不足时创建、变化时按位置更新、多余时删除；
- 支持 `DRY_RUN=true` 预览变更；
- 支持 `RUN_ONCE=true` 单次执行，适合手动运行或外部定时任务；
- 使用腾讯云 TC3-HMAC-SHA256 签名调用 DNSPod API；
- 对 API 错误、重复记录、配置错误和部分线路失败进行明确处理；
- 容器使用非 root 用户运行。

## 工作流程

```text
FlareSolverr
     |
     v
目标页面 -> 解析线路/IPv4 -> 选择每条线路前 3 个 IP
                                            |
                                            v
                         DNSPod Describe / Create / Modify / Delete
```

## 快速开始

### 1. 准备 DNSPod API 密钥

在腾讯云控制台创建 API 密钥，并确保账号拥有目标域名的 DNSPod 解析管理权限。

### 2. 创建配置文件

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

`SUBDOMAIN` 使用 DNSPod 的主机记录填写：`@` 表示根域名 `example.com`；如果要更新 `abc.example.com`，填写 `SUBDOMAIN=abc`，不要填写完整域名。

### 3. 启动

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f cf-dns-updater
```

程序会直接按当前 `.env` 配置持续运行。只有在需要单次执行或预览变更时，才按需设置 `RUN_ONCE=true` 或 `DRY_RUN=true`，无需额外切换部署流程。

### 4. 使用 Docker Hub 镜像（可选）

项目发布到 Docker Hub 后，也可以直接用现成镜像部署，无需本地构建：

```bash
# 在 .env 中指定镜像（或直接修改 docker-compose.yml 的 image: 行）
DOCKER_IMAGE=<DockerHub用户名>/cloudflareyes:latest

docker compose pull cf-dns-updater
docker compose up -d
```

如果确定只使用镜像、不在本地构建，也可以把 `docker-compose.yml` 中 `cf-dns-updater` 服务的 `build: .` 行删掉，只保留 `image:`。


## 配置项

| 变量 | 默认值 | 必填 | 说明 |
| --- | --- | --- | --- |
| `TENCENT_SECRET_ID` | 无 | 是 | 腾讯云 API SecretId |
| `TENCENT_SECRET_KEY` | 无 | 是 | 腾讯云 API SecretKey |
| `DOMAIN` | 无 | 是 | DNSPod 中的主域名，例如 `example.com` |
| `SUBDOMAIN` | `@` | 否 | DNSPod 主机记录；`@` 表示 `example.com`，填写 `abc` 表示 `abc.example.com`，不要填写完整域名 |
| `LINES` | `电信,联通,移动` | 否 | 要同步的线路，逗号分隔，必须匹配 DNSPod 线路名 |
| `MAX_IPS_PER_LINE` | `3` | 否 | 每条线路最多维护的 IP 数量；默认只同步页面排名前 3 个 |
| `INTERVAL_MINUTES` | `10` | 否 | 循环运行时的同步间隔，单位为分钟 |
| `TTL` | `600` | 否 | 新建或修改记录时使用的 TTL，范围为 1–604800 秒 |
| `DRY_RUN` | `false` | 否 | 只查询并打印变更，不执行创建、修改或删除 |
| `RUN_ONCE` | `false` | 否 | 只执行一轮后退出 |
| `LOG_LEVEL` | `INFO` | 否 | `DEBUG`、`INFO`、`WARNING` 或 `ERROR` |
| `HTTP_TIMEOUT` | `90` | 否 | HTTP 请求超时时间，单位为秒 |
| `FLARE_MAX_TIMEOUT` | `60000` | 否 | FlareSolverr 单次浏览器请求最大超时时间，单位为毫秒 |
| `FLARESOLVERR_URL` | `http://flaresolverr:8191` | 否 | FlareSolverr 服务地址 |
| `DNSPOD_ENDPOINT` | `https://dnspod.tencentcloudapi.com` | 否 | DNSPod API 地址；生产环境不要修改 |
| `DNSPOD_ALLOW_HTTP` | `false` | 否 | 仅本地集成测试使用；设为 `true` 才允许 `DNSPOD_ENDPOINT` 使用 http（生产环境请保持默认） |
| `TARGET_URL` | `https://api.uouin.com/cloudflare.html` | 否 | 优选 IP 页面地址 |
| `DOCKER_IMAGE` | `transflo/cloudflareyes:latest` | 否 | compose 使用的镜像名；从 Docker Hub 拉取时改为 `<你的用户名>/cloudflareyes:latest` |

## DNSPod 线路说明

DNSPod 的分线路记录通常要求同一主机记录先存在一条「默认」线路记录。
如果创建线路记录时收到 `MustAddDefaultLineFirst`，请先在 DNSPod 控制台为相同的 `DOMAIN/SUBDOMAIN` 创建默认线路记录，再重新运行。

程序只处理精确匹配 `DOMAIN/SUBDOMAIN/线路` 的 A 记录，并按 `RecordId` 排序作为 3 个 DNS 位置。每轮同步后，该线路只保留目标列表中的记录；页面数据不足 3 个有效 IPv4 时不会填充虚假地址，但会删除多余的旧记录。

## 安全说明

- `docker-compose.yml` 中 FlareSolverr 只绑定 `127.0.0.1`，不会向局域网/公网暴露这个无鉴权的浏览器代理服务；如使用其它部署方式，请勿把 `8191` 端口发布到公网。
- `DNSPOD_ENDPOINT` 强制使用 HTTPS，避免腾讯云 API 签名头在网络上明文传输；本地 mock 测试需显式设置 `DNSPOD_ALLOW_HTTP=true`。
- 程序只接受公网 IPv4（自动过滤内网、回环、链路本地、保留/组播地址），避免第三方页面把非公网地址写入 DNS。
- 数据源（默认 `api.uouin.com`）是第三方页面，页面被篡改即等同于攻击者可以改写你的解析记录，请确认来源可信并关注页面变化。

## 发布到 Docker Hub

### 手动发布

```bash
docker build -t <DockerHub用户名>/cloudflareyes:latest .
docker tag <DockerHub用户名>/cloudflareyes:latest <DockerHub用户名>/cloudflareyes:v1.0.0
docker login        # 推荐使用 Access Token，而不是账号密码
docker push <DockerHub用户名>/cloudflareyes:latest
docker push <DockerHub用户名>/cloudflareyes:v1.0.0
```

### 自动发布（GitHub Actions）

仓库已内置 `.github/workflows/docker-publish.yml`：

- 触发：推送到 `main`、推送 `v*` 标签，或手动运行 `workflow_dispatch`；
- 使用 Buildx 构建 `linux/amd64` 与 `linux/arm64` 双架构并推送到 Docker Hub；
- 镜像名 = `${{ secrets.DOCKERHUB_USERNAME }}/cloudflareyes`。

首次使用前需要：

1. 在 [Docker Hub](https://hub.docker.com) 创建仓库 `cloudflareyes`；
2. 在 Docker Hub 账号设置中生成 Access Token；
3. 在 GitHub 仓库 Settings → Secrets and variables → Actions 中添加：
   - `DOCKERHUB_USERNAME`：Docker Hub 用户名；
   - `DOCKERHUB_TOKEN`：上一步生成的 Access Token；
4. 推送 tag 触发发布：

```bash
git tag v1.0.0
git push origin v1.0.0
```

发布完成后即可按「使用 Docker Hub 镜像」一节部署。

## 本地测试

### Python 单元测试

项目不依赖 pytest，直接使用标准库 `unittest`：

```bash
python -m py_compile app.py
python -m unittest discover -v
```

### Docker 构建

```bash
docker build -t cloudflareyes-test .
```

### 无真实密钥的 Docker 集成测试

应用支持通过 `DNSPOD_ENDPOINT` 指向本地 mock 服务。生产环境不要修改该变量。

下面的测试会验证：

1. 容器可以正常启动；
2. FlareSolverr 返回的 HTML 能被解析；
3. DNSPod 的查询、修改、创建和删除流程可以执行；
4. `RUN_ONCE=true` 能在一轮执行后正常退出。

项目已内置同时模拟 FlareSolverr 和 DNSPod API 的测试服务。在项目根目录启动：

```bash
# mock 服务监听宿主机 8192 端口
python tests/mock_services.py
```

然后运行：

```bash
docker run --rm \
  --add-host=host.docker.internal:host-gateway \
  -e TENCENT_SECRET_ID=test-secret-id \
  -e TENCENT_SECRET_KEY=test-secret-key \
  -e DOMAIN=example.com \
  -e SUBDOMAIN=@ \
  -e LINES=电信,联通,移动 \
  -e RUN_ONCE=true \
  -e DRY_RUN=false \
  -e FLARESOLVERR_URL=http://host.docker.internal:8192 \
  -e DNSPOD_ENDPOINT=http://host.docker.internal:8192 \
  -e DNSPOD_ALLOW_HTTP=true \
  -e TARGET_URL=https://example.invalid/cloudflare.html \
  cloudflareyes-test
```

如果只想确认解析和变更计划，不需要 mock DNSPod 修改接口，可使用：

```bash
docker run --rm \
  --add-host=host.docker.internal:host-gateway \
  -e TENCENT_SECRET_ID=test-secret-id \
  -e TENCENT_SECRET_KEY=test-secret-key \
  -e DOMAIN=example.com \
  -e SUBDOMAIN=@ \
  -e LINES=电信,联通,移动 \
  -e RUN_ONCE=true \
  -e DRY_RUN=true \
  -e FLARESOLVERR_URL=http://host.docker.internal:8192 \
  -e DNSPOD_ENDPOINT=http://host.docker.internal:8192 \
  -e DNSPOD_ALLOW_HTTP=true \
  -e TARGET_URL=https://example.invalid/cloudflare.html \
  cloudflareyes-test
```

上面的命令仍需要先启动 `tests/mock_services.py`，因为应用必须先从 FlareSolverr 获取页面；`DRY_RUN=true` 只会阻止 DNSPod 的创建/修改请求。

### Compose 冒烟测试

正式部署使用 Compose：

~~~bash
docker compose up -d --build
docker compose logs -f cf-dns-updater
~~~

如需使用 Compose 做单次验证或预览，可按需在 `.env` 中设置 `RUN_ONCE=true` 或 `DRY_RUN=true`。

## 安全建议

- 不要把 `.env`、`TENCENT_SECRET_KEY` 或真实 API 响应提交到 Git；
- 使用权限尽可能小的腾讯云 API 子账号；
- 需要确认变更计划时可使用 `DRY_RUN=true`，它不会创建、修改或删除 DNS 记录；
- FlareSolverr 端口只应暴露在可信网络中。

## 项目文件

- `app.py`：主程序；
- `test_app.py`：单元测试；
- `Dockerfile`：应用镜像；
- `docker-compose.yml`：FlareSolverr 与更新器编排；
- `.env.example`：配置模板；
- `requirements.txt`：Python 依赖。
- `tests/mock_services.py`：本地 Docker 集成测试用的 FlareSolverr/DNSPod mock 服务。
- `.github/workflows/docker-publish.yml`：自动构建并推送到 Docker Hub 的 GitHub Actions 工作流。
