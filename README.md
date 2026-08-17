# Cloudflare 优选 IPv4 -> DNSPod 线路解析自动更新器

自动抓取 Cloudflare 优选 IPv4，并将每条线路的最优 IP 同步到腾讯云 DNSPod 的 A 记录。
适合部署在 Docker / Docker Compose 中定时运行。

## 功能

- 通过 [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) 获取目标页面；
- 自动解析页面中的线路和 IPv4，并保留页面排序；
- 默认维护「电信、联通、移动」三条线路；
- DNSPod 中不存在记录时自动创建，存在但 IP 变化时自动更新；
- 支持 `DRY_RUN=true` 预览变更；
- 支持 `RUN_ONCE=true` 单次执行，适合首次验证和定时任务；
- 使用腾讯云 TC3-HMAC-SHA256 签名调用 DNSPod API；
- 对 API 错误、重复记录、配置错误和部分线路失败进行明确处理；
- 容器使用非 root 用户运行。

## 工作流程

```text
FlareSolverr
     |
     v
目标页面 -> 解析线路/IPv4 -> 选择每条线路第一个 IP
                                      |
                                      v
                         DNSPod Describe / Create / Modify
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

建议第一次运行时使用：

```dotenv
RUN_ONCE=true
DRY_RUN=true
```

### 3. 启动

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f cf-dns-updater
```

确认预览结果正确后，将 `.env` 改为：

```dotenv
RUN_ONCE=false
DRY_RUN=false
```

然后重新创建更新器：

```bash
docker compose up -d --build cf-dns-updater
```

## 配置项

| 变量 | 默认值 | 必填 | 说明 |
| --- | --- | --- | --- |
| `TENCENT_SECRET_ID` | 无 | 是 | 腾讯云 API SecretId |
| `TENCENT_SECRET_KEY` | 无 | 是 | 腾讯云 API SecretKey |
| `DOMAIN` | 无 | 是 | DNSPod 中的主域名，例如 `example.com` |
| `SUBDOMAIN` | `@` | 否 | 主机记录，例如 `@`、`www` |
| `LINES` | `电信,联通,移动` | 否 | 要同步的线路，逗号分隔，必须匹配 DNSPod 线路名 |
| `INTERVAL_MINUTES` | `10` | 否 | 循环运行时的同步间隔，单位为分钟 |
| `TTL` | `600` | 否 | 新建或修改记录时使用的 TTL，范围为 1–604800 秒 |
| `DRY_RUN` | `false` | 否 | 只查询并打印变更，不执行创建/修改 |
| `RUN_ONCE` | `false` | 否 | 只执行一轮后退出 |
| `LOG_LEVEL` | `INFO` | 否 | `DEBUG`、`INFO`、`WARNING` 或 `ERROR` |
| `HTTP_TIMEOUT` | `90` | 否 | HTTP 请求超时时间，单位为秒 |
| `FLARE_MAX_TIMEOUT` | `60000` | 否 | FlareSolverr 单次浏览器请求最大超时时间，单位为毫秒 |
| `FLARESOLVERR_URL` | `http://flaresolverr:8191` | 否 | FlareSolverr 服务地址 |
| `DNSPOD_ENDPOINT` | `https://dnspod.tencentcloudapi.com` | 否 | DNSPod API 地址；生产环境不要修改 |
| `TARGET_URL` | `https://api.uouin.com/cloudflare.html` | 否 | 优选 IP 页面地址 |

## DNSPod 线路说明

DNSPod 的分线路记录通常要求同一主机记录先存在一条「默认」线路记录。
如果创建线路记录时收到 `MustAddDefaultLineFirst`，请先在 DNSPod 控制台为相同的 `DOMAIN/SUBDOMAIN` 创建默认线路记录，再重新运行。

程序会拒绝在无法精确匹配记录时直接修改查询结果中的第一条记录，以避免误改其它线路。

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
3. DNSPod 的查询、修改和创建流程可以执行；
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

首次验证建议在 `.env` 中设置 `RUN_ONCE=true` 和 `DRY_RUN=true`。确认日志和线路匹配后，再改为循环运行并关闭 DRY-RUN。

## 安全建议

- 不要把 `.env`、`TENCENT_SECRET_KEY` 或真实 API 响应提交到 Git；
- 使用权限尽可能小的腾讯云 API 子账号；
- 生产环境先使用 `DRY_RUN=true` 验证线路名称和目标域名；
- 建议为首次部署保留 `RUN_ONCE=true`，确认日志无误后再启用循环模式；
- FlareSolverr 端口只应暴露在可信网络中。

## 项目文件

- `app.py`：主程序；
- `test_app.py`：单元测试；
- `Dockerfile`：应用镜像；
- `docker-compose.yml`：FlareSolverr 与更新器编排；
- `.env.example`：配置模板；
- `requirements.txt`：Python 依赖。
- `tests/mock_services.py`：本地 Docker 集成测试用的 FlareSolverr/DNSPod mock 服务。
