# M9-B RC Download Instructions

## Download URL (all 5 assets)
https://github.com/nghqqa/MergePilot/releases/tag/m9b-rc-delivery

## Files to download (all must be in the SAME folder)
1. `images-oci.tar` (847.4MB) — 9 stack images
2. `manifest.json` — version manifest (git_commit=3a6e531)
3. `checksums.sha256` — SHA-256 manifest (LF, forward-slash)
4. `mergepilot-v0.1.0-preview.4-rc.1-package.zip` — bootstrapper + docs
5. `EXTERNAL-ACCEPTANCE.md` — step-by-step instructions

## Verification after download
```bash
sha256sum -c checksums.sha256
```

## Installation
```powershell
# Extract the ZIP to a subfolder 'package/'
Expand-Archive mergepilot-v0.1.0-preview.4-rc.1-package.zip -DestinationPath package
.\packageootstrapper.ps1 -Action Check
.\packageootstrapper.ps1 -Action Install -ImageTar images-oci.tar
.\packageootstrapper.ps1 -Action Start
# Browser: http://127.0.0.1:8600/e2e-status.html
.\packageootstrapper.ps1 -Action Cleanup
```

## Expected environment-specific behavior
If WinNAT/Hyper-V port exclusion ranges cover 8600 or 8090 on the test
machine, Check will fail with `WINDOWS_PORT_BIND_UNAVAILABLE` and a
netsh remediation hint. **This is an environment condition, not an RC
defect.** See EXTERNAL-ACCEPTANCE.md for details.

## Branch for code review
`feature/m9-real-pr-contracts` @ `3a6e531` (GitHub API accessible)
