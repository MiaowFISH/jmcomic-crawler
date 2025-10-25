# syntax=docker/dockerfile:1

# 使用官方 uv 基础镜像（内置 Python 与 uv）
FROM ghcr.io/astral-sh/uv:python3.12-bookworm

# 工作目录
WORKDIR /app

# 仅复制依赖清单，提前安装依赖以最大化缓存命中
COPY pyproject.toml uv.lock ./

# 同步依赖（不含 dev），使用锁文件保证可复现
RUN uv sync --frozen --no-dev

# 复制项目源码与配置
COPY app ./app
COPY config.yml ./config.yml
COPY README.md server.md ./

# 暴露服务端口（默认 8000，可由 config.yml 覆盖）
EXPOSE 8000

# 运行 FastAPI 服务（pyproject.scripts: start = "app.main:start"）
CMD ["uv", "run", "start"]