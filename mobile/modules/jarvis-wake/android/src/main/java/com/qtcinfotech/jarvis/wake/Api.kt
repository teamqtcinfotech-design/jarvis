package com.qtcinfotech.jarvis.wake

import android.content.Context
import android.util.Log
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Small HTTP client for the background part, with its own login (device token) that it refreshes
 * itself. Both the reminder watcher and the background conversation use it.
 */
object Api {
    private const val TAG = "JarvisWake"
    @Volatile var tokensDead = false

    fun get(context: Context, path: String, timeoutMs: Int = 12000): JSONObject? = call(context, "GET", path, null, timeoutMs)

    fun post(context: Context, path: String, body: JSONObject, timeoutMs: Int = 40000): JSONObject? =
        call(context, "POST", path, body.toString(), timeoutMs)

    private fun call(context: Context, method: String, path: String, body: String?, timeoutMs: Int): JSONObject? {
        val prefs = WakeBus.prefs(context)
        val server = prefs.getString(WakeBus.KEY_SERVER, "") ?: ""
        if (server.isEmpty()) return null
        val url = "$server/api/v1$path"
        val first = request(method, url, body, prefs.getString(WakeBus.KEY_ACCESS, "") ?: "", timeoutMs)
        if (first.first != 401) return first.second
        if (!refresh(context)) return null
        return request(method, url, body, prefs.getString(WakeBus.KEY_ACCESS, "") ?: "", timeoutMs).second
    }

    private fun refresh(context: Context): Boolean {
        val prefs = WakeBus.prefs(context)
        val server = prefs.getString(WakeBus.KEY_SERVER, "") ?: ""
        val refreshToken = prefs.getString(WakeBus.KEY_REFRESH, "") ?: ""
        if (refreshToken.isEmpty()) {
            tokensDead = true
            return false
        }
        val body = JSONObject().put("refresh", refreshToken).toString()
        val (code, json) = request("POST", "$server/api/v1/auth/refresh/", body, null, 12000)
        if (code != 200 || json == null) {
            if (code == 401) tokensDead = true
            return false
        }
        prefs.edit()
            .putString(WakeBus.KEY_ACCESS, json.optString("access"))
            .putString(WakeBus.KEY_REFRESH, json.optString("refresh", refreshToken))
            .apply()
        return true
    }

    private fun request(method: String, url: String, body: String?, bearer: String?, timeoutMs: Int): Pair<Int, JSONObject?> {
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = method
                connectTimeout = 8000
                readTimeout = timeoutMs
                setRequestProperty("Accept", "application/json")
                if (!bearer.isNullOrEmpty()) setRequestProperty("Authorization", "Bearer $bearer")
                if (body != null) {
                    doOutput = true
                    setRequestProperty("Content-Type", "application/json; charset=utf-8")
                    outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
                }
            }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
            Pair(code, runCatching { JSONObject(text) }.getOrNull())
        } catch (e: Exception) {
            Log.w(TAG, "request failed: ${e.message}")
            Pair(0, null)
        } finally {
            conn?.disconnect()
        }
    }
}
