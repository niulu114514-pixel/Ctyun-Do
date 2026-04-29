# 部署教程

本文档用于部署当前项目，采用 `gunicorn + systemd` 方式运行。

## 1. 环境要求

- Linux 服务器
- Python 3
- 可以访问外网下载依赖和 Playwright 浏览器

如果是 Debian / Ubuntu，先安装基础依赖：

```bash
apt update
apt install -y python3 python3-pip
```

## 2. 进入项目目录

```bash
cd /opt/ctyun
```

如果你的项目目录不是 `/opt/ctyun`，请把后续命令里的路径替换成你的实际路径。

## 3. 安装 Python 依赖

```bash
pip3 install -r requirements.txt
```

安装 Playwright Chromium：

```bash
playwright install chromium
```

## 4. 手动测试启动

先确认项目能正常跑起来：

```bash
cd /opt/ctyun
gunicorn -c gunicorn.conf.py wsgi:application
```

默认监听地址：

```text
http://服务器IP:5000
```

如果页面能打开，就说明服务基本正常。

停止服务可按：

```bash
Ctrl + C
```

## 5. 配置 systemd 开机自启

把服务文件复制到系统目录：

```bash
cp /opt/ctyun/ctyun-login.service /etc/systemd/system/ctyun-login.service
```

重新加载 systemd：

```bash
systemctl daemon-reload
```

设置开机自启并立即启动：

```bash
systemctl enable --now ctyun-login.service
```

查看服务状态：

```bash
systemctl status ctyun-login.service
```

## 6. 查看运行日志

实时查看日志：

```bash
journalctl -u ctyun-login.service -f
```

查看最近日志：

```bash
journalctl -u ctyun-login.service -n 100
```

## 7. 常用服务命令

启动服务：

```bash
systemctl start ctyun-login.service
```

停止服务：

```bash
systemctl stop ctyun-login.service
```

重启服务：

```bash
systemctl restart ctyun-login.service
```

查看是否开机自启：

```bash
systemctl is-enabled ctyun-login.service
```

## 8. 开放端口

如果服务器启用了防火墙，需要放行 `5000` 端口。

例如使用 `ufw`：

```bash
ufw allow 5000/tcp
```

## 9. 更新代码后的操作

如果你修改了项目代码，执行：

```bash
cd /opt/ctyun
systemctl restart ctyun-login.service
```

## 10. 当前部署注意事项

当前项目必须使用现在这份 `gunicorn` 配置：

- `1 worker`
- 多线程

原因是短信验证码登录会话保存在应用内存里。  
如果你把 `workers` 改成大于 `1`，登录请求和验证码请求可能进入不同进程，导致验证码续登失败。

所以当前正确方式是：

- 可以多个用户同时使用
- 但必须运行在单进程多线程模式

## 11. JSON 文件保存位置

登录成功后，原始 JSON 会保存到 `data` 目录中。

主要有两类文件：

- `data/ctyun_state_手机号_sessionid.json`
  每一次登录独立保存一份，不会和别的登录混在一起。
- `data/ctyun_state_手机号.json`
  这个手机号最近一次登录结果的快捷文件。

网页上显示给用户复制的 JSON，来自当前这次登录对应的独立文件内容。

## 12. 推荐的访问方式

如果只是内网使用，直接访问：

```text
http://服务器IP:5000
```

如果要正式对外提供访问，建议再加一层 Nginx 反向代理，并绑定域名。

## 13. 虚拟环境部署（可选）

建议使用虚拟环境隔离项目依赖：

```bash
# 1. 安装虚拟环境支持（如果没有）
apt install python3-venv -y

# 2. 进入项目目录
cd /opt/ctyun

# 3. 创建虚拟环境（命名为 venv）
python3 -m venv venv

# 4. 激活虚拟环境
source venv/bin/activate

# 5. 安装项目依赖
pip install -r requirements.txt

# 6. 运行测试（先试前台运行）
python app.py
```