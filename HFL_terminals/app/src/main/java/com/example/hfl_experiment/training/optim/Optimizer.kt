package com.example.hfl_experiment.training.optim

/** 任意の最適化手法の共通インタフェース */
interface Optimizer {
    /** 1 step 更新（スケジューラがあれば lr を更新してから呼ぶ） */
    fun step()

    /** モーメントなどの状態をリセット */
    fun reset()

    /** パラメタグループを全て返す（監視/デバッグ用） */
    fun paramGroups(): List<ParamGroup>
}
