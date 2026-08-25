# M9 外部验收与真实 PR 案例轮 · 冻结基线与裁决

- 日期：2026-08-25
- Release 基线：v0.1.0-preview.3（tag → commit 379744d，main 一致，fetch 后核对）
- 冻结输入：SAME_MACHINE_ACCEPTED / EXTERNAL_BLOCKED / tests=2246 passed, 20 skipped /
  offline images=9 / images-oci.tar=847,382,016 bytes / transport_profile=wsl-user-relay /
  direct_routing_verified=false / production_verified=false
- preview.1/.2/.3 的 tag 与 Release 资产本轮零改动（复核确认原 digest）
- 本轮机器角色：仅验收操作（按维护者指令）；代码修复开发移交原机器
- 裁决：MERGEPILOT_M9_EXTERNAL_REALCASE_BLOCKED

## 唯一外部阻塞条件

不存在第二台可用的独立 Windows 11 物理机。本机（同机）不能替代外部验证。

## 本轮新增缺陷（全部移交原机器修复；分支 fix/m9-pgvector-pin-and-checksums
已含 A/B 的复现测试与最小修复供评审，未合入 main，未发布）

- A（资产回归）：preview.3 checksums.sha256 为 CRLF + 反斜杠路径，
  `sha256sum -c` 12/12 全部失败（preview.2 已修过的回归）。
- B（发布阻断，离线安装不可完成）：pgvector 三方字节不一致 ——
  代码 pin（Id 语义）8e5355e9…、随包 tar 实际字节 7f58c993…、
  registry 现 tag ccc6e83d…/旧 digest a3625087…。
  `docker inspect .Id` 语义随存储后端漂移（graph2=config digest，
  containerd=manifest digest），单 pin 门禁在全新机器上必然
  PGVECTOR_NOT_CACHED；随后 postgres 计划仍按 config-Id ref 下发
  `docker run`（containerd 后端 rc=125）。含 doctor 错误信息不含
  可接受值清单的问题。
- C（环境阻断，Windows 出版边）：本机 WinNAT/Hyper-V 动态保留区间
  覆盖 8550-8649 与 8050-8149 → forwarder 绑 127.0.0.1:8600/8090
  被内核拒绝（WinError 10013）；Check 只测"无监听"不测"可绑定"，
  因此直到最后一步才失败并全量回退。
- D（栈启动，仅在修复分支运行时暴露）：demo-console 容器 exit 1 /
  unhealthy，自动回退；诊断捕获 logs_tail 仅含预检横幅，无真实错误
  （可诊断性缺陷）。未定位根因，移交原机器。
- E（工具链覆盖）：sast-scan 对受控真实路径穿越（open(os.path.join(
  BASE_DIR, name)) 未校验）报告 0 findings；失败测试反而证明了缺陷。
  risk-classify 仅给 L1（SOURCE_CONFIG_CHANGE），未识别新增文件访问面。

## 真实性边界（不变）

application_integration_verified=false / database_verified=false /
production_verified=false / revision_producer_contract=NOT_VERIFIED /
audit_producer_contract=NOT_VERIFIED。本轮无运行时栈内管道执行，
revision/audit 合同评估证据不足，维持 NOT_VERIFIED。
