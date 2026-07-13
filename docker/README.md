# Docker 部署

前后端一体镜像，单容器内同时运行三个 Web UI 与可选调度器。

| 端口 | 服务 |
|------|------|
| 8090 | 主站（招标 / Hermes / BIM） |
| 8091 | 商机洞察 |
| 8092 | 股票专题 |

## 镜像

默认镜像名：`reg.hdec.com/web_scraper:latest`

可通过环境变量覆盖：

```bash
export DOCKER_REGISTRY=reg.hdec.com
export DOCKER_IMAGE=web_scraper
export IMAGE_TAG=latest
```

## 构建与运行

```bash
# 构建
make docker-build

# 启动 app + mongo + influxdb（及其他 compose 服务）
make docker-up

# 推送（需已 docker login reg.hdec.com）
make docker-push IMAGE_TAG=v1.0.0
```

基础镜像默认 `reg.hdec.com/library/python:3.11-slim`；若 registry 路径不同，构建时指定：

```bash
make docker-build BASE_IMAGE=reg.hdec.com/<项目>/python:3.11-slim
```

或直接使用 compose（在项目根目录）：

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

## 环境变量

复制 `.env.example` 为 `.env` 后按需修改。Compose 中 `app` 服务会自动将 MongoDB / InfluxDB 等地址改为容器内网络主机名（`mongo`、`influxdb` 等），覆盖 `.env` 里的 `localhost` 配置。

可选：

- `ENABLE_SCHEDULER=false` — 不启动 `run_scheduler.py`
- `HOST_UI=0.0.0.0` — 监听地址（默认已设置）

## 数据卷

- `app_logs` → `/app/logs`
- `app_data` → `/app/data`


## 拉取官方基础镜像（InfluxDB 等）

`docker-compose.yml` 中 postgres / redis / mongo / influxdb 使用 Docker Hub 官方 tag。内网或 Hub 不可达时：

1. 在 `.env` 中设置 `HTTP_PROXY` / `HTTPS_PROXY`（见 `.env.example`「Docker 拉镜像代理」）。
2. **Docker Desktop**：Settings → Resources → Proxies → Manual，填写与 `.env` 相同的代理 URL（仅 export 环境变量无法让 `docker pull` 走代理）。
3. 执行：

```bash
make pull-influx
```

`pull-influx` 会先拉取 `INFLUXDB_IMAGE`（默认 `influxdb:2.7`）；失败时自动尝试 `INFLUXDB_MIRROR`（默认 DaoCloud 加速）并 `docker tag` 为官方名。

可选：在 `.env` 中直接指定 compose 使用的镜像：

```bash
INFLUXDB_IMAGE=docker.m.daocloud.io/library/influxdb:2.7
```

应用镜像仍使用 `reg.hdec.com`；InfluxDB 暂无 `reg.hdec.com` 同步 tag，勿与 `BASE_IMAGE` 混淆。
