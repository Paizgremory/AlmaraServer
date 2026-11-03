plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.almara.server"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.almara.server"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
        ndk { abiFilters += listOf("arm64-v8a") }
    }

    buildTypes {
        release { isMinifyEnabled = false }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    sourceSets["main"].java.srcDir("src/main/kotlin")
    packaging {
        // libffmpeg.so / libffprobe.so harus diekstrak ke disk supaya bisa dijalankan
        jniLibs.useLegacyPackaging = true
    }
}

chaquopy {
    defaultConfig {
        version = "3.12"
        pip {
            install("flask")
            install("requests")
            install("certifi")
            install("edge-tts")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
}
