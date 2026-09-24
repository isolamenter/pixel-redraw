# 一个只发静态文件的镜像：没有后端进程、没有可写目录。
# Pyodide WASM 运行时与 Python 核心源码已内置于镜像内，无需外网 CDN。
FROM nginx:1.27-alpine

RUN apk add --no-cache openssl && \
    mkdir -p /etc/nginx/ssl && \
    openssl req -x509 -newkey rsa:2048 -nodes \
      -keyout /etc/nginx/ssl/key.pem \
      -out /etc/nginx/ssl/cert.pem \
      -days 3650 \
      -subj "/CN=pixel-redraw" \
      -addext "subjectAltName=IP:192.168.1.186,IP:192.168.1.36,IP:127.0.0.1,DNS:localhost" && \
    apk del openssl

COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf

# 前端：index.html + app.css + js/
COPY static/ /usr/share/nginx/html/

# 核心 Python 源码放在 web 根上，由浏览器里的 Pyodide 直接 fetch 进 WASM 文件系统。
# 仓库里那一份就是唯一一份：不打包、不转译、不生成副本，所以页面跑的算法
# 和 tests/ 测的是同一段代码。
COPY pixel_redraw.py pixel_palettes.py pixel_pipeline.py pixel_color.py pixel_reduce.py /usr/share/nginx/html/

# busybox wget 是 alpine 自带的，不需要额外装东西
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

EXPOSE 80 443
