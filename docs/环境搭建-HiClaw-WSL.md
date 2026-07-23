# HiClaw 环境搭建(WSL2 + DeepSeek)

> 目标:把 AgentTeams/HiClaw 在本地 WSL 跑起来,登录 Element Web,创建第一个测试 Worker,验证 Manager-Worker 通信。这是前置原型的第一步。
> 适用:Windows + WSL2(Ubuntu-22.04)+ WSL 内 Docker Engine。**不需要 Docker Desktop。**

---

## 0. 预检结果(已确认 ✅)

| 项 | 状态 |
|---|---|
| WSL 发行版 | Ubuntu-22.04(WSL2)✅ |
| Docker(WSL 内) | Docker 29.5.2,daemon 正常 ✅ |
| 内存 | 23G(空闲 22G)✅ |
| 磁盘 | 664G 可用 ✅ |
| 端口 18080/18001/18088/18888 | 全部空闲 ✅ |

> 与已有 Docker 镜像无冲突(HiClaw 用专属命名 `hiclaw-*`)。

---

## 1. 安装(在 WSL 终端里跑)

打开 **Windows Terminal → Ubuntu-22.04**,选下面一种方式。

### 方式 A:交互式(推荐,简单)

```bash
bash <(curl -sSL https://higress.ai/hiclaw/install.sh)
```

按提示填(关键几项):
- **安装模式**:选 **手动配置 / OpenAI 兼容 / 自定义 Base URL**(不是阿里云百炼那项)
- **Base URL**:`https://api.deepseek.com`
- **API Key**:粘贴你的 DeepSeek key(`sk-...`)
- **默认模型**:`deepseek-v4-flash`(V4 系列;`deepseek-chat`/`deepseek-reasoner` 2026/07/24 弃用,勿用)
- **最大上下文长度**:按 DeepSeek 模型页标注的 max context 填;找不到就填 `128000`(或回车接受默认 150000,服务端会截断)
- 其余 temperature / max_tokens 等:**一路回车用默认**
- **管理员用户名**:回车用默认 `admin`
- **管理员密码**:回车自动生成 —— **务必记下它打印的密码**
- **域名**:回车用默认
- **GitHub PAT**:回车跳过(原型阶段先用 Mock,不接真实 GitHub)

### 方式 B:非交互(一行,填好 key 直接跑)

```bash
HICLAW_LLM_PROVIDER=openai-compat \
HICLAW_OPENAI_BASE_URL=https://api.deepseek.com \
HICLAW_DEFAULT_MODEL=deepseek-v4-flash \
HICLAW_LLM_API_KEY=粘贴你的DeepSeek_key \
bash <(curl -sSL https://higress.ai/hiclaw/install.sh)
```

> 若仍弹出 admin 密码/域名/GitHub PAT 提示,一路回车用默认;**记下打印的管理员密码**。

首次会拉几个 GB 镜像,约 5–15 分钟,看到 `=== HiClaw Manager Started! ===` 即成功。

---

## 2. 验证(装完跑这几条,或让我帮你查)

```bash
# 两个主容器在跑
docker ps | grep -E 'hiclaw-controller|hiclaw-manager'
# 浏览器登录 Element Web:http://127.0.0.1:18088  (admin / 你记下的密码)
# 声明式 CLI 可用
docker exec hiclaw-controller hiclaw get workers
```

我这边可以代你查容器状态(只读):
```bash
wsl -- docker ps --format '{{.Names}}\t{{.Status}}' | grep hiclaw
```

---

## 3. 创建第一个测试 Worker(冒烟)

登录 Element Web 后,向 `manager` 私信:
> 请创建一个名为 alice 的 Worker,负责代码审查任务。

或命令行:
```bash
docker exec hiclaw-controller hiclaw create worker --name alice --model deepseek-v4-flash
```

验证:
- Element Web 出现 alice 的房间(3 成员:你、manager、alice)
- `docker ps | grep hiclaw-worker-alice` 有容器在跑
- 给 alice 发个简单任务(如"写个 hello.py"),看她在房间回复

能跑通这一步,HiClaw 环境就稳了,可以开始搭 MergePilot 的 4 个 Agent。

---

## 4. 常见问题

| 现象 | 对策 |
|---|---|
| 端口被占 | 安装时选手动配置,把 18080/18088 换端口 |
| `docker: Cannot connect to daemon` | WSL 里 `sudo service docker start` |
| DeepSeek 连不上 | 确认 key 有效、`https://api.deepseek.com` 可达;模型名用 `deepseek-v4-flash`(V4 系列;旧名 `deepseek-chat` 7/24 弃用) |
| Worker 没自动创建 | curl 安装可能没挂 docker socket;Manager 会回一条 `docker run` 命令,复制到 WSL 手动跑即可 |

---

## 5. 后续:DeepSeek ↔ 阿里云百炼 一键切换

决赛 Demo 想贴合推荐工具栈时,经 Higress 网关换 provider,代码不动:
```bash
# 切百炼(示例,值按你百炼账号填)
HICLAW_LLM_PROVIDER=qwen HICLAW_LLM_API_KEY=百炼key make install   # 或重新跑 install.sh 选百炼
```
开发继续用 DeepSeek(便宜、顺手),两边好处都拿。
