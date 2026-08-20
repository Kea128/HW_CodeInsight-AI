# Ubuntu 安装 Portainer CE

## 1. 确认 Docker 已安装

```bash
sudo docker version
```

如果尚未安装 Docker，请参考官方文档：

https://docs.docker.com/engine/install/ubuntu/

启动 Docker，并设置为开机自动启动：

```bash
sudo systemctl enable --now docker
```

## 2. 下载 Portainer 镜像

该步骤可以省略；运行 Portainer 时，Docker 会自动下载缺少的镜像。

```bash
sudo docker pull portainer/portainer-ce:lts
```

## 3. 创建持久化数据卷

```bash
sudo docker volume create portainer_data
```

该数据卷用于保存 Portainer 的账号、配置和环境信息。

## 4. 启动 Portainer

```bash
sudo docker run -d \
  --name portainer \
  --restart=always \
  -p 9443:9443 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v portainer_data:/data \
  portainer/portainer-ce:lts
```

## 5. 检查运行状态

```bash
sudo docker ps
```

查看 Portainer 日志：

```bash
sudo docker logs portainer
```

## 6. 开放防火墙端口

仅在 Ubuntu 启用了 UFW 时执行：

```bash
sudo ufw allow 9443/tcp
sudo ufw status
```

> 不建议将 `9443` 端口直接暴露到公网。建议通过局域网、VPN、Tailscale 或防火墙白名单访问。

## 7. 获取 Ubuntu IP 地址

```bash
hostname -I
```

## 8. 从 Windows 访问

在 Windows 浏览器中打开：

```text
https://Ubuntu服务器IP:9443
```

例如：

```text
https://192.168.1.100:9443
```

首次访问可能出现自签名证书警告。确认地址正确后继续访问，并创建管理员账号。

## 常用管理命令

停止 Portainer：

```bash
sudo docker stop portainer
```

启动 Portainer：

```bash
sudo docker start portainer
```

重启 Portainer：

```bash
sudo docker restart portainer
```

实时查看日志：

```bash
sudo docker logs -f portainer
```

检查 `9443` 端口是否监听：

```bash
sudo ss -lntp | grep 9443
```
