package ru.vksms.relay;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public class SentReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        try (Jobs jobs = new Jobs(context)) {
            int result = getResultCode();
            jobs.partResult(intent.getStringExtra("id"), intent.getIntExtra("part", -1),
                    result == Activity.RESULT_OK, result);
        }
    }
}
