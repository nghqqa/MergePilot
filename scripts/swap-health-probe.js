// swap-health-probe.js — swap 工具的容器健康探针（由 swap-console.cjs fs 读入后经
// docker exec <container> node -e "<本文件内容>" 执行）。独立成文件的原因：
// 内联字符串经多层转义往返极易损坏（rc.35.2 排障终局实录——healthScript 字符串
// 在多次 python 注入后被写坏，运行时 SyntaxError 进 stderr 且被静默丢弃，
// live 门误判失败四连）。本文件只使用单引号字符串，无模板字面量、无反引号。
fetch('http://127.0.0.1:' + (process.env.CONSOLE_PORT || 4730) + '/api/health')
  .then(r => r.json())
  .then(j => console.log('HEALTH ' + (j.data_mode ?? 'unknown') + ' ' + (j.version ?? '')
    + ' commit=' + (j.git_commit ? String(j.git_commit).slice(0, 12) : 'n/a')
    + ' bundle=' + (j.frontend_bundle ? j.frontend_bundle.file : 'n/a')))
  .catch(e => console.log('HEALTH ERR ' + e.message));
