# Cleanup: Emulator-specific configs removed

- Removed `android:networkSecurityConfig` from `app/src/main/AndroidManifest.xml` (now relies on `usesCleartextTraffic=true` for local testing on real devices).
- Deleted obsolete `app/src/main/res/xml/network_security_config.xml`.
- Confirmed there is no `app/src/debug/` folder left; emulator-only configs were not present.
- Project builds and runs on real devices; no emulator-specific paths remain.
