# model_gateway — 模型配置化切换与隔离验收

2026-09-23 起,模型调用配置面收敛到本包。**代码默认值 = 当前已验证配置
(deepseek-chat 经 agentteams-gateway),任何切换只经环境/部署配置,不改代码。**

## 现状事实(2026-09-23 复核)

```
worker 容器 (elemiso-worker-<role>)
  └─ .copaw.secret/providers/active_model.json   ← 当前激活模型选择器
       {"provider_id": "agentteams-gateway", "model": "deepseek-chat"}
  └─ .copaw.secret/providers/custom/agentteams-gateway.json  ← provider 定义
       base_url = http://elemiso-controller:8080/v1  (OpenAI-compatible)
  └─ openclaw.json  agents.defaults.model.primary   ← worker 配置层别名
       (models 目录为静态清单,已与上游脱节——不代表可用品类)
higress 网关 (elemiso-ctrl, :8080/v1)
  └─ 上游 AGENTTEAMS_OPENAI_BASE_URL = https://api.deepseek.com
上游 /models 实时目录(2026-09-23 实测): **deepseek-flash, deepseek-v4-pro**
—— deepseek-chat 已不在上游目录(CASE1 之后的上游变更)。
```

`deepseek-flash` 是**上游供应商真实模型名**(api.deepseek.com /models 直接返回),
不是本地网关 alias。

## 配置面(env;全部可选,缺省=当前已验证值)

| 变量 | 含义 | 默认 |
|---|---|---|
| `MERGEPILOT_MODEL` | 请求模型 id | `deepseek-chat` |
| `MERGEPILOT_PROVIDER_BASE_URL` | OpenAI-compatible base URL | `http://elemiso-controller:8080/v1` |
| `MERGEPILOT_MODEL_API_KEY_ENV` | **存 key 的环境变量名**(值不进代码/仓库) | `AGENTTEAMS_GATEWAY_API_KEY` |
| `MERGEPILOT_MODEL_TIMEOUT_S` | 单次请求超时(秒) | `60` |
| `MERGEPILOT_MODEL_MAX_ATTEMPTS` | 有界重试上限 | `3` |
| `MERGEPILOT_MODEL_TEMPERATURE` | 温度(缺省不发送,与现链一致) | 不发送 |
| `MERGEPILOT_MODEL_MAX_TOKENS` | token 上限(缺省不发送) | 不发送 |

## 切换操作杆(生产链)

真正决定 worker 用什么模型的是 **MinIO 里每个角色的 active_model.json**:

1. 改 `agents/<role>/.copaw.secret/providers/active_model.json` 的 `model`
   (如 `deepseek-chat` → `deepseek-flash`);必要时同步 openclaw.json 的
   `agents.defaults.model.primary`(两处保持一致)。
2. `docker exec elemiso-ctrl agt worker wake --name <role>` 重新拉起并同步。
3. 桥派发时从 reviewer 容器只读取 primary 写入 run-manifest(`model.primary`),
   切换后 manifest 自动反映新值,无需改桥。

切换前必须先跑隔离 smoke(下节),通过才允许真实案例。

## 隔离 smoke(零 GitHub 写入)

```bash
# 在 worker 容器内、生产同路径跑(容器内有网关 key,值不落盘不打印):
docker exec elemiso-worker-reviewer python3 /workspace/tools/model_gateway/smoke.py \
    --model deepseek-flash --old-model deepseek-chat --key-file <网关key文件> --out smoke.json
```

八点检查:①目录含目标模型 ②最小非敏感请求 ③响应形状兼容
④401/permanent/timeout 分类(429/5xx 由单测覆盖,不在线制造)
⑤usage 计量 ⑥响应侧模型 id 记录 ⑦旧模型回滚探测 ⑧零 GitHub 结构性声明。

**回滚** = 把 active_model.json(与 openclaw.json)一行改回旧值 + wake。
注意:若旧模型已从上游目录消失(如当前 deepseek-chat),回滚目标不可用——
smoke 点7 会如实标 warn 并给出探测结果。

## 已知缺口(R5 后续)

- 生产 run 的**响应侧**模型 id(上游实际服务的模型)目前只在 smoke 捕获;
  worker 内部不回传。派发侧以 run-manifest `model.catalog_state_at_dispatch`
  (派发时网关实时目录)+ `model.primary`(配置)记录,响应侧留待后续轮。
