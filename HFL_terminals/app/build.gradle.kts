plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    id("io.github.takahirom.roborazzi")
}

android {
    namespace = "com.example.hfl_experiment"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.example.hfl_experiment"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        // BuildConfig fields for server base URL and an optional auth token
        buildConfigField("String", "EDGE_BASE_URL", "\"http://192.168.11.6:8001\"")
        buildConfigField("String", "SERVER_AUTH_TOKEN", "\"secret-token\"")
    }

    packagingOptions {
        resources {
            excludes += setOf(
                "META-INF/DEPENDENCIES",
                "META-INF/LICENSE",
                "META-INF/LICENSE.txt",
                "META-INF/NOTICE",
                "META-INF/NOTICE.txt",
                "META-INF/*.kotlin_module",
                "/META-INF/{AL2.0,LGPL2.1}",
                "META-INF/INDEX.LIST",
                "META-INF/io.netty.versions.properties" // ★ 新しいエラーのためにこの行を追加
            )
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }
    kotlinOptions {
        jvmTarget = "11"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.3"
    }

    testOptions {
        unitTests {
            isIncludeAndroidResources = true
            // 素のJVMテスト(非Robolectric)で android.util.Log 等のスタブが例外を投げるのを防ぐ
            isReturnDefaultValues = true
        }
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    // Process lifecycle (to observe app background/foreground via ProcessLifecycleOwner)
    implementation("androidx.lifecycle:lifecycle-process:2.6.1")
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.ui)
    implementation(libs.androidx.ui.graphics)
    implementation(libs.androidx.ui.tooling.preview)
    implementation(libs.androidx.material3)

    implementation(libs.gson)
    implementation(libs.lifecycle.viewmodel.compose)
    implementation(libs.compose.ui)
    implementation(libs.compose.material3)
    implementation(libs.activity.compose)
    implementation(libs.viewmodel.compose)
    implementation(libs.retrofit)
    implementation(libs.converter.gson)
    implementation(libs.logging.interceptor)
    implementation(libs.kotlinx.coroutines.android.v173)

    implementation("androidx.appcompat:appcompat:1.7.1")
    implementation("com.squareup.okhttp3:okhttp:5.3.2")
    implementation("com.squareup.okhttp3:logging-interceptor:5.3.2")
    implementation("com.squareup.okio:okio:3.5.0")

    // WorkManager (バックグラウンド送信／再試行用)
    implementation("androidx.work:work-runtime-ktx:2.8.1")

    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.0")


    testImplementation(libs.junit)
    // PC(JVM)上で Compose UI テストを実行するための Robolectric 基盤
    testImplementation("org.robolectric:robolectric:4.14.1")
    testImplementation("androidx.test.ext:junit:1.2.1")
    testImplementation(platform(libs.androidx.compose.bom))
    testImplementation("androidx.compose.ui:ui-test-junit4")
    // 既存 DecisionEngineTest が参照する mockito（従来は未宣言でコンパイル不能だった）
    testImplementation("org.mockito:mockito-core:5.14.2")
    testImplementation("com.squareup.okhttp3:mockwebserver:5.3.2")  // app の okhttp 5.3.2 と揃える
    // スクショ検証(PC): Roborazzi
    testImplementation("io.github.takahirom.roborazzi:roborazzi:1.41.0")
    testImplementation("io.github.takahirom.roborazzi:roborazzi-compose:1.41.0")
    testImplementation("io.github.takahirom.roborazzi:roborazzi-junit-rule:1.41.0")
    // async-hfl 由来のテスト (ViewModel / コルーチン) 用
    testImplementation("org.mockito.kotlin:mockito-kotlin:5.2.1")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.7.3")
    testImplementation("androidx.arch.core:core-testing:2.2.0")
    testImplementation("androidx.test:core:1.5.0")
    androidTestImplementation(libs.androidx.junit)
    androidTestImplementation(libs.androidx.espresso.core)
    androidTestImplementation(platform(libs.androidx.compose.bom))
    androidTestImplementation(libs.androidx.ui.test.junit4)
    debugImplementation(libs.androidx.ui.tooling)
    debugImplementation(libs.androidx.ui.test.manifest)


}
