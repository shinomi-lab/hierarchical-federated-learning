package com.example.hfl_experiment.training

import android.app.Application
import android.os.Looper
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hfl_experiment.training.core.ModelParameters
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/** SP3d 4.1: 新しいモデルを受信しても、学習のセッションを自動で始めない（受信したことだけを表示・記録する）。 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class ModelUpdateNoAutoStartTest {
    @Test fun message() {
        assertTrue(modelReceivedMessage("/x/weight.bin").startsWith("新しいモデルを受信しました（学習はボタン/adb から開始）"))
        assertTrue(modelReceivedMessage(null).endsWith("(不明)"))
    }

    @Test fun modelUpdate_doesNotStartSession() {
        val vm = TrainingViewModel(ApplicationProvider.getApplicationContext<Application>())
        // 旧処理の発火条件を作る: アップロード後に新モデル待ち、かつ前回のパラメータがある、セッション外・未完了
        val cls = TrainingViewModel::class.java
        cls.getDeclaredField("awaitingModelAfterUpload").apply { isAccessible = true }.setBoolean(vm, true)
        cls.getDeclaredField("lastUploadedModelParameters").apply { isAccessible = true }.set(vm, ModelParameters(
            listOf(listOf(1f)), listOf(0f), listOf(1f), listOf(0f), listOf(listOf(1f)), listOf(0f),
            listOf(1f), listOf(0f), listOf(listOf(1f)), listOf(0f)))

        vm.notifyExternalModelUpdate("/x/weight.bin")
        shadowOf(Looper.getMainLooper()).idle()

        // 旧処理では runFiveCycles が呼ばれ、学習データが無いため「学習データが空…」のエラーになり、待ちフラグも下ろされていた
        assertFalse(vm.isTrainingRunning.value)
        assertNull(vm.errorMessage.value)
        assertTrue(cls.getDeclaredField("awaitingModelAfterUpload").apply { isAccessible = true }.getBoolean(vm))
        assertTrue(vm.trainingDataStatus.value.startsWith("新しいモデルを受信しました"))
    }
}
