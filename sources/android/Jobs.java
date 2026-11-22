package ru.vksms.relay;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;
import org.json.JSONException;
import org.json.JSONObject;

final class Jobs extends SQLiteOpenHelper {
    Jobs(Context context) {
        super(context, "usb-jobs.db", null, 1);
    }

    @Override public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE jobs (id TEXT PRIMARY KEY, parts INTEGER NOT NULL," +
                " seen INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 0," +
                " error INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending'," +
                " detail TEXT NOT NULL DEFAULT '')");
    }

    @Override public void onUpgrade(SQLiteDatabase db, int oldVersion, int version) {
        throw new IllegalStateException("Unsupported database version");
    }

    static JSONObject answer(String state, String detail) throws JSONException {
        return new JSONObject().put("state", state).put("detail", detail);
    }

    JSONObject status(String id) throws JSONException {
        try (Cursor row = getReadableDatabase().rawQuery(
                "SELECT state, detail FROM jobs WHERE id = ?", new String[]{id})) {
            return row.moveToFirst() ? answer(row.getString(0), row.getString(1)) : answer("missing", "");
        }
    }

    boolean claim(String id, int parts) {
        ContentValues values = new ContentValues();
        values.put("id", id);
        values.put("parts", parts);
        return getWritableDatabase().insertWithOnConflict(
                "jobs", null, values, SQLiteDatabase.CONFLICT_IGNORE) != -1;
    }

    void markReview(String id) {
        ContentValues values = new ContentValues();
        values.put("state", "review");
        values.put("detail", "Не удалось подтвердить передачу SMS в Android.");
        getWritableDatabase().update("jobs", values, "id = ? AND state = 'pending'", new String[]{id});
    }

    void partResult(String id, int part, boolean success, int result) {
        if (id == null) return;
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try (Cursor row = db.rawQuery("SELECT parts, seen, ok, error FROM jobs " +
                "WHERE id = ? AND state = 'pending'", new String[]{id})) {
            if (!row.moveToFirst() || part < 0 || part >= row.getInt(0)) return;
            int previous = row.getInt(1);
            int bit = 1 << part;
            if ((previous & bit) != 0) return;
            int seen = previous | bit;
            int ok = row.getInt(2) | (success ? bit : 0);
            int error = success ? row.getInt(3) : result;
            ContentValues values = new ContentValues();
            values.put("seen", seen);
            values.put("ok", ok);
            values.put("error", error);
            if (Integer.bitCount(seen) == row.getInt(0)) {
                values.put("state", ok == seen ? "sent" : ok == 0 ? "failed" : "review");
                values.put("detail", ok == seen ? "Android подтвердил отправку всех частей."
                        : (ok == 0 ? "Android отклонил SMS." : "Отправлена только часть SMS.")
                          + " Код: " + error + ".");
            }
            db.update("jobs", values, "id = ?", new String[]{id});
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }
}
