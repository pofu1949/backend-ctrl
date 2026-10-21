# CtrlStore 的可选容器镜像（决议 Q13）。systemd 是主交付方式，这里只提供
# 一个可复现的构建；数据目录与配置通过 volume 挂进去。
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/ctrlstore

# 先拷构建所需的文件：wheel 会把 alembic 一起打进包里（hatch force-include）。
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic ./alembic
RUN pip install --no-cache-dir .

RUN useradd --system --home-dir /var/lib/ctrlstore --shell /usr/sbin/nologin ctrlstore \
    && mkdir -p /var/lib/ctrlstore \
    && chown ctrlstore:ctrlstore /var/lib/ctrlstore

USER ctrlstore
EXPOSE 8000

# 默认只起 API；worker 用同一个镜像、不同 command（见 compose 示例）。
CMD ["ctrlstore", "serve", "--host", "0.0.0.0", "--port", "8000"]
