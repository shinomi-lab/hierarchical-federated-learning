package com.example.hfl_experiment.storage

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import com.example.hfl_experiment.util.PerfLogger

/**
 * Simple SQLite helper to record successful uploads (acks) on the device.
 * Table: send_history
 * Columns: sha TEXT PRIMARY KEY, req_id TEXT, round INTEGER, ack_timestamp TEXT, created_at INTEGER
 */
class SendHistoryDbHelper(context: Context) : SQLiteOpenHelper(context, DB_NAME, null, DB_VERSION) {

    init {
        // Ensure PerfLogger is initialized so DB timings are written out
        PerfLogger.init(context)
    }

    companion object {
        private const val DB_NAME = "send_history.db"
        private const val DB_VERSION = 1
        private const val TABLE = "send_history"
        private const val COL_SHA = "sha"
        private const val COL_REQ_ID = "req_id"
        private const val COL_ROUND = "round"
        private const val COL_ACK_TS = "ack_timestamp"
        private const val COL_CREATED = "created_at"
    }

    override fun onCreate(db: SQLiteDatabase) {
        val sql = """
            CREATE TABLE $TABLE (
                $COL_SHA TEXT PRIMARY KEY,
                $COL_REQ_ID TEXT,
                $COL_ROUND INTEGER,
                $COL_ACK_TS TEXT,
                $COL_CREATED INTEGER
            )
        """.trimIndent()
        db.execSQL(sql)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        // For now, simple migration: drop and recreate
        db.execSQL("DROP TABLE IF EXISTS $TABLE")
        onCreate(db)
    }

    fun markAck(sha: String, reqId: String?, round: Int?, ackTimestamp: String?) {
        val t0 = System.currentTimeMillis()
        val db = writableDatabase
        val values = ContentValues().apply {
            put(COL_SHA, sha)
            put(COL_REQ_ID, reqId)
            put(COL_ROUND, round)
            put(COL_ACK_TS, ackTimestamp)
            put(COL_CREATED, System.currentTimeMillis())
        }
        db.insertWithOnConflict(TABLE, null, values, SQLiteDatabase.CONFLICT_REPLACE)
        val t1 = System.currentTimeMillis()
        PerfLogger.append("SendHistoryDbHelper", "markAck sha=${sha} reqId=${reqId} round=${round} ms=${t1 - t0}")
    }

    fun exists(sha: String): Boolean {
        val t0 = System.currentTimeMillis()
        val db = readableDatabase
        var cursor: Cursor? = null
        return try {
            cursor = db.query(TABLE, arrayOf(COL_SHA), "$COL_SHA = ?", arrayOf(sha), null, null, null)
            val found = cursor.moveToFirst() && !cursor.isAfterLast
            val t1 = System.currentTimeMillis()
            PerfLogger.append("SendHistoryDbHelper", "exists sha=${sha} found=${found} ms=${t1 - t0}")
            found
        } catch (e: Exception) {
            PerfLogger.append("SendHistoryDbHelper", "exists sha=${sha} error=${e.message}")
            false
        } finally {
            cursor?.close()
        }
    }

    fun closeSafe() {
        try { close() } catch (_: Throwable) { }
    }
}
