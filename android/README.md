# YAAH Android Thin Client

A lightweight Android app that connects to an existing YAAH desktop instance on the LAN, providing a live view and remote control of agent sessions.

## Architecture

Talks to the existing `/api/remote/...` REST endpoints on the YAAH desktop's FastAPI backend.

## Requirements

- Android SDK 34
- Gradle 8.0+
- Java 17+

## Building

```bash
cd android
./gradlew assembleDebug
```

The APK will be at `app/build/outputs/apk/debug/app-debug.apk`.

## Testing on Device

```bash
adb install app/build/outputs/apk/debug/app-debug.apk
```

## Running

Connect to your YAAH desktop on the same LAN. The app will attempt to discover it via mDNS, or you can enter the IP:port manually.

## Tech Stack

- Kotlin + Jetpack Compose + Material 3
- Retrofit + OkHttp for HTTP
- AndroidX lifecycle components