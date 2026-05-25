package com.example.hfl_experiment.util

import android.annotation.SuppressLint
import android.content.BroadcastReceiver
import android.content.Context
import android.content.IntentFilter
import android.os.Build

/**
 * Compatibility helper to register a BroadcastReceiver with RECEIVER_NOT_EXPORTED when possible.
 * We annotate with @SuppressLint so lint does not report missing flags at call sites.
 */
@SuppressLint("UnprotectedBroadcastReceiver")
fun registerReceiverNotExported(context: Context, receiver: BroadcastReceiver, filter: IntentFilter) {
    try {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            // Try to invoke the 3-arg overload that accepts flags.
            try {
                val method = context.javaClass.getMethod("registerReceiver", BroadcastReceiver::class.java, IntentFilter::class.java, Int::class.java)
                method.invoke(context, receiver, filter, Context.RECEIVER_NOT_EXPORTED)
                return
            } catch (e: NoSuchMethodException) {
                // fall through to direct call below
            }
        }
    } catch (_: Throwable) {
        // ignore and fall back
    }

    // Fallback: use the two-arg overload on older platforms or if reflection failed
    context.registerReceiver(receiver, filter)
}

