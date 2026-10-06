# MergePilot 官网部署指南（二级域名 + Nginx + 版本目录）

## 前置条件
- 一台 Linux 服务器（Ubuntu 22.04+ 推荐），已安装 Nginx
- DNS A 记录已将二级域名指向服务器 IP
- 端口 80/443 已开放

## 待填写项
| # | 项 | 示例值 |
|---|---|---|
| 1 | 二级域名 | `docs.nghqqa.cn` |
| 2 | 服务器 IP | `your.server.ip` |
| 3 | SSH 用户 | `root` 或 `deploy` |
| 4 | 部署根目录 | `/var/www/docs.nghqqa.cn` |
| 5 | TLS 方案 | Let's Encrypt (certbot --nginx) |

## 部署步骤

### 1. 上传网站文件
```bash
# 本地打包
tar -czf website-dist.tar.gz -C website .

# 上传到服务器
scp website-dist.tar.gz user@server:/tmp/

# 服务器上创建版本目录并解压
ssh user@server
sudo mkdir -p /var/www/<domain>/releases/$(date +%Y%m%d%H%M%S)
sudo tar -xzf /tmp/website-dist.tar.gz -C /var/www/<domain>/releases/<ts>
```

### 2. 创建 current symlink
```bash
sudo ln -sfn /var/www/<domain>/releases/<ts> /var/www/<domain>/current
```

### 3. TLS 证书
```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d <domain>
# 自动续期已由 certbot timer 处理
```

### 4. Nginx 配置
```bash
sudo cp nginx-site.conf /etc/nginx/sites-available/<domain>
# 编辑 <domain> 与 root 路径
sudo ln -s /etc/nginx/sites-available/<domain> /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 5. 文件权限
```bash
sudo chown -R www-data:www-data /var/www/<domain>
sudo find /var/www/<domain> -type d -exec chmod 755 {} \;
sudo find /var/www/<domain> -type f -exec chmod 644 {} \;
```

### 6. 上线验证
```bash
curl -sI https://<domain>/ | head -5     # 确认 200 + 安全头
curl -s https://<domain>/api/health      # 静态站无 API，确认 200 index
```

## 回滚步骤
```bash
# 1. 找上一个版本目录
ls -lt /var/www/<domain>/releases/ | head -3
# 2. symlink 指回旧版本
sudo ln -sfn /var/www/<domain>/releases/<旧ts> /var/www/<domain>/current
# 3. Nginx 不需要 reload（symlink 即时生效）
```

## 升级步骤（新版本发布时）
```bash
# 1. 打包新版 website/
tar -czf website-dist.tar.gz -C website .
# 2. 上传到服务器新版本目录
# 3. symlink 切换
# 4. 旧版本保留 3 个，多余的删除
ls -lt /var/www/<domain>/releases/ | tail -n +4 | awk '{print $NF}' | xargs rm -rf
```

## Nginx 安全注意事项
- 不代理 Console、数据库、frpc 或 webhook 内部端口
- 不暴露 `.env`、`.git`、`CHANGELOG` 等文件
- HTML 不缓存；assets 长期 immutable
- 限流按 IP（建议 30r/s + burst 30）
