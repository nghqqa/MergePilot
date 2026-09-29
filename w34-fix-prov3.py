import io
p = 'deploy/agentteams-beta/provision-workers.sh'
lines = io.open(p, encoding='utf-8').read().split('\n')

# find markers
i_patch_comment = next(i for i, l in enumerate(lines) if l.strip().startswith('# 3) '))
i_done = next(i for i, l in enumerate(lines) if i > i_patch_comment and l.strip() == 'done')
# per-role loop content: takeover ends just before patch comment. Insert touch before patch comment.
touch_block = """  # 3) reconcile（Sleeping→Running touch）——必须在模型 patch 之前（touch 触发 controller
  #     重写 openclaw.json，会覆盖 deepseek-direct 模型配置）
  curl -s -m 10 -o /dev/null -X PUT -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \\
    -d '{"spec":{"state":"Sleeping"}}' "http://127.0.0.1:$PORT/api/v1/workers/mergepilot-$ROLE"
  sleep 2
  curl -s -m 10 -o /dev/null -X PUT -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \\
    -d '{"spec":{"state":"Running"}}' "http://127.0.0.1:$PORT/api/v1/workers/mergepilot-$ROLE"
  sleep 2
""".split('\n')
lines[i_patch_comment:i_patch_comment] = touch_block[:-1]

# recompute done (shifted)
i_patch_comment = next(i for i, l in enumerate(lines) if l.strip().startswith('# 4) '))
i_done = next(i for i, l in enumerate(lines) if i > i_patch_comment and l.strip() == 'done')

# replace comment '# 3)' original (now after touch) — keep patch itself; after loop done add nothing.
# remove the OLD trailing touch block (lines from '# 4) reconcile' .. before final check) — it was already inside loop now.
i_old_touch = next(i for i, l in enumerate(lines) if l.strip().startswith('# 4) reconcile'))
i_check = next(i for i, l in enumerate(lines) if i > i_old_touch and 'READY_JSON=' in l)
# between i_done+1 and i_check there may be old touch block — delete [i_done+1, i_check)
del lines[i_done + 1:i_check]

io.open(p, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines))
print('reordered')
