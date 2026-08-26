# M9-L — v0.1.0-preview.4 Formal Pre-release Publication (2026-08-26)

Release: https://github.com/nghqqa/MergePilot/releases/tag/v0.1.0-preview.4

## Verdict

**MERGEPILOT_V0_1_PREVIEW4_PRERELEASE_PUBLISHED**

The formal v0.1.0-preview.4 pre-release is live. Its 7 assets are the
accepted final-candidate bytes, unchanged (7/7 byte-identical). The
annotated tag points exactly at `5bb2635` (the candidate's
manifest.git_commit, an ancestor of main; main tip at publication was
`46d1677`, the M9-J handoff docs commit).

## Chain of custody

1. **Pre-publication snapshot** — re-downloaded all 7 assets from
   `m9j-preview4-final-candidate`; 7/7 digests matched both the M9-J
   record and the live API; checksums 3/3; asset-sha256 217/217;
   ZIP 213 entries with 0 cache artifacts; manifest identity exact
   (version `v0.1.0-preview.4`, git_commit `5bb2635`, 9/9 images);
   OCI blobs 182/182, config-digest 9/9, manifest-vs-tar match.
2. **Tag** — created annotated (`git tag -a`) at exactly `5bb2635`;
   no tag existed locally or remotely before; pushed with a normal
   push (new tag, no force). Tag object `083b4cb` peels to `5bb2635`.
3. **Release** — `prerelease=true`, `draft=false`, not Latest
   (GET /releases/latest → 404); title "MergePilot v0.1.0 Preview 4";
   the snapshot's original 7 assets uploaded unchanged.
4. **Post-publication readback** — API confirms all fields; formal
   release re-download is 7/7 byte-identical to the snapshot (`cmp`
   on raw bytes); manifest.git_commit == tag peeled SHA; the 5 prior
   releases (preview.1/.2/.3, m9b-rc5-delivery,
   m9j-preview4-final-candidate) keep their digests unchanged.

## Acceptance class

`SAME_MACHINE_ACCEPTED` — the final candidate passed same-machine
black-box full-lifecycle validation (verdict
`MERGEPILOT_M9K_FINAL_CANDIDATE_SAME_MACHINE_ACCEPTED`, evidence at the
test machine). Independent physical-machine acceptance remains
`EXTERNAL_BLOCKED`. This is a Preview and is not production-ready.

## Truth boundaries (frozen, unchanged)

application_integration_verified=false
database_verified=false
production_verified=false
revision_producer_contract=NOT_VERIFIED
audit_producer_contract=NOT_VERIFIED
direct_routing_verified=false
transport_profile=wsl-user-relay

## Files

- `release-v0.1.0-preview.4.json` — release identity + pre-publication verification + notes compliance
- `release-assets-sha256.json` — the 7 published asset digests/bytes
- `tag-verification.json` — annotated-tag creation, exact target, push mode
- `release-post-download.json` — API readback + 7/7 byte-compare + historical digests
- `SUMMARY.md` — this file
