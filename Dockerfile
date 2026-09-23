# 一个只发静态文件的镜像：没有后端进程、没有依赖安装、没有可写目录。
#
# 不需要构建期外网：Pyodide 由浏览器在运行时从 CDN 取（约 7.5MB，一年缓存）。
# 代价见 README「网络前提」——用户浏览器必须能访问 CDN 与模型端点。
FROM nginx:1.27-alpine

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

EXPOSE 80
