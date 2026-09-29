# Holdings mobile release guide

Holdings uses the same React screens for the website, iOS, and Android. Capacitor bundles the built frontend into each native project. Windows uses the responsive website. The mobile package ID is `ir.parhambm.holdings` on both platforms.

## Architecture and behavior

- The app calls the Django API over HTTPS. Its default API origin is `https://portfolio.parhambm.ir`; set `VITE_API_URL` at build time to target a staging HTTPS server. Do not put a development server URL into a release build.
- Browser sessions keep the existing HTTP-only refresh cookie and CSRF flow. Mobile sessions use `/api/auth/mobile/` login, registration, refresh, logout, logout-all, and password-change endpoints. The API checks the Capacitor origin and returns a rotating refresh token in JSON. The app keeps the refresh token in iOS Keychain or Android Keystore-backed secure storage, and the access token only in memory. A normal web origin cannot call the body-token endpoints through CORS. This origin check is not device attestation.
- The online UI contains Portfolio, Activity, Markets, Guidance, onboarding, account settings, and admin-only Operations. Legacy URLs redirect to those destinations. Operations links to Django admin open in the device browser.
- When the device disconnects, the app replaces online pages with a read-only offline screen. It saves only the last fetched account names, totals, holdings, allocation inputs, and 30-day net-worth points in encrypted, account-scoped secure storage. It displays the saved timestamp and the server-declared monetary basis. Device PIN, passcode, or biometric authentication is required on each cold launch and after returning from background before showing the saved figures. Sign-out removes the local snapshot and refresh token. Other data and all editing require a connection.
- The offline screen's sign-out clears local credentials immediately. If the device is disconnected, the server cannot receive a revocation request; a previously issued refresh token expires under the normal server policy. Online sign-out calls the server first.
- The native shell opens outbound links through the system browser, shares data exports using the native share sheet, responds to Android Back, and respects safe areas. There is no push notification, offline editing, or separate biometric sign-in.

## Build and run

1. Install Node.js and the frontend dependencies: `cd frontend && npm ci`. Install current Xcode from the Mac App Store for iOS, or Android Studio with the SDK and emulator for Android. Xcode Command Line Tools alone cannot build an iOS app. Use a Mac for iOS signing and simulator runs. Android can be built on a supported desktop platform.
2. Set the API target only if building against staging: `export VITE_API_URL=https://your-staging-host`. That server must include `capacitor://localhost` and `http://localhost` in CORS, serve the mobile auth endpoints, and have a valid TLS certificate. For a production build, unset `VITE_API_URL` to use the production origin.
3. Run `npm run build:mobile` in `frontend/`. This runs Vite and syncs assets/plugins into `ios/` and `android/`. Run it after each frontend or plugin change.
4. Run `npm run ios` to open Xcode, select a simulator or device, set the Apple development team under Signing & Capabilities, then build. Run `npm run android` to open Android Studio, allow Gradle/SDK setup, select an emulator or device, then build. The same app ID must be kept for all builds of an installed test app.
5. For private testing, archive and upload the signed iOS build to TestFlight internal testing. Publish an Android App Bundle to a Play Console internal testing track. Keep signing certificates, provisioning profiles, Android upload keys, and API credentials outside Git. If local SDK setup is impractical, select a build service only after the app and API are ready for device testing; use the same app ID and signing ownership.

### Release signing (secrets stay off Git)

**iOS (TestFlight)**

1. In Xcode → App target → Signing & Capabilities, enable Automatically manage signing and select the Apple Developer team. That writes `DEVELOPMENT_TEAM` into the local xcuserdata only; do not commit team IDs or provisioning profiles.
2. Product → Archive, then Distribute App → App Store Connect → Upload. Use an App Store Connect API key or Apple ID with access to the `ir.parhambm.holdings` app record.
3. Promote the build to TestFlight Internal Testing. Confirm Face ID / device passcode unlock still works on a physical device before any external testers.

**Android (Play internal track)**

1. Create an upload keystore outside the repo, e.g. `keytool -genkeypair -v -keystore ~/holdings-upload.jks -keyalg RSA -keysize 2048 -validity 10000 -alias holdings`.
2. Add a local `android/keystore.properties` (gitignored) with `storeFile`, `storePassword`, `keyAlias`, `keyPassword`. Wire it in `android/app/build.gradle` under `android.signingConfigs.release` and set `buildTypes.release.signingConfig signingConfigs.release` only on the machine that builds release AABs — never commit those values.
3. `cd android && ./gradlew :app:bundleRelease`, then upload `app/build/outputs/bundle/release/app-release.aab` to a Play Console internal testing track.

`PrivacyInfo.xcprivacy` is committed under `ios/App/App/` for App Store privacy declarations. Deep links / custom URL schemes are intentionally unused: password reset stays a website URL opened in the system browser. Capacitor still defines `custom_url_scheme` in Android `strings.xml` for its own `http://localhost` WebView origin — that is not an app deep-link entry.

App icons and splash graphics are generated in the native projects from `frontend/public/lattice.svg` and `frontend/assets/holdings-foreground.svg` / `holdings-splash.svg`. Native project changes are committed; generated copied web assets, build outputs, and local signing files are ignored.

## Verification and release gate

Run backend auth tests and the full backend suite against fixture-backed local/staging databases; run `npm run lint`, `npm run build`, `npm run test:vitest`, and `npm run test:unit`. Browser review must cover 390px phone, 768px tablet, and 1366px laptop widths, including all four destinations, onboarding, account settings, Operations role denial, and legacy redirects. Native simulator/device review must cover first login, relaunch refresh rotation, password change, logout/revocation, Android Back, admin external links, Persian asset names, Toman/Rial/USD unit labels, offline unlock after background, account switching, and offline edit blocking.

The MVP staging checklist in `MVP-IMPLEMENTATION-CHECKLIST.md` still has financial storage conversion, rounding audit, provider counter bootstrap, and deployment gates open. Do not point a device build at production for authenticated testing until the new server API is deployed and those gates have passed. Initial production smoke checks are read-only: sign in with a test account, inspect totals and unit labels, browse destinations, validate roles and redirects, then sign out. Use fixtures for automated write tests.

## Native verification on 2026-09-24

- An Android 35 ARM emulator booted on Apple silicon. `npm run build:mobile` and `:app:assembleDebug` produced `frontend/android/app/build/outputs/apk/debug/app-debug.apk` with package ID `ir.parhambm.holdings`. The APK installed and reached sign-in. Against an isolated local PostgreSQL fixture, native login returned tokens, relaunch rotated the refresh token, and browser routes for Portfolio, Activity, Markets, Guidance, and account settings loaded. A Persian-labelled coin valued at 50,000,000 Toman displayed correctly. Offline unlock required the emulator PIN and showed the timestamped total, 30-day chart, allocation, and holding; returning from background hid it again. Offline sign-out erased the saved view, and online sign-out returned HTTP 204 from the revocation endpoint. The first emulator run exposed a secure-storage proxy startup hang; that path was fixed and covered by `src/mobile.spec.js`.
- This Mac has Java 21, Android SDK Platform 36, Build Tools 35.0.0, and an Android 35 ARM emulator installed. On this network, Google's normal Android SDK and Maven host returned 404, so the packages were obtained from Google's redirector and verified against Google's repository checksums. The local Gradle build used a temporary Maven redirector init script. Standard SDK/Gradle downloads should work on other networks.
- Authenticated production testing remains gated: the default production API does not yet have the new mobile auth deployment, and the MVP financial migration and release checklist gates are open. The fixture test used a debug-only Android network policy permitting cleartext to `localhost` over `adb reverse`; release builds retain HTTPS-only policy. Use a fixture-backed staging API for any broader role, unit, and mutation checks before production.
- Xcode 27 is installed on this Mac. As of 2026-09-25 the Xcode license is accepted and an `iphonesimulator` arm64 Debug build succeeds after `npm run build:mobile`; interactive simulator smoke still needs an iOS Simulator runtime installed via Xcode → Settings → Platforms. Xcode cannot run on the Linux VPS. Signed private distribution also requires the owner's Apple and Android signing setup.

### VPS Android build

The Debian 13 VPS at `45.139.10.12` has OpenJDK 21 and a separate Android SDK under `/opt/apps/holdings-mobile-build/sdk`. A copy of this branch's tracked frontend lives under `/opt/apps/holdings-mobile-build/source/portfolio-saas/frontend`. It was built on 2026-09-24 with the existing `node:22-alpine` image for `npm ci && npm run build:mobile`, then with:

```sh
cd /opt/apps/holdings-mobile-build/source/portfolio-saas/frontend/android
JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 \
ANDROID_HOME=/opt/apps/holdings-mobile-build/sdk \
./gradlew :app:assembleDebug --no-daemon
```

The resulting `app/build/outputs/apk/debug/app-debug.apk` is a 7.4 MB debug APK. `aapt` confirmed `ir.parhambm.holdings`, minimum SDK 24, and target SDK 36; `apksigner verify` confirmed an APK v2 signature. SHA-256: `75c08b7a255e966f078c9b93f9da097c1b67a912bc3c1e6b35b48ca867f5f006`. This debug signature is local to the VPS and is not a release signing key. Before each new VPS build, copy current tracked frontend source from the isolated branch and rerun `npm ci && npm run build:mobile`. The live portfolio deployment and Docker containers were not changed. About 8 GB remained free after the build; monitor disk space before adding emulator images or keeping multiple build artifacts.

The same isolated source also passed frontend lint, 53 Vitest checks, and 26 Node unit checks in `node:22-alpine` on the VPS.

## Branch completion pass on 2026-09-25

In-repo follow-ups on this branch:

- Offline holdings list keys now prefer the saved `item.key` (`holdingRowKey`), with Vitest coverage for unlock/lock and `MobileRuntime` offline banner / admin external links.
- Capacitor template Android tests under `com.getcapacitor.myapp` were removed.
- `PrivacyInfo.xcprivacy` was added to the iOS App target; docs index lists `MOBILE.md`; release signing steps and keystore gitignore are documented.
- CI gained a `mobile` job (`npm run build:mobile` + `:app:assembleDebug` package-id check) that fails PRs on native bit-rot but does not gate website deploy.

Verification on this Mac (2026-09-25):

- Frontend: `npm run lint` clean; Vitest **60** passed (was 53), including new offline/runtime specs.
- Android: with Homebrew OpenJDK 21 and `$HOME/Library/Android/sdk`, `:app:assembleDebug` produced `app-debug.apk` (`ir.parhambm.holdings`, min 24 / target 36).
- iOS: after `npm run build:mobile`, `xcodebuild … -sdk iphonesimulator ARCHS=arm64 CODE_SIGNING_ALLOWED=NO` **BUILD SUCCEEDED**; `PrivacyInfo.xcprivacy` is present in the `.app` bundle. No iOS Simulator *runtime* is installed on this Mac (`simctl` lists no devices), so interactive simulator smoke (login / offline unlock UI) remains pending until a runtime is installed in Xcode → Settings → Platforms.

Still outside automated control (owner actions):

- Install an iOS Simulator runtime, then run the full native smoke matrix in the simulator.
- Deploy mobile auth + CORS to a staging HTTPS API, then rebuild with `VITE_API_URL` and complete authenticated device testing. Do not point authenticated builds at production until MVP financial/release gates pass.
- Provide Apple team + Android upload keystore for TestFlight / Play internal tracks.
