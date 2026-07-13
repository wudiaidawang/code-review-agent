FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 暴露 API 端口
EXPOSE 8000

# 默认启动 API 服务；CLI 模式通过 docker run ... python -m app.cli review ... 覆盖
CMD ["python", "-m", "uvicorn", "app.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
