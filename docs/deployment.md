# 部署与回滚

## Ubuntu2

- 主机：`ubuntu@192.168.1.186`，密码从 `UBUNTU_2_PASSWORD` 环境变量读取。
- 仓库：`/home/ubuntu/pixel-redraw`。
- Compose 服务与容器：`pixel-redraw`。
- 端口：HTTP `8080`、HTTPS `8443`。
- 应用是无状态静态站点；Compose 没有数据卷或数据库迁移。

## 发布

本地先完成与改动相称的验证，确认工作区干净并确定要发布的 `main` 提交。将该分支打成带目标提交短 SHA 的 Git bundle，通过 `scp` 传到 Ubuntu2。`static/pyodide/` 被 Git 忽略，不会进入 bundle；构建前确认远端完整运行时与目标版本一致，缺失或不同才单独复制到远端仓库的 `static/` 目录：

```bash
SSHPASS="$UBUNTU_2_PASSWORD" sshpass -e scp -r static/pyodide ubuntu@192.168.1.186:/home/ubuntu/pixel-redraw/static/
```

在远端仓库确认工作区干净，并记录当前提交及运行镜像 ID；给当前镜像加上带日期和旧镜像 ID 前缀的回滚 tag。然后快进到目标提交并重建、启动服务：

```bash
cd /home/ubuntu/pixel-redraw
git fetch /tmp/pixel-redraw-<目标SHA>.bundle main
git merge --ff-only FETCH_HEAD
docker compose build
docker compose up -d
```

连接服务器或传送文件时使用 `sshpass` 和 `UBUNTU_2_PASSWORD`，不要把密码写入命令、文档或日志。发布后检查 Compose health 状态、HTTP/HTTPS 入口及内置运行时资源：

```bash
docker compose ps
curl -fsS -o /dev/null http://127.0.0.1:8080/
curl -kfsS -o /dev/null https://127.0.0.1:8443/
curl -kfsS -o /dev/null -w '%{http_code} %{content_type}\n' https://127.0.0.1:8443/pyodide/pyodide.mjs
curl -kfsS -o /dev/null -w '%{http_code} %{content_type}\n' https://127.0.0.1:8443/pyodide/pyodide.asm.wasm
curl -kfsS -o /dev/null -w '%{http_code} %{content_type}\n' https://127.0.0.1:8443/pixel_pipeline.py
```

所有请求都应成功；`.mjs` 返回 `text/javascript`，WASM 返回 `application/wasm`，Python 源码返回 `text/plain`。这验证静态入口与容器健康检查；模型 API 的浏览器操作需单独验证。

## 回滚

将发布前保存的镜像 tag（例如 `pixel-redraw:rollback-20260928-e47e734ff935`）重新标记为 `pixel-redraw:latest`，然后让 Compose 使用现有镜像重建容器：

```bash
docker image tag pixel-redraw:rollback-YYYYMMDD pixel-redraw:latest
docker compose up -d --no-build
docker compose ps
```

回滚后再次检查 `healthy` 状态及 HTTP、HTTPS 入口。发布记录应保留旧提交、镜像 ID 和回滚 tag。

该操作回滚容器镜像，不回退仓库提交；仓库仍在新提交上，下一次构建会再次生成新版本镜像。

## 发布记录

### 2026-09-28

- 主机：Ubuntu2（`192.168.1.186`）；路径：`/home/ubuntu/pixel-redraw`；bundle：`pixel-redraw-6a5763ad.bundle`。
- 提交：`93f71cc` → `6a5763ad8ecbdeba9acead88842305744bd6a50d`。
- 发布前镜像 ID：`sha256:e47e734ff93558abd40d8b600947d190b088399810ca2c304c7d3630547eef42`；回滚 tag：`pixel-redraw:rollback-20260928-e47e734ff935`。
- 发布后镜像 ID：`sha256:ebe60359daa5385dc4b371838d1adc3d2b1ebfbc37c8565ef0b2cce05c872277`。
- 结果：容器 `running/healthy`；HTTP 首页返回 200，HTTPS 首页、CSS 与应用 JS 均返回 200。原有完整 Pyodide 运行时与本地目标文件 SHA-256 一致，Git bundle 外无需另行复制。3 个更新后的 JS、5 个 Python 文件及运行时资源（mjs、WASM、NumPy/Pillow wheels、stdlib、lock 文件）线上 SHA-256 均与本地一致，响应 MIME 类型正确；WASM 为 `application/wasm`（9,598,218 字节）。55 项单测通过，`linkcheck` 和 `py_compile` 通过。
- 构建提示缺少 buildx 插件，但 Docker driver 构建成功。未做模型 API 浏览器业务验收。
