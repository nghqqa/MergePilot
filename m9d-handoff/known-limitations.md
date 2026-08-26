# Known Limitations (m9-d)

1. WinNAT port exclusion on the test machine will cause Check to fail
   with WINDOWS_PORT_BIND_UNAVAILABLE — environment condition, not RC defect.
   The test machine ports are reported as restored; verify with:
   `netsh interface ipv4 show excludedportrange protocol=tcp`

2. Doctor in standalone mode reports DOCTOR_NOT_INSTALLED before the
   first Install — correct behavior (no install.json yet).

3. The dev machine has WinNAT exclusion covering 8600; Start was NOT
   tested on the dev machine (port-blocked). Start/Stop/Status/Cleanup
   lifecycle requires the test machine with bindable ports.

4. Evidence git-blob hashing (m9-c §5) and PR#4 hygiene replay (m9-c §6)
   remain deferred.

5. revision_producer_contract and audit_producer_contract remain
   PARTIALLY_VERIFIED (dev-machine pipeline only).
