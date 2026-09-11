# M9-D RC.3 Download & Verification

## Download
https://github.com/nghqqa/MergePilot/releases/tag/m9b-rc3-delivery

## 7 Assets (all in the same folder after download)
1. images-oci.tar (9 images, 847.4MB)
2. manifest.json (version=v0.1.0-preview.4-rc.3, git=58cfd47)
3. checksums.sha256 (LF, 3 top-level files)
4. mergepilot-v0.1.0-preview.4-rc.3-package.zip (bootstrapper + tools/ + config/)
5. EXTERNAL-ACCEPTANCE.md
6. SHA256SUMS
7. asset-sha256.json

## Verify
```bash
sha256sum -c checksums.sha256   # 3/3 should be OK
```

## What changed vs RC.2
- Doctor dual-mode: offline (default) does NOT require Dockerfiles
- Full tools/ + config/ bundled → Start works from the package
- Cleanup per-resource report
- install.json written before Doctor in Install flow
