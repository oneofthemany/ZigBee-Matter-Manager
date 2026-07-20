import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Signing material lives in keystore.properties, which is gitignored — the
// keystore password must never end up in version control. See README for the
// keytool command that generates it. Absent the file we simply don't configure
// signing: IDE sync and debug builds still work, and assembleRelease produces
// an *unsigned* APK rather than failing, so the warning below is the only
// thing standing between you and an APK the phone will refuse to install.
val keystorePropsFile = rootProject.file("keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.exists()) {
        keystorePropsFile.inputStream().use { load(it) }
    }
}
val hasSigningConfig = keystoreProps.getProperty("storeFile") != null

android {
    namespace = "com.zmm.presence"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.zmm.presence"
        minSdk = 26          // O — geofencing + notification channels
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    signingConfigs {
        if (hasSigningConfig) {
            create("release") {
                storeFile = rootProject.file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (hasSigningConfig) {
                signingConfig = signingConfigs.getByName("release")
            } else {
                logger.warn(
                    "WARNING: android/keystore.properties not found — the release APK " +
                    "will be UNSIGNED and cannot be installed. See android/README.md."
                )
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    // The ONLY third-party dependency, and it's unavoidable: OS geofencing lives
    // in Play Services. Everything else is AndroidX/Kotlin. No analytics, no
    // crash reporting, no network library — HttpURLConnection is enough for two
    // endpoints, and fewer deps is fewer things that can phone home.
    implementation("com.google.android.gms:play-services-location:21.3.0")
}
