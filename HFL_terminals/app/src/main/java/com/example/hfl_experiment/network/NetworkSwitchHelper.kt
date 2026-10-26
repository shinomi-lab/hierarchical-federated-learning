package com.example.hfl_experiment.network

import android.content.Context
import android.net.*
import com.example.hfl_experiment.util.logging.RealTimeLogger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient

private const val TAG = "NetworkSwitchHelper"

suspend fun <T> requestNetworkAndRun(
    context: Context,
    transportType: Int, // NetworkCapabilities.TRANSPORT_WIFI or TRANSPORT_CELLULAR
    timeoutMs: Long = 12_000L,
    block: suspend (OkHttpClient) -> T
): T? {
    val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
    val request = NetworkRequest.Builder()
        .addTransportType(transportType)
        .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
        .build()

    val deferred = CompletableDeferred<Network?>()
    val callback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            if (!deferred.isCompleted) deferred.complete(network)
        }

        override fun onUnavailable() {
            if (!deferred.isCompleted) deferred.complete(null)
        }

        override fun onLost(network: Network) {
            // treat lost as unavailable if not completed
            if (!deferred.isCompleted) deferred.complete(null)
        }
    }

    try {
        try {
            cm.requestNetwork(request, callback)
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "requestNetwork failed to start: ${e.message}")
            return null
        }

        val network = withTimeoutOrNull(timeoutMs) { deferred.await() } ?: run {
            try { cm.unregisterNetworkCallback(callback) } catch (_: Exception) {}
            RealTimeLogger.w(TAG, "requestNetwork timed out after ${timeoutMs}ms for transport=$transportType")
            return null
        }

        // Build OkHttp client that uses this network's socket factory
        val clientOnNet = OkHttpClient.Builder()
            .socketFactory(network.socketFactory)
            .build()

        return withContext(Dispatchers.IO) {
            try {
                block(clientOnNet)
            } finally {
                // no-op
            }
        }
    } finally {
        try { cm.unregisterNetworkCallback(callback) } catch (e: Exception) { RealTimeLogger.w(TAG, "unregisterNetworkCallback failed: ${e.message}") }
    }
}

/**
 * Return an alternate transport type (WIFI <-> CELLULAR) relative to current active network.
 * If unable to detect, returns null.
 */
fun detectOtherTransport(context: Context): Int? {
    try {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val active = cm.activeNetwork ?: return null
        val caps = cm.getNetworkCapabilities(active) ?: return null
        return when {
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> NetworkCapabilities.TRANSPORT_CELLULAR
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> NetworkCapabilities.TRANSPORT_WIFI
            else -> null
        }
    } catch (e: Exception) {
        RealTimeLogger.w(TAG, "detectOtherTransport failed: ${e.message}")
        return null
    }
}

suspend fun <T> bindProcessToNetworkAndRun(
    context: Context,
    transportType: Int,
    timeoutMs: Long = 12_000L,
    block: suspend () -> T
): T? {
    val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
    val request = NetworkRequest.Builder()
        .addTransportType(transportType)
        .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
        .build()

    val deferred = CompletableDeferred<Network?>()
    val callback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            if (!deferred.isCompleted) deferred.complete(network)
        }
        override fun onUnavailable() {
            if (!deferred.isCompleted) deferred.complete(null)
        }
        override fun onLost(network: Network) {
            if (!deferred.isCompleted) deferred.complete(null)
        }
    }

    try {
        try {
            cm.requestNetwork(request, callback)
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "requestNetwork failed to start: ${e.message}")
            return null
        }

        val network = withTimeoutOrNull(timeoutMs) { deferred.await() } ?: run {
            try { cm.unregisterNetworkCallback(callback) } catch (_: Exception) {}
            RealTimeLogger.w(TAG, "bindProcessToNetwork timed out after ${timeoutMs}ms for transport=$transportType")
            return null
        }

        var bound = false
        try {
            bound = try {
                cm.bindProcessToNetwork(network)
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "bindProcessToNetwork failed: ${e.message}")
                false
            }

            if (!bound) {
                RealTimeLogger.w(TAG, "bindProcessToNetwork returned false; cannot bind process to network")
                return null
            }

            return withContext(Dispatchers.IO) {
                try {
                    block()
                } finally {
                    // no-op
                }
            }
        } finally {
            // unbind
            try {
                cm.bindProcessToNetwork(null)
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "unbind (bindProcessToNetwork(null)) failed: ${e.message}")
            }
        }
    } finally {
        try { cm.unregisterNetworkCallback(callback) } catch (e: Exception) { RealTimeLogger.w(TAG, "unregisterNetworkCallback failed: ${e.message}") }
    }
}
