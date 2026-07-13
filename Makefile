# web_scraper_agent — 商机洞察 8091 + 股票 8092（screen + .venv）
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PYTHON := $(ROOT)/.venv/bin/python
HOST_UI ?= 0.0.0.0
PORT_AGRI := 8091
PORT_STOCK := 8092
MONGO_CONTAINER := web_scraper_agent_mongo
INFLUX_CONTAINER := web_scraper_agent_influxdb

SCREEN_AGRI := web_scraper_agent_agri_ui
SCREEN_STOCK := web_scraper_agent_stock_ui
SCREEN_SCHED := web_scraper_agent_sched

LOG_DIR := $(ROOT)/logs
LOG_AGRI := $(LOG_DIR)/agri_ui.log
LOG_STOCK := $(LOG_DIR)/stock_ui.log
LOG_SCHED := $(LOG_DIR)/scheduler.log

DOCKER_DIR := $(ROOT)/docker
DOCKER_REGISTRY ?= reg.hdec.com
DOCKER_IMAGE ?= web_scraper_agent
IMAGE_TAG ?= latest
DOCKER_FULL_IMAGE := $(DOCKER_REGISTRY)/$(DOCKER_IMAGE):$(IMAGE_TAG)
BASE_IMAGE ?= reg.hdec.com/library/python:3.11-slim
COMPOSE_FILE := $(DOCKER_DIR)/docker-compose.yml
export DOCKER_REGISTRY DOCKER_IMAGE IMAGE_TAG BASE_IMAGE

INFLUXDB_IMAGE ?= influxdb:2.7
INFLUXDB_MIRROR ?= docker.m.daocloud.io/library/influxdb:2.7

.PHONY: help start restart stop status start-mongo restart-mongo start-influx restart-influx \
	start-agri restart-agri stop-agri start-stock restart-stock stop-stock \
	start-scheduler restart-scheduler stop-scheduler \
	logs-agri logs-stock logs-scheduler sync-biz-clue \
	pull-influx docker-build docker-up docker-down docker-push docker-logs docker-config

help:
	@echo "WebScraperAgent — 商机洞察 8091 + 股票 8092"
	@echo ""
	@echo "启动:  make start | start-mongo | start-influx | start-agri | start-stock | start-scheduler"
	@echo "重启:  make restart | restart-agri | restart-stock | restart-scheduler"
	@echo "停止:  make stop | stop-agri | stop-stock | stop-scheduler"
	@echo "状态:  make status"
	@echo "日志:  make logs-agri | logs-stock | logs-scheduler"
	@echo "同步:  make sync-biz-clue"

$(LOG_DIR):
	mkdir -p $(LOG_DIR)

start-mongo:
	@if command -v docker >/dev/null 2>&1; then \
		if docker ps -a --format '{{.Names}}' | grep -qx '$(MONGO_CONTAINER)'; then \
			docker start $(MONGO_CONTAINER) || true; \
		else \
			docker compose -f $(COMPOSE_FILE) up -d mongo; \
		fi; \
	else \
		echo "警告: 未安装 docker，跳过 Mongo"; \
	fi

restart-mongo:
	@if command -v docker >/dev/null 2>&1; then \
		if docker ps --format '{{.Names}}' | grep -qx '$(MONGO_CONTAINER)'; then \
			docker restart $(MONGO_CONTAINER); \
		elif docker ps -a --format '{{.Names}}' | grep -qx '$(MONGO_CONTAINER)'; then \
			docker start $(MONGO_CONTAINER); \
		else \
			$(MAKE) start-mongo; \
		fi; \
	else \
		echo "警告: 未安装 docker，跳过 Mongo"; \
	fi

start-influx:
	@if command -v docker >/dev/null 2>&1; then \
		if docker ps -a --format '{{.Names}}' | grep -qx '$(INFLUX_CONTAINER)'; then \
			docker start $(INFLUX_CONTAINER) || true; \
		else \
			docker compose -f $(COMPOSE_FILE) up -d influxdb; \
		fi; \
	else \
		echo "警告: 未安装 docker，跳过 InfluxDB"; \
	fi

restart-influx:
	@if command -v docker >/dev/null 2>&1; then \
		if docker ps --format '{{.Names}}' | grep -qx '$(INFLUX_CONTAINER)'; then \
			docker restart $(INFLUX_CONTAINER); \
		elif docker ps -a --format '{{.Names}}' | grep -qx '$(INFLUX_CONTAINER)'; then \
			docker start $(INFLUX_CONTAINER); \
		else \
			$(MAKE) start-influx; \
		fi; \
	else \
		echo "警告: 未安装 docker，跳过 InfluxDB"; \
	fi

define stop_screen
	@if screen -list 2>/dev/null | grep -q '\.$(1)[[:space:]]'; then \
		screen -S $(1) -X quit || true; \
	fi
endef

stop-agri:
	$(call stop_screen,$(SCREEN_AGRI))
	@pkill -f '[.]venv/bin/python.*scripts/run_agri_ui.py' 2>/dev/null || true
	@pkill -f '[p]ython.*scripts/run_agri_ui.py' 2>/dev/null || true

stop-stock:
	$(call stop_screen,$(SCREEN_STOCK))
	@pkill -f '[.]venv/bin/python.*scripts/run_stock_ui.py' 2>/dev/null || true
	@pkill -f '[p]ython.*scripts/run_stock_ui.py' 2>/dev/null || true

stop-scheduler:
	$(call stop_screen,$(SCREEN_SCHED))
	@pkill -f '[.]venv/bin/python.*scripts/run_scheduler.py' 2>/dev/null || true
	@pkill -f '[p]ython.*scripts/run_scheduler.py' 2>/dev/null || true

stop: stop-agri stop-stock stop-scheduler
	@echo "已停止 screen 会话与 UI/调度器进程"

start-agri: $(LOG_DIR)
	@test -x $(PYTHON) || (echo "错误: 缺少 $(PYTHON)，请先 bash scripts/install.sh" && exit 1)
	$(MAKE) stop-agri
	@cd $(ROOT) && screen -dmS $(SCREEN_AGRI) bash -lc '\
		cd "$(ROOT)" && exec $(PYTHON) scripts/run_agri_ui.py --host $(HOST_UI) --port $(PORT_AGRI) \
		>> "$(LOG_AGRI)" 2>&1'
	@echo "商机洞察 UI screen: $(SCREEN_AGRI) → http://127.0.0.1:$(PORT_AGRI)/"

start-stock: $(LOG_DIR)
	@test -x $(PYTHON) || (echo "错误: 缺少 $(PYTHON)" && exit 1)
	$(MAKE) stop-stock
	@cd $(ROOT) && screen -dmS $(SCREEN_STOCK) bash -lc '\
		cd "$(ROOT)" && exec $(PYTHON) scripts/run_stock_ui.py --host $(HOST_UI) --port $(PORT_STOCK) \
		>> "$(LOG_STOCK)" 2>&1'
	@echo "股票 UI screen: $(SCREEN_STOCK) → http://127.0.0.1:$(PORT_STOCK)/"

start-scheduler: $(LOG_DIR)
	@test -x $(PYTHON) || (echo "错误: 缺少 $(PYTHON)" && exit 1)
	$(MAKE) stop-scheduler
	@cd $(ROOT) && screen -dmS $(SCREEN_SCHED) bash -lc '\
		cd "$(ROOT)" && exec $(PYTHON) scripts/run_scheduler.py \
		>> "$(LOG_SCHED)" 2>&1'
	@echo "调度器 screen: $(SCREEN_SCHED) → 日志 $(LOG_SCHED)"

start: start-mongo start-influx start-agri start-stock start-scheduler
	@echo "全栈已启动（Mongo + InfluxDB + 8091 + 8092 + scheduler）"

restart-agri: stop-agri start-agri
restart-stock: stop-stock start-stock
restart-scheduler: stop-scheduler start-scheduler
restart: stop start

status:
	@echo "== screen 会话 =="
	@screen -ls 2>/dev/null || true
	@echo ""
	@echo "== 端口监听 =="
	@for p in $(PORT_AGRI) $(PORT_STOCK); do \
		if command -v lsof >/dev/null 2>&1 && lsof -iTCP:$$p -sTCP:LISTEN -P -n 2>/dev/null | grep -q LISTEN; then \
			echo "  $$p: 监听中"; \
		else \
			echo "  $$p: 未监听"; \
		fi; \
	done
	@echo ""
	@echo "== HTTP 探测 =="
	@for url in \
		"http://127.0.0.1:8090/insights" \
		"http://127.0.0.1:8090/api/biz-clue/summary" \
		"http://127.0.0.1:$(PORT_AGRI)/api/agri/health" \
		"http://127.0.0.1:$(PORT_STOCK)/" \
		"http://127.0.0.1:$(PORT_STOCK)/insights" \
		"http://127.0.0.1:$(PORT_STOCK)/api/stock/health"; do \
		code=$$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 "$$url" 2>/dev/null || echo "000"); \
		echo "  $$code  $$url"; \
	done

logs-agri:
	@tail -f $(LOG_AGRI)

logs-stock:
	@tail -f $(LOG_STOCK)

logs-scheduler:
	@tail -f $(LOG_SCHED)

sync-biz-clue:
	@test -x $(PYTHON) || (echo "错误: 缺少 $(PYTHON)" && exit 1)
	@cd $(ROOT) && $(PYTHON) scripts/run_daily_biz_clue_sync.py

pull-influx:
	@if ! command -v docker >/dev/null 2>&1; then echo "错误: 未安装 docker"; exit 1; fi
	@if docker pull "$(INFLUXDB_IMAGE)"; then echo "已拉取 $(INFLUXDB_IMAGE)"; \
	elif docker pull "$(INFLUXDB_MIRROR)"; then docker tag "$(INFLUXDB_MIRROR)" "$(INFLUXDB_IMAGE)"; \
	else exit 1; fi

docker-build:
	docker build --build-arg BASE_IMAGE=$(BASE_IMAGE) -f $(DOCKER_DIR)/Dockerfile -t $(DOCKER_FULL_IMAGE) $(ROOT)

docker-up:
	docker compose -f $(COMPOSE_FILE) up -d --build

docker-down:
	docker compose -f $(COMPOSE_FILE) down

docker-push:
	docker push $(DOCKER_FULL_IMAGE)

docker-logs:
	docker compose -f $(COMPOSE_FILE) logs -f app

docker-config:
	docker compose -f $(COMPOSE_FILE) config
