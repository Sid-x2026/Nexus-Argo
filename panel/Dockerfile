# Nexus-Argo 管理面板镜像 (v1 精简版)
# python:3-alpine + docker CLI: 面板用标准库跑 HTTP 服务,
# 通过 /var/run/docker.sock 调宿主 Docker 完成部署。
FROM python:3-alpine
RUN apk add --no-cache docker-cli curl openssl
WORKDIR /app
COPY panel-server.py panel.html ./
EXPOSE 2053
CMD ["python3", "/app/panel-server.py"]
