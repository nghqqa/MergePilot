# 敏感扫描（PR #233 全 diff，20195 行）

- 秘密模式扫描（gho_/ghp_/sk- 长串/POSTGRES_PASSWORD=明文/api_key=字面量）: 3 处命中
  - 全部为 **tools/costmeter 脱敏机制的测试夹具**（假 token `ghp_ABCDEFGHIJK...` 用于断言 redact 输出不含 token）——非真实凭证
- 硬编码 SECRET/PASSWORD/TOKEN/DSN 赋值: 0（唯一 PASSWORD 形状 = env.example 的 SUBSTITUTE_AT_DEPLOY 占位与 ALTER ROLE 参数化传参，值来自环境变量）
- DSN: 示例文件为占位符；isolated_smoke/pgvector_smoke 从环境变量读取，源码不含真实连接串
- 真实凭证 / token / 私钥: **0**
- 历史/运行数据: knowledge 种子行=合成测试数据（本轮 smoke 建的隔离行），非生产数据
- 大型自动生成物: 0
结论: **无秘密、无真实凭证、无运行数据泄漏**。
