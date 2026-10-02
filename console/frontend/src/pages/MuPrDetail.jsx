// MuPrDetail 路由页（/mu/repos/:owner/:name/pr/:n）——Wave 3.16 后为薄壳：
// 内容块复用 MuPrDetailContent（master-detail 右栏同一实现）。
import React from 'react';
import { Link, useParams } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { MuPrDetailContent } from './MuPrDetailContent.jsx';

export function MuPrDetail() {
  const params = useParams();
  const owner = params.owner ?? '';
  const name = params.name ?? '';
  const prNumber = Number(params.prNumber);
  const [repoId, setRepoId] = useState(null);

  useEffect(() => {
    let dead = false;
    fetch('/api/mu/repositories', { credentials: 'same-origin' })
      .then((r) => r.json().catch(() => null))
      .then((repos) => {
        if (dead) return;
        const repo = (repos?.repositories ?? []).find((r) => r.owner === owner && r.name === name);
        setRepoId(repo?.repo_id ?? null);
      })
      .catch(() => { if (!dead) setRepoId(null); });
    return () => { dead = true; };
  }, [owner, name]);

  return (
    <div>
      <div className="breadcrumb"><Link className="crumb-back" to="/multiuser">返回组织与接入</Link></div>
      <MuPrDetailContent prRef={repoId ? { repoId, owner, name, prNumber } : null} />
    </div>
  );
}
