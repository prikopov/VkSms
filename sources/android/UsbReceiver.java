package ru.vksms.relay;

import android.Manifest;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.telephony.SmsManager;
import android.telephony.SubscriptionManager;
import android.telephony.TelephonyManager;
import android.util.Base64;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import org.json.JSONObject;

public class UsbReceiver extends BroadcastReceiver {
    private static final int FLAGS = Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING;

    @Override public void onReceive(Context context, Intent intent) {
        if (!isOrderedBroadcast() || !"ru.vksms.relay.USB".equals(intent.getAction())) return;
        try {
            String encoded = intent.getStringExtra("payload");
            JSONObject request = new JSONObject(new String(Base64.decode(encoded, FLAGS), StandardCharsets.UTF_8));
            JSONObject response = handle(context, request);
            setResultData(Base64.encodeToString(response.toString().getBytes(StandardCharsets.UTF_8), FLAGS));
            setResultCode(82);
        } catch (Exception error) {
            android.util.Log.e("VkSmsUSB", "USB command failed", error);
            setResultCode(83);
        }
    }

    private JSONObject handle(Context context, JSONObject request) throws Exception {
        String operation = request.getString("op");
        if ("ping".equals(operation)) {
            String problem = problem(context);
            return Jobs.answer(problem.isEmpty() ? "ready" : "blocked", problem);
        }
        String id = request.getString("id");
        if (!id.matches("[0-9a-f]{32}")) return Jobs.answer("rejected", "Неверный идентификатор задания.");
        try (Jobs jobs = new Jobs(context)) {
            JSONObject existing = jobs.status(id);
            if ("status".equals(operation) || !"missing".equals(existing.getString("state"))) return existing;
            if (!"send".equals(operation)) return Jobs.answer("rejected", "Неизвестная команда.");
            String problem = problem(context);
            if (!problem.isEmpty()) return Jobs.answer("failed", problem);
            String phone = request.getString("phone");
            String text = request.getString("text");
            if (!phone.matches("\\+7[0-9]{10}") || text.trim().isEmpty() || text.length() > 400)
                return Jobs.answer("failed", "Неверный номер или длина текста SMS.");
            SmsManager manager = SmsManager.getDefault();
            ArrayList<String> parts = manager.divideMessage(text);
            if (parts.isEmpty() || parts.size() > 10)
                return Jobs.answer("failed", "Недопустимое количество частей SMS.");
            if (!jobs.claim(id, parts.size())) return jobs.status(id);
            try {
                ArrayList<PendingIntent> callbacks = new ArrayList<>();
                for (int part = 0; part < parts.size(); part++) {
                    Intent callback = new Intent(context, SentReceiver.class)
                            .setData(Uri.parse("vksms://sent/" + id + "/" + part))
                            .putExtra("id", id).putExtra("part", part);
                    callbacks.add(PendingIntent.getBroadcast(
                            context, 0, callback, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_ONE_SHOT));
                }
                if (parts.size() == 1)
                    manager.sendTextMessage(phone, null, parts.get(0), callbacks.get(0), null);
                else
                    manager.sendMultipartTextMessage(phone, null, parts, callbacks, null);
            } catch (RuntimeException error) {
                jobs.markReview(id);
            }
            return jobs.status(id);
        }
    }

    private String problem(Context context) {
        if (context.checkSelfPermission(Manifest.permission.SEND_SMS) != PackageManager.PERMISSION_GRANTED)
            return "Откройте VkSms USB и выдайте разрешение на отправку SMS.";
        TelephonyManager telephony = context.getSystemService(TelephonyManager.class);
        if (telephony == null || !telephony.isSmsCapable())
            return "Устройство не поддерживает отправку SMS.";
        if (!SubscriptionManager.isValidSubscriptionId(SmsManager.getDefaultSmsSubscriptionId()))
            return "Установите активную SIM и выберите SIM по умолчанию для SMS.";
        return "";
    }
}
