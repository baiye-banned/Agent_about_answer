# 部署指南（Nginx / DNS / HTTPS）

本文件是生产部署的完整细节。本地运行与依赖安装见 [README 快速开始](../README.md#快速开始)。

## Nginx 反向代理

下面示例假设：

- 域名：`example.com`
- 前端静态文件目录：`/var/www/rag/dist`
- 后端地址：`http://127.0.0.1:8002`

```nginx
server {
    listen 80;
    server_name example.com;

    root /var/www/rag/dist;
    index index.html;

    location = /health {
        proxy_pass http://127.0.0.1:8002/health;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8002/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_buffering off;
        proxy_read_timeout 300s;
    }

    location /uploads/ {
        proxy_pass http://127.0.0.1:8002/uploads/;
    }
}
```

`proxy_buffering off` 对 SSE 很重要，否则流式回答可能被 Nginx 缓冲，导致前端不能实时显示。

健康检查必须单独代理：后端的健康检查路由注册在**根路径 `/health`**，不在 `/api/` 前缀下，所以上面的 `location = /health` 不能省。省略它的话，`/health` 会落进 `location /` 的 `try_files`，返回前端 `index.html`（`200` + `text/html`）而不是健康检查的 JSON，部署自检就会误判。另外**`/api/health` 并不存在**，请求它只会得到 `404`。

关于接口文档：本示例**故意不代理** `/docs`、`/redoc`、`/openapi.json`。这三个是 FastAPI 挂在根路径下的交互文档与 OpenAPI Schema，按上述配置在公网不可达（会被 `location /` 兜到前端页面），可以避免对外暴露完整的接口结构。如果确实需要在受控环境里访问，在 server 块内补充：

```nginx
    # Swagger UI 页面本身是 /docs，它还会请求 /docs/oauth2-redirect，
    # 因此精确匹配 /docs 之外还要放行 /docs/ 子路径。
    # proxy_set_header 在 location 之间互不继承（写在 server 级才会被各 location 继承），
    # 这里逐块书写，方便单独复制其中一段。
    location = /docs {
        proxy_pass http://127.0.0.1:8002/docs;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /docs/ {
        proxy_pass http://127.0.0.1:8002/docs/;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /redoc {
        proxy_pass http://127.0.0.1:8002/redoc;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /openapi.json {
        proxy_pass http://127.0.0.1:8002/openapi.json;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
```

这里统一用 `=` 精确匹配，避免 `/docsXYZ` 这类并不存在的路径也被转发到后端。`proxy_set_header Host $host;` 同样是必须的：不写的话 Nginx 默认把 `Host` 设成上游地址（`127.0.0.1:8002`），而 Starlette 对 `/docs/` 会回一个 `307` 跳转到 `/docs`，跳转目标里就会带上 `http://127.0.0.1:8002` 这个只在服务器内部可达的地址，浏览器拿到后会跳不过去。四个块里还一并设置了 `X-Forwarded-Proto`（与上面 `/api/` 块保持一致），这样从 HTTPS 入口访问时 `307` 直接跳到 `https://`；少了它，跳转目标会是 `http://`，还要经 80 端口再 `301` 回 443，白多一次往返。开启前建议配合 IP 白名单（`allow` / `deny`）或额外的鉴权，不要直接暴露在公网。

## DNS 配置

如果要绑定域名，需要在域名服务商处添加解析记录：

| 类型 | 主机记录 | 指向 |
| --- | --- | --- |
| A | `@` | 服务器公网 IPv4 |
| A | `www` | 服务器公网 IPv4 |
| CNAME | `api` | 可选，指向主域名或后端网关域名 |

配置后可用以下命令检查解析：

```bash
nslookup example.com
```

或：

```bash
dig example.com
```

DNS 生效可能需要几分钟到数小时，取决于 TTL 和域名服务商。

## HTTPS 配置

生产环境建议使用 HTTPS。可以通过 Let's Encrypt 申请免费证书：

```bash
sudo certbot --nginx -d example.com -d www.example.com
```

证书签发后，确认 Nginx 中存在 443 配置，并将 HTTP 自动跳转到 HTTPS：

```nginx
server {
    listen 80;
    server_name example.com www.example.com;
    return 301 https://$host$request_uri;
}
```

上面是 HTTP → HTTPS 跳转。`certbot --nginx` 是**原地改写**匹配到 `example.com` 的那个 server 块（也就是上文「Nginx 反向代理」里监听 80 的那一个），所以那里的 `location = /health` 通常会被一并保留；但如果你另行编写了 443 的 server 块，或换用了其它证书签发方式，就必须逐条确认 443 块里也有这条规则，否则 HTTPS 入口的健康检查仍会被 `location /` 兜成前端页面：

```nginx
server {
    listen 443 ssl;
    server_name example.com;
    # ssl_certificate / ssl_certificate_key 等由 certbot 写入

    location = /health {
        proxy_pass http://127.0.0.1:8002/health;
    }

    # 其余 location / 与 location /api/ 的配置同「Nginx 反向代理」
}
```

HTTPS 部署后需要检查：

- `https://example.com` 可以打开前端页面。
- `https://example.com/health` 可以正常返回健康检查 JSON。
- `/api/chat/stream` 流式输出不会被代理缓冲。
- `VITE_API_BASE_URL` 与 Nginx 代理路径一致。
- 生产环境 `SECRET_KEY` 已替换为强随机值（未替换时后端会拒绝启动）。
- CORS、Cookie、安全响应头按真实部署域名收紧。

其中健康检查这一项可以直接用命令验证：

```bash
curl -i https://example.com/health
```

期望结果是 `200`、`content-type: application/json`，响应体为 `{"status":"ok"}`。如果拿到的是 `text/html`，说明 `/health` 被 `location /` 兜到了前端页面（缺少上文「Nginx 反向代理」的 `location = /health`）；如果拿到 `404`，则是请求了并不存在的 `/api/health`。
