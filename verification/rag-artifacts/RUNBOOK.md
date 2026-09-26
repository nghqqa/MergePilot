# RAG Canary 工件准备 Runbook（不启用）

## A · approved model cache
1. 操作员离线获取模型（embedding 下载保持 DISABLED；D7 单独授权是唯一下载通道）
2. 计算 files 清单 SHA256/bytes，填 approved-cache-manifest.template.json 并署名批准
3. 部署：MERGEPILOT_CR_APPROVED_MANIFEST=<manifest 路径> + MERGEPILOT_CR_EMBEDDING_CACHE_DIR=<cache 路径>
4. 核验：verify_approved_cache(manifest, cache_dir) → 全字段/哈希/尺寸一致（fail-closed：缺文件/哈希不符/manifest 缺失即拒）

## B · case_provider_metadata（已就绪部分）
1. [已做] 隔离 PG 应用 ensure_schema（表结构核验通过，tests_attested 默认 false）
2. 后端写入 provider/model 元数据行（snapshot_digest 64-hex、row_count、dimension）
3. live 合同测试（query↔store 对齐、scope 隔离）通过后置 tests_attested=true
4. 失效行为：无源/非权威/未对齐/未 attest → registration_decision 拒绝（已实测）

## C · RUN_BINDING_AUTH（未闭合 → NOT_WIRED）
1. 密钥生成与登记（run-binding-key.template.json 字段）
2. 分发机制落地（当前缺失——闭合前保持 NOT_WIRED，无降级）
3. 轮换：rotate_by 到期换 key_version；撤销：revocation_list 生效
4. 合同行为已验证：nonce 重放 400 / 篡改 400 / 错 key 400 / required 未绑定 409 / 审计 redaction
